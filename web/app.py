#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""script-to-video 导演台后端（标准库 + 可选 jieba 分词；向量相似度走本机 Ollama bge-m3）。

导演台模式：分镜表携带导演参数（景别/运镜/比例/步数/种子），
后端负责把它们折叠进最终提示词，并提交 ComfyUI 生成。

提供：
  GET  /                   前端页面
  GET  /api/state          已保存的分镜状态（刷新页面后恢复）
  GET  /api/status         ComfyUI 队列状态
  GET  /api/progress       ComfyUI 当前生成阶段与采样进度（实时）
  POST /api/optimize       脚本 -> 分镜（含导演参数）
  POST /api/generate       单镜生成（提交 ComfyUI 并轮询）
  POST /api/cancel         取消当前生成（中断 ComfyUI 里的 prompt 并立刻放开生成位）
  POST /api/concat         合成 + 字幕
"""
import base64, io, json, math, os, random, re, shutil, socket, subprocess, threading, time, urllib.request, zipfile, uuid
from collections import deque
from urllib.parse import unquote, urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import jieba
except ImportError:
    jieba = None


def _load_env():
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip().lstrip("\ufeff")
                v = v.strip().strip('"').strip("'")
                if k and (k not in os.environ or not os.environ[k]):
                    os.environ[k] = v
    except Exception:
        pass


_load_env()


_COMFY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
MODELS_DIR = os.environ.get("MODELS_DIR", os.path.join(_COMFY_ROOT, "models"))

# ComfyUI 提交单飞锁：同一时刻只允许一个生成请求，重复点击立即提示，不在 ComfyUI 队列里静默排队
_GEN_LOCK = threading.Lock()
_GEN_ACTIVE = {"scene": "", "deadline": 0.0, "job": None}


def _log(msg):
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def _gen_acquire(scene_id, timeout=1800):
    """占生成位。拿到位返回 (None, 自己的任务句柄)，被别人占着返回 (提示, None)。"""
    with _GEN_LOCK:
        job = _GEN_ACTIVE["job"]
        if job and time.time() < _GEN_ACTIVE["deadline"]:
            el = max(0, int(time.time() - job["started"]))
            return "ComfyUI 正在生成「%s」（已进行 %d分%d秒），请等它完成后再点生成，可在 http://127.0.0.1:8188 查看队列" % (_GEN_ACTIVE["scene"], el // 60, el % 60), None
        job = {"scene": scene_id, "started": time.time(), "prompt_id": "", "abort": threading.Event()}
        _GEN_ACTIVE["scene"] = scene_id
        _GEN_ACTIVE["deadline"] = job["started"] + max(timeout, 60) + 300
        _GEN_ACTIVE["job"] = job
        return None, job


def _gen_release(job):
    """只放自己那一份：取消后用户马上重开新任务时，旧线程不能把新任务的生成位放掉。"""
    with _GEN_LOCK:
        if _GEN_ACTIVE["job"] is job:
            _GEN_ACTIVE.update({"scene": "", "deadline": 0.0, "job": None})


def _gen_active():
    """当前占着生成锁的镜头 id 与已用秒数；空闲或已超时返回 ("", 0)。"""
    with _GEN_LOCK:
        job = _GEN_ACTIVE["job"]
        if not job or time.time() >= _GEN_ACTIVE["deadline"]:
            return "", 0
        return _GEN_ACTIVE["scene"], int(time.time() - job["started"])


def _gen_job():
    """当前占着生成位的任务。持位的线程在提交前记下来，取消后才能精确停掉自己那一次提交。"""
    with _GEN_LOCK:
        return _GEN_ACTIVE["job"]


def _gen_bind_prompt(job, prompt_id):
    """把刚提交的 prompt_id 记到任务上。返回 False 说明提交前后用户已经点了取消，调用方要直接收掉它。"""
    with _GEN_LOCK:
        if job["abort"].is_set():
            return False
        job["prompt_id"] = prompt_id
        return True


def _comfy_cancel_prompt(prompt_id):
    """停住 ComfyUI 里的这次生成：先按 prompt_id 精确取消（在跑就中断、在排队就出队），再全局中断一次收尾。

    只精确取消不够：导播台只记得自己最后一次提交的 prompt_id，ComfyUI 那边可能在跑别的
    （导播台被杀过、过渡帧任务不占生成位、有人在 8188 手动提交过），这时精确取消照样回
    cancelled=true，GPU 却继续跑，前端只能显示「已取消」。全局中断和 ComfyUI 网页端的取消按钮
    是同一条路；中断标志在下一个 prompt 开始执行时会被清掉，不会误伤之后新提交的任务。
    返回 ComfyUI 那边是否接受了这次中断。"""
    base = COMFY_URL.rstrip("/")
    stopped = False
    if prompt_id:
        try:
            r = _post(base + "/api/jobs/" + prompt_id + "/cancel", {})
            stopped = bool(isinstance(r, dict) and r.get("cancelled"))
            _log("已通知 ComfyUI 取消 prompt %s（%s）" % (prompt_id, "已收掉" if stopped else "队列里没有它"))
        except Exception as e:
            _log("通知 ComfyUI 取消 prompt %s 失败：%s" % (prompt_id, e))
    try:
        _post(base + "/interrupt", {})
        _log("已中断 ComfyUI 当前执行的 prompt（导播台 prompt_id=%s）" % (prompt_id or "无"))
        stopped = True
    except Exception as e:
        _log("中断 ComfyUI 失败：%s" % e)
    return stopped


def cancel_running_gen():
    """取消当前生成。先放开生成位再通知 ComfyUI，用户不用等轮询线程收尾就能重新点生成。"""
    with _GEN_LOCK:
        job = _GEN_ACTIVE["job"]
        _GEN_ACTIVE.update({"scene": "", "deadline": 0.0, "job": None})
    if not job:
        # 导播台这边没有在跑的任务，ComfyUI 里可能还压着上一次提交（导播台重启过、过渡帧任务），照旧中断一次
        return {"ok": True, "cancelled": False, "stopped": _comfy_cancel_prompt("")}
    job["abort"].set()
    _log("取消生成 %s（prompt_id=%s）" % (job["scene"], job["prompt_id"] or "尚未提交"))
    return {"ok": True, "cancelled": True, "scene": job["scene"], "stopped": _comfy_cancel_prompt(job["prompt_id"])}


# ComfyUI 实时进度：常连 /ws 收阶段与采样步数，前端每秒取 /api/progress 显示在正在生成的镜头上
# ComfyUI 只把进度发给提交 prompt 的那个 client_id（execution.py 里按 extra_data 路由），所以监听与提交必须用同一个 id
_COMFY_CLIENT = "web"
_LIVE_LOCK = threading.Lock()
_LIVE = {"connected": False, "prompt_id": "", "node": "", "stage": "", "step": 0, "steps": 0, "queue": 0, "started": 0.0}
_LIVE_NODES = {}

_STAGE_NAMES = {
    "UNETLoader": "加载主模型", "CLIPLoader": "加载文本编码器", "VAELoader": "加载 VAE",
    "LoraLoaderModelOnly": "加载 LoRA", "LoadBackgroundRemovalModel": "加载抠图模型",
    "LoadImage": "读取图片", "LoadAudio": "读取音频", "RemoveBackground": "抠主体",
    "MiniMaxH3MemoryEfficientSageAttentionPatch": "应用加速补丁", "MiniMaxH3SigmaShift": "配置采样偏移",
    "MiniMaxH3ImageToVideo": "文本编码 / 准备潜变量", "MiniMaxH3ReferenceToVideo": "参考图编码 / 准备潜变量",
    "SamplerCustomAdvanced": "采样", "SamplerCustom": "采样", "KSampler": "采样",
    "VAEDecode": "解码画面", "VAEDecodeAudio": "解码音频", "CreateVideo": "封装视频", "SaveVideo": "保存视频",
}


def _live_track(graph):
    """记住刚提交的图：/ws 的 executing 事件只给节点号，靠这张表换成可读的阶段名。"""
    with _LIVE_LOCK:
        _LIVE_NODES.clear()
        for nid, node in graph.items():
            _LIVE_NODES[str(nid)] = node.get("class_type") or ""


def _live_stage(node_id):
    cls = _LIVE_NODES.get(str(node_id), "")
    return _STAGE_NAMES.get(cls, cls or "生成中")


def _live_reset():
    """调用方持 _LIVE_LOCK。"""
    _LIVE.update({"stage": "", "node": "", "step": 0, "steps": 0, "started": 0.0})


def _live_apply(msg):
    t = msg.get("type")
    d = msg.get("data") or {}
    with _LIVE_LOCK:
        if t == "status":
            _LIVE["queue"] = ((d.get("status") or {}).get("exec_info") or {}).get("queue_remaining", 0)
        elif t == "execution_start":
            _LIVE.update({"prompt_id": d.get("prompt_id") or "", "stage": "准备", "node": "", "step": 0, "steps": 0, "started": time.time()})
        elif t == "executing":
            if d.get("node"):
                if str(d["node"]) != _LIVE["node"]:
                    _LIVE["step"] = 0  # 换节点说明采样已结束，别把上一步的步数带进解码/保存阶段
                    _LIVE["steps"] = 0
                _LIVE["node"] = str(d["node"])
                _LIVE["stage"] = _live_stage(d["node"])
            else:
                _live_reset()
        elif t == "progress":
            _LIVE["step"] = int(d.get("value") or 0)
            _LIVE["steps"] = int(d.get("max") or 0)
            if d.get("node"):
                _LIVE["node"] = str(d["node"])
                _LIVE["stage"] = _live_stage(d["node"])
        elif t in ("execution_success", "execution_error", "execution_interrupted"):
            _live_reset()


def _live_loop():
    """后台常连 ComfyUI /ws（aiohttp 是 ComfyUI 自带依赖），断线自动重连。"""
    import asyncio
    import aiohttp

    url = COMFY_URL.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/ws?clientId=" + _COMFY_CLIENT

    async def run():
        async with aiohttp.ClientSession() as sess:
            while True:
                try:
                    async with sess.ws_connect(url, heartbeat=20) as ws:
                        with _LIVE_LOCK:
                            _LIVE["connected"] = True
                        async for m in ws:
                            if m.type == aiohttp.WSMsgType.TEXT:
                                _live_apply(json.loads(m.data))
                except Exception:
                    pass
                with _LIVE_LOCK:
                    _LIVE["connected"] = False
                await asyncio.sleep(5)

    asyncio.run(run())


OUTPUT_DIR = os.environ.get("OUTPUT_DIR", os.path.join(_COMFY_ROOT, "output", "video"))
INPUT_DIR = os.environ.get("INPUT_DIR", os.path.join(_COMFY_ROOT, "input"))
ASSETS_DIR = os.environ.get("ASSETS_DIR", os.path.join(_COMFY_ROOT, "assets"))
FONT = os.environ.get("FONT", "C:/Windows/Fonts/simhei.ttf")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
PROJECTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects")
CURRENT_PROJECT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "current_project.json")

MODELS = {
    "unet": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "unet_ref2va": "minimax_h3_ref2va_pruned_fp8_scaled.safetensors",
    "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "vae_video": "minimax_h3_video_vae_fp16.safetensors",
    "vae_audio": "minimax_h3_audio_vae_fp32.safetensors",
}
# 各机器装的 ref2va 量化档不一定一样，按这个顺序认
REF2VA_PREFERENCE = ("minimax_h3_ref2va_pruned_fp8_scaled", "minimax_h3_ref2va_pruned_int8_convrot",
                     "minimax_h3_ref2va_pruned_bf16", "minimax_h3_ref2va_int8_convrot", "minimax_h3_ref2va_bf16")

# ---- 导演参数 ----
ASPECT_RATIOS = {"16:9": (16, 9), "9:16": (9, 16), "1:1": (1, 1), "21:9": (21, 9)}
RESOLUTIONS = {"480p": 480, "720p": 720, "1080p": 1080, "2K": 1440, "4K": 2160}
H3_NATIVE_SHORT = 768      # MiniMax H3 训练画布短边（官方 adapt_canvas 基准）
H3_AREA_CAP = 768 * 1344   # 单帧像素上限，超出会出现伪影/重复图案
# 去 AI 味关键词来自 crafting-ai-video-shot-prompts：光写「高质量画面」没有用，
# 要显式点名「超写实/极致逼真/真人实景拍摄」，并用「杜绝 xxx」反向约束——
# 反向约束比正向描述更管用。
STYLE_SUFFIX = "电影质感，超写实，极致逼真，真人实景拍摄，超写实细节，无字幕无水印。"
H3_AVOID_SUFFIX = ("杜绝游戏CG感、杜绝动作僵硬、杜绝镜头漂移、杜绝塑料皮肤；"
                   "避免出现：字幕、水印、画面模糊、画面闪烁、变形，"
                   "以及人物三只手、多余肢体、畸形手部、畸形面部等解剖错误。")
MODES = {"标准": {"steps": 25, "turbo": False}, "均衡": {"steps": 12, "turbo": False}, "高清": {"steps": 32, "turbo": False}, "极速": {"steps": 4, "turbo": True}}
SHOT_SIZES = ("远景", "全景", "中景", "近景", "特写", "大特写")
CAMERA_ANGLES = ("平视", "仰视", "俯视", "过肩", "鸟瞰")   # 与 static/app.js 的 ANGLES 对齐
CAMERA_MOVES = ("固定镜头", "缓慢推近", "缓慢拉远", "水平摇镜", "垂直摇镜", "跟随镜头", "手持晃动", "环绕运镜")
TURBO_LORA = "minimax_h3_turbo_4step_ema_ckpt850.safetensors"
BG_REMOVAL_MODEL = "birefnet.safetensors"
MODELS_SD = {
    "ckpt": "RealVisXL_V5.0_fp16.safetensors",
    "vae": "RealVisXL_V5.0_vae.safetensors",
}
SD_CANVAS = {"16:9": (1344, 768), "9:16": (768, 1344), "1:1": (1024, 1024), "21:9": (1536, 640)}
DING_NEGATIVE = "text, watermark, logo, signature, deformed, distorted, disfigured, extra limbs, extra fingers, bad hands, bad anatomy, blurry, low quality, jpeg artifacts, cartoon, anime, painting, illustration, oversaturated, plastic skin"
# 故事版关键帧：Flux.1 Kontext dev（参考图编辑，核心原生 ReferenceLatent）
MODELS_KONTEXT = {
    "unet": "flux1-dev-kontext_fp8_scaled.safetensors",
    "t5": "t5xxl_fp8_e4m3fn_scaled.safetensors",
    "clip_l": "clip_l.safetensors",
    "vae": "ae.safetensors",
}
# 视频修补：Wan2.1-VACE 掩膜重绘 + SAM3 按文字圈区域（官方 video_wan_vace_inpainting 模板的接线）
MODELS_VACE = {
    "unet": "wan2.1_vace_14B_fp16.safetensors",
    "clip": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
    "vae": "wan_2.1_vae.safetensors",
    "sam3": "sam3.1_multiplex_fp16.safetensors",
}
VACE_FPS = 16            # VACE 是 16fps 模型，喂 24fps 的片子会被当成 16fps，动作会变慢
VACE_MAX_FRAMES = 81     # 官方说明：单次约 81 帧（≈5 秒）最稳，再长要分段跑
VACE_SHIFT = 5.0
KONTEXT_SYS_REF = ("你是影视分镜师，正在用 Flux Kontext 图像编辑模型生成「故事版关键帧」。"
                   "用户给你一段中文分镜描述和一张角色/场景资产参考图。"
                   "请写成英文编辑指令，必须满足：以 Keep the person's identity, face, hairstyle and clothing unchanged "
                   "开头（若这一镜没有人物，则改用以 Keep the location, layout, materials and lighting style consistent 开头）；"
                   "然后描述这一镜要变成什么样：景别与机位、人物正在做的动作与表情、光线方向与色调、环境与道具、景深虚实。"
                   "角色描述里的性别、年龄段、发型发色、服装与围裙颜色必须原样写进指令，不要改写、不要省略、不要概括成 elderly man 这类笼统说法。"
                   "只输出英文指令本身，一段话，不要分点，不要解释。")
# 同一个镜头里从上一帧改下一帧：参考图就是这一镜自己的上一帧，背景/光线/色调都得留着，
# 只改这一帧真正变化的那点东西。和上面那条（拿资产设定图建场景）不是一回事，混用会把
# 影棚灰底或整片场重建一遍，人就跟着重画了。
KONTEXT_SYS_CHAIN = ("你是影视分镜师，正在用 Flux Kontext 生成「故事版关键帧」的下一帧。"
                     "用户给你这一帧的中文画面描述，参考图是同一个镜头上一帧的画面。"
                     "请写成英文编辑指令，必须满足：以 Keep the subject's identity, face, hairstyle and clothing unchanged, "
                     "and keep the background, the location, the lighting and the colour grading exactly as in the reference frame "
                     "开头（参考图里第一张是这一镜的上一帧，其余几张只是角色与场景的外观参考）；"
                     "然后只描述这一帧相对上一帧发生的变化：人物的动作与表情、被摄主体的位置、画面里多了或少了一样什么。"
                     "不要重新描述整个场景，不要换背景。只输出英文指令本身，一段话，不要分点，不要解释。")
KONTEXT_SYS_T2I = ("你是影视分镜师，正在用 Flux 生成「故事版关键帧」。"
                   "把用户的中文分镜描述写成 SDXL/Flux 通用的英文画面提示词：主体与动作、景别与机位、光线方向与色调、"
                   "环境与道具、景深虚实，末尾加 cinematic film still, photorealistic, highly detailed。"
                   "只输出英文提示词，不要解释。")
# SAM3 是开放词汇分割模型，文本提示要短英文名词短语，不要整句
SAM3_SYS = ("你在给开放词汇分割模型写英文提示词。把用户的中文描述压成一个简短的英文名词短语，"
            "只写物体本身，不要整句、不要写镜头位置之外的啰嗦修饰。"
            "例如「画面右边穿蓝衣服的人」-> person in blue shirt；「桌上那个红色的杯子」-> red cup。"
            "只输出这个英文短语，不要解释。")
# 「换物体」：拿成片的一帧做图生图编辑，只改指定物体，其余一律不动
EDIT_FRAME_SYS = ("你是影视后期修图师。用户会给你一帧视频画面和一句中文修改要求（例如「把红色杯子换成蓝色玻璃瓶」）。"
                  "请写成 Flux Kontext 的英文编辑指令，必须满足："
                  "先写 Keep everything else unchanged — the composition, camera angle, lighting, colour grading, "
                  "the subject, the background and every other object stay exactly as they are；"
                  "再只用一句话描述要换成什么，写清它的形状、材质、颜色、尺寸和它在画面里的位置。"
                  "只输出英文指令本身，一段话，不要解释。")
MODELS_WAN22 = {
    "unet": "wan2.2_ti2v_5B_fp16.safetensors",
    "clip": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
    "vae": "wan2.2_vae.safetensors",
}
WAN_MODE_STEPS = {"标准": 30, "均衡": 20, "高清": 40, "极速": 10}
WAN_DEFAULT_NEGATIVE = "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走"
MODELS_WAN22_14B = {
    "unet_t2v_high": "wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors",
    "unet_t2v_low": "wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors",
    "unet_i2v_high": "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors",
    "unet_i2v_low": "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors",
    "clip": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
    "vae": "wan_2.1_vae.safetensors",
}
WAN14_MODE_STEPS = {"标准": 20, "均衡": 20, "高清": 30, "极速": 10}
MODELS_WAN22_S2V = {
    "unet": "wan2.2_s2v_14B_fp8_scaled.safetensors",
    "clip": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
    "vae": "wan_2.1_vae.safetensors",
    "audio_encoder": "wav2vec2_large_english_fp16.safetensors",
}
S2V_MODE_STEPS = {"标准": 10, "均衡": 10, "高清": 15, "极速": 6}
S2V_CHUNK = 77  # Wan2.2 S2V 官方分块帧数（16fps，每块约 4.8 秒）


# 时长(秒) -> 帧数
def seconds_to_length(sec):
    frames = max(5, round(sec * 24))
    while frames % 17 != 5:
        frames += 1
    return frames


def resolve_geometry(scene, snap=8):
    aspect = scene.get("aspect") or "16:9"
    rw, rh = ASPECT_RATIOS.get(aspect, (16, 9))
    short = RESOLUTIONS.get(scene.get("resolution") or "480p", 480)
    if rw >= rh:
        height = short
        width = round(short * rw / rh)
    else:
        width = short
        height = round(short * rh / rw)
    width = (width // snap) * snap
    height = (height // snap) * snap
    return (max(width, snap), max(height, snap))


def _h3_geometry(scene):
    """H3 画布：短边封顶 768、面积封顶 768×1344、32 对齐（与官方 adapt_canvas 一致）。"""
    aspect = scene.get("aspect") or "16:9"
    rw, rh = ASPECT_RATIOS.get(aspect, (16, 9))
    ratio = rw / rh
    short = min(RESOLUTIONS.get(scene.get("resolution") or "480p", 480), H3_NATIVE_SHORT)
    if ratio >= 1.0:
        w, h = short * ratio, short
    else:
        w, h = short, short / ratio
    if w * h > H3_AREA_CAP:
        s = math.sqrt(H3_AREA_CAP / (w * h))
        w, h = w * s, h * s
    return (max(32, round(w / 32.0) * 32), max(32, round(h / 32.0) * 32))


# 平面/图形类镜头不能用「电影质感、真实实拍风格、超写实细节」收尾：这三个词会把一个
# 纯白底的扁平图形镜头拽成暗调写实片。实测一条「纯白背景 + 线条问号 + 气泡关键词」的
# 分镜，成片出来是黑底青色 —— 就是被这个后缀带偏的。
FLAT_STYLE_SUFFIX = "扁平化动态图形风格，简洁矢量线条，纯色背景干净无噪点，无字幕无水印。"
_FLAT_HINTS = ("纯白背景", "纯色背景", "白色背景", "深色背景", "扁平", "矢量", "图标",
               "简笔", "动态图形", "线条绘制", "逐笔绘制", "平面设计", "信息图", "MG")


def style_suffix_for(text):
    """按画面性质挑风格后缀：图形/动画类走扁平风，实拍类才走电影写实。"""
    t = text or ""
    for h in _FLAT_HINTS:
        if h in t:
            return FLAT_STYLE_SUFFIX
    return STYLE_SUFFIX


# 资产描述注入镜头提示词时的抬头。措辞沿用原来那套「固定项」说法：声明成固定，模型才不会
# 每一镜重新解读一遍。
_ASSET_INJECT = {"character": ("角色", "全程固定同一人"),
                 "equipment": ("设备", "全程固定同一台"),
                 "scene": ("场景", "全程固定同一场景")}


def compose_prompt(scene):

    """把画面描述 + 导演参数折叠成最终提示词。"""
    prompt = (scene.get("prompt") or "").strip()
    while prompt.endswith(("。", "！", "？", "，", "、", " ")):
        prompt = prompt[:-1]
    shot = scene.get("shot") or "中景"
    camera = scene.get("camera") or "固定镜头"
    segs = []
    # 资产按连线注入：这个镜头连到的角色/场景/设备，名称与描述整段照抄。同一段描述在每一
    # 镜里一字不差，人物和场景才不会一镜换一张脸。连线是自动算的（见 link_scene_assets）。
    for e in (scene.get("ref_images") or []):
        desc = str(e.get("description") or "").strip()
        if not desc:
            continue
        label, scope = _ASSET_INJECT.get(e.get("kind"), ("资产", "全程固定"))
        ename = str(e.get("name") or "").strip()
        segs.append("%s（%s）：%s%s" % (label, scope, (ename + "，") if ename else "", desc))
    # 主体 / 背景 / 光线拆成固定项单独注入：埋在自由文本里的话，每次生成都会被重新
    # 解读一遍，背景就会自己变。声明成「固定」并前置，是这套 compose_prompt 里角色/
    # 场景/设备已经在用的办法。
    for label, key, scope in (("主体", "subject", "全片固定"), ("环境/背景", "environment", "本镜固定"),
                              ("光线", "lighting", "本镜固定")):
        val = (scene.get(key) or "").strip()
        if val:
            segs.append("%s（%s）：%s" % (label, scope, val))
    # 风格与画质放在画面内容之前。这条来自 crafting-ai-video-shot-prompts
    # （Mx-Shell 方法论）的实测结论：顺序敏感，模型对提示词前段的注意力权重更高；
    # 把风格放中段、画面内容放最后，能避免风格词污染对人物/地点/背景的字面理解。
    # 判定要看整段——背景搬到 environment 字段后只扫 prompt 会漏判「纯白背景」这类
    # 扁平镜头，白底就会被电影写实的后缀带成暗调。
    segs.append("风格与画质：" + style_suffix_for("。".join(segs) + prompt).rstrip("。"))
    if prompt:
        segs.append(prompt)
    segs.append("景别：" + shot)
    angle = (scene.get("angle") or "").strip()
    if angle:
        segs.append("角度：" + angle)
    segs.append("运镜：" + camera)
    return "。".join(segs) + "。"


def _merged_negative(scene):
    """合并全局与镜头负向词，供 Wan2.2 系列图作为负向 conditioning 使用。"""
    neg = (scene.get("global_negative_prompt") or "").strip()
    shot_neg = (scene.get("negative_prompt") or "").strip()
    if neg and shot_neg:
        return neg + "，" + shot_neg
    return neg or shot_neg


def prepare_scene(scene):
    """补全默认值、解析几何、计算帧数、折叠提示词。"""
    s = dict(scene)
    had_steps = bool(s.get("steps"))
    s.setdefault("shot", "中景")
    s.setdefault("camera", "固定镜头")
    s.setdefault("aspect", "16:9")
    s.setdefault("resolution", "480p")
    s.setdefault("mode", "标准")
    s.setdefault("steps", 25)
    s.setdefault("negative_prompt", "")
    s.setdefault("model", "MiniMax H3")
    s.setdefault("guides", [])
    s.setdefault("need_audio", True)
    s["seconds"] = max(3, min(15, int(s.get("seconds") or 5)))
    if s["model"] == "Wan2.2 14B":
        s["length"] = s["seconds"] * 16 + 1
        s["width"], s["height"] = resolve_geometry(s, snap=16)
    elif s["model"] == "Wan2.2 5B":
        s["length"] = s["seconds"] * 16 + 1
        s["width"], s["height"] = resolve_geometry(s, snap=32)
    elif s["model"] == "Wan2.2 S2V":
        s["length"] = max(S2V_CHUNK, int(s["seconds"] * 16))
        s["width"], s["height"] = resolve_geometry(s, snap=16)
        if not had_steps:
            s["steps"] = S2V_MODE_STEPS.get(s.get("mode") or "标准", 10)
    else:
        s["length"] = seconds_to_length(s["seconds"])
        s["width"], s["height"] = _h3_geometry(s)
    s["steps"] = int(s.get("steps") or 25)
    s["seed"] = int(s["seed"]) if s.get("seed") else None
    s["prompt"] = compose_prompt(s)
    return s


def build_graph(scene):
    if scene.get("model") == "Wan2.2 5B":
        return build_graph_wan22(scene)
    if scene.get("model") == "Wan2.2 14B":
        return build_graph_wan22_14b(scene)
    if scene.get("model") == "Wan2.2 S2V":
        return build_graph_wan22_s2v(scene)
    return build_graph_h3(scene)


def build_graph_wan22(scene):
    prompt = scene["prompt"]
    width = scene.get("width", 832)
    height = scene.get("height", 480)
    length = scene.get("length") or (max(3, min(15, int(scene.get("seconds") or 5))) * 16 + 1)
    seed = scene.get("seed") or random.randrange(0, 2 ** 31)
    mode = scene.get("mode") or "标准"
    steps = scene.get("steps") or WAN_MODE_STEPS.get(mode, 30)
    neg = _merged_negative(scene) or WAN_DEFAULT_NEGATIVE
    prefix = scene.get("prefix", "video/wan22")
    graph = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS_WAN22["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": MODELS_WAN22["clip"], "type": "wan", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS_WAN22["vae"]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": neg, "clip": ["2", 0]}},
        "7": {"class_type": "Wan22ImageToVideoLatent", "inputs": {"vae": ["3", 0], "width": width, "height": height, "length": length, "batch_size": 1}},
        "8": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 8.0}},
        "9": {"class_type": "KSampler", "inputs": {"model": ["8", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["7", 0], "seed": seed, "steps": steps, "cfg": 5.0, "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1.0}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "11": {"class_type": "CreateVideo", "inputs": {"images": ["10", 0], "fps": 16.0}},
        "12": {"class_type": "SaveVideo", "inputs": {"video": ["11", 0], "filename_prefix": prefix, "format": "auto"}},
    }
    first_frame = scene.get("first_frame")
    if first_frame:
        graph["6"] = {"class_type": "LoadImage", "inputs": {"image": first_frame}}
        graph["7"]["inputs"]["start_image"] = ["6", 0]
    voice = scene.get("voice")
    if voice and scene.get("need_audio", True):
        graph["13"] = {"class_type": "LoadAudio", "inputs": {"audio": voice}}
        graph["11"]["inputs"]["audio"] = ["13", 0]
    return graph


def build_graph_wan22_14b(scene):
    """Wan2.2 14B：高噪/低噪两个 UNet 两段采样（官方结构）。有首帧走 I2V，无首帧走 T2V。"""
    prompt = scene["prompt"]
    width = scene.get("width", 832)
    height = scene.get("height", 480)
    length = scene.get("length") or 81
    seed = scene.get("seed") or random.randrange(0, 2 ** 31)
    mode = scene.get("mode") or "标准"
    steps = int(scene.get("steps") or WAN14_MODE_STEPS.get(mode, 20))
    split = max(1, steps // 2)
    neg = _merged_negative(scene) or WAN_DEFAULT_NEGATIVE
    prefix = scene.get("prefix", "video/wan22_14b")
    first_frame = scene.get("first_frame")
    is_i2v = bool(first_frame)
    unet_high = MODELS_WAN22_14B["unet_i2v_high" if is_i2v else "unet_t2v_high"]
    unet_low = MODELS_WAN22_14B["unet_i2v_low" if is_i2v else "unet_t2v_low"]
    graph = {
        "1": {"class_type": "CLIPLoader", "inputs": {"clip_name": MODELS_WAN22_14B["clip"], "type": "wan", "device": "default"}},
        "2": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS_WAN22_14B["vae"]}},
        "3": {"class_type": "UNETLoader", "inputs": {"unet_name": unet_high, "weight_dtype": "default"}},
        "4": {"class_type": "UNETLoader", "inputs": {"unet_name": unet_low, "weight_dtype": "default"}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 0]}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": neg, "clip": ["1", 0]}},
        "7": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["3", 0], "shift": 8.0}},
        "8": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["4", 0], "shift": 8.0}},
    }
    if is_i2v:
        graph["9"] = {"class_type": "LoadImage", "inputs": {"image": first_frame}}
        graph["10"] = {"class_type": "WanImageToVideo", "inputs": {"positive": ["5", 0], "negative": ["6", 0], "vae": ["2", 0], "width": width, "height": height, "length": length, "batch_size": 1, "start_image": ["9", 0]}}
        pos_src, neg_src, latent_src = ["10", 0], ["10", 1], ["10", 2]
    else:
        graph["10"] = {"class_type": "EmptyHunyuanLatentVideo", "inputs": {"width": width, "height": height, "length": length, "batch_size": 1}}
        pos_src, neg_src, latent_src = ["5", 0], ["6", 0], ["10", 0]
    graph["11"] = {"class_type": "KSamplerAdvanced", "inputs": {"model": ["7", 0], "positive": pos_src, "negative": neg_src, "latent_image": latent_src, "add_noise": "enable", "noise_seed": seed, "steps": steps, "cfg": 3.5, "sampler_name": "euler", "scheduler": "simple", "start_at_step": 0, "end_at_step": split, "return_with_leftover_noise": "enable"}}
    graph["12"] = {"class_type": "KSamplerAdvanced", "inputs": {"model": ["8", 0], "positive": pos_src, "negative": neg_src, "latent_image": ["11", 0], "add_noise": "disable", "noise_seed": 0, "steps": steps, "cfg": 3.5, "sampler_name": "euler", "scheduler": "simple", "start_at_step": split, "end_at_step": 10000, "return_with_leftover_noise": "disable"}}
    graph["13"] = {"class_type": "VAEDecode", "inputs": {"samples": ["12", 0], "vae": ["2", 0]}}
    graph["14"] = {"class_type": "CreateVideo", "inputs": {"images": ["13", 0], "fps": 16.0}}
    graph["15"] = {"class_type": "SaveVideo", "inputs": {"video": ["14", 0], "filename_prefix": prefix, "format": "auto"}}
    voice = scene.get("voice")
    if voice and scene.get("need_audio", True):
        graph["16"] = {"class_type": "LoadAudio", "inputs": {"audio": voice}}
        graph["14"]["inputs"]["audio"] = ["16", 0]
    return graph


def build_graph_wan22_s2v(scene):
    """Wan2.2 S2V 语音生视频：LoadAudio → wav2vec2 → WanSoundImageToVideo(+Extend 分块) → KSampler → 解码，16fps 合入原声。"""
    prompt = scene["prompt"]
    width = scene.get("width", 832)
    height = scene.get("height", 480)
    length = int(scene.get("length") or S2V_CHUNK)
    n_chunks = max(1, math.ceil(length / S2V_CHUNK))
    seed = scene.get("seed") or random.randrange(0, 2 ** 31)
    mode = scene.get("mode") or "标准"
    steps = int(scene.get("steps") or S2V_MODE_STEPS.get(mode, 10))
    neg = _merged_negative(scene) or WAN_DEFAULT_NEGATIVE
    prefix = scene.get("prefix", "video/wan22_s2v")
    voice = scene.get("voice")
    if not voice:
        raise ValueError("Wan2.2 S2V 需要说话语音：请先在镜头卡片「上传配音」传一段人声（语音驱动画面）")
    graph = {
        "1": {"class_type": "CLIPLoader", "inputs": {"clip_name": MODELS_WAN22_S2V["clip"], "type": "wan", "device": "default"}},
        "2": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS_WAN22_S2V["vae"]}},
        "3": {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS_WAN22_S2V["unet"], "weight_dtype": "default"}},
        "4": {"class_type": "AudioEncoderLoader", "inputs": {"audio_encoder_name": MODELS_WAN22_S2V["audio_encoder"]}},
        "5": {"class_type": "LoadAudio", "inputs": {"audio": voice}},
        "6": {"class_type": "AudioEncoderEncode", "inputs": {"audio_encoder": ["4", 0], "audio": ["5", 0]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 0]}},
        "8": {"class_type": "CLIPTextEncode", "inputs": {"text": neg, "clip": ["1", 0]}},
        "9": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["3", 0], "shift": 8.0}},
    }
    ref = scene.get("first_frame")
    if ref:
        graph["10"] = {"class_type": "LoadImage", "inputs": {"image": ref}}
    base = {"class_type": "WanSoundImageToVideo", "inputs": {"positive": ["7", 0], "negative": ["8", 0], "vae": ["2", 0], "width": width, "height": height, "length": S2V_CHUNK, "batch_size": 1, "audio_encoder_output": ["6", 0]}}
    if ref:
        base["inputs"]["ref_image"] = ["10", 0]
    graph["11"] = base
    graph["12"] = {"class_type": "KSampler", "inputs": {"model": ["9", 0], "positive": ["11", 0], "negative": ["11", 1], "latent_image": ["11", 2], "seed": seed, "steps": steps, "cfg": 6.0, "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1.0}}
    latent = ["12", 0]
    for i in range(1, n_chunks):
        eid, kid = str(11 + 2 * i), str(12 + 2 * i)
        ext = {"class_type": "WanSoundImageToVideoExtend", "inputs": {"positive": ["7", 0], "negative": ["8", 0], "vae": ["2", 0], "length": S2V_CHUNK, "video_latent": latent, "audio_encoder_output": ["6", 0]}}
        if ref:
            ext["inputs"]["ref_image"] = ["10", 0]
        graph[eid] = ext
        graph[kid] = {"class_type": "KSampler", "inputs": {"model": ["9", 0], "positive": [eid, 0], "negative": [eid, 1], "latent_image": [eid, 2], "seed": seed + 7 * i, "steps": steps, "cfg": 6.0, "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1.0}}
        latent = [kid, 0]
    vid, cid, sid = str(11 + 2 * n_chunks), str(12 + 2 * n_chunks), str(13 + 2 * n_chunks)
    graph[vid] = {"class_type": "VAEDecode", "inputs": {"samples": latent, "vae": ["2", 0]}}
    graph[cid] = {"class_type": "CreateVideo", "inputs": {"images": [vid, 0], "fps": 16.0, "audio": ["5", 0]}}
    graph[sid] = {"class_type": "SaveVideo", "inputs": {"video": [cid, 0], "filename_prefix": prefix, "format": "auto"}}
    return graph


def _build_ref_prompt(prompt, n, scene_ref=None):
    """参考图挂成 <Subject N>/<Picture N>；场景参考图挂成 <Picture K> 描述环境，不占首帧。"""
    lines = []
    if n:
        subs = "\n".join("<Subject %d> is the subject shown in <Picture %d>." % (i, i) for i in range(1, n + 1))
        rets = "\n".join("<Subject %d> (appears in [Shot 1]): fully_preserved - the identity and appearance of <Subject %d> are retained." % (i, i) for i in range(1, n + 1))
        follows = " and ".join("<Subject %d>" % i for i in range(1, n + 1))
        lines.append("subject_definitions:\n" + subs)
        lines.append("summary: [reference generation] the target video keeps " + follows + " consistent.")
        lines.append("retention_analysis:\n" + rets)
    if scene_ref:
        k = n + 1
        lines.append("scene_definitions:\n<Picture %d> shows the environment and location; the generated video is set in this same place, keeping the scene, lighting, and layout consistent with <Picture %d>." % (k, k))
    lines.append("detailed_description: [Shot 1] " + prompt)
    return "\n\n".join(lines)


def _bg_removal_available():
    """H3-only 机器上 BiRefNet 权重只剩占位文件，不能当模型加载。"""
    path = os.path.join(MODELS_DIR, "background_removal", BG_REMOVAL_MODEL)
    try:
        with open(path, "rb") as f:
            head = f.read(8)
            size = os.fstat(f.fileno()).st_size
    except OSError:
        return False
    # safetensors 前 8 字节是 header 长度，占位文本会解出天文数字
    return len(head) == 8 and 8 + int.from_bytes(head, "little") <= size


def resolve_ref2va_unet():
    """目标 ComfyUI 上实际有的 ref2va 权重名；一个都没有就说清楚，别让 ComfyUI 回一句 400。"""
    try:
        options = _get_object_info()["UNETLoader"]["input"]["required"]["unet_name"][0]
    except Exception:
        return MODELS["unet_ref2va"]  # 问不到就用配置值，让提交时报真正的连接错误
    if MODELS["unet_ref2va"] in options:
        return MODELS["unet_ref2va"]
    for pref in REF2VA_PREFERENCE:
        for name in options:
            if name.startswith(pref):
                _log("ref2va 权重改用目标机器上现有的 %s" % name)
                return name
    others = sorted(n for n in options if "ref2va" in n)
    if others:
        _log("ref2va 权重改用目标机器上现有的 %s" % others[0])
        return others[0]
    raise ValueError("目标 ComfyUI 的 models/diffusion_models 里没有 ref2va 权重（minimax_h3_ref2va_*.safetensors），多图参考用不了；"
                     "文件放好后要重启 ComfyUI 才会进 UNETLoader 清单")


_ASSET_STAGE_CACHE = {}
def _stage_asset_ref(rel):
    """把素材池里的图复制进 ComfyUI 的 input 目录，返回 input 相对路径。

    参考图最终是 ComfyUI 的 LoadImage 在读，而 LoadImage 只认 input/ 下的文件；
    素材池在 assets/ 下，不复制过去 ComfyUI 会直接以 Invalid image file 拒绝整次提交。
    路径已经指向 input 的（手动上传的 guides）原样返回，不重复复制。
    """
    try:
        src = resolve_asset_path(rel)
    except Exception:
        return rel
    if not src or not os.path.isfile(src):
        return rel
    try:
        key = (src, os.path.getmtime(src))
        cached = _ASSET_STAGE_CACHE.get(key)
        if cached:
            return cached
        d = os.path.join(INPUT_DIR, "frames")
        os.makedirs(d, exist_ok=True)
        name = "asset_" + re.sub(r"[^0-9a-zA-Z_.-]", "_", os.path.basename(rel))
        shutil.copyfile(src, os.path.join(d, name))
        out = "frames/" + name
        _ASSET_STAGE_CACHE[key] = out
        return out
    except Exception:
        return rel


# ---- 一键出片：简单想法 -> 分镜词 -> 定帧图 -> 故事板 -> 视频 -----------------------
#
# 这条链把散在界面各处的步串成一次运行：LLM 拆镜、逐镜写分镜词、逐镜文生图定帧、
# 可选生成资产六视图、可选逐镜出视频。每一步都是界面里已有的能力，这里只做编排。
#
# 跑在后台线程里，前端轮询 auto_direct_status 看进度 —— 和 generateScene /
# asset_views 同一套模式，因为整条链要几分钟，阻塞式请求会把前端拖死。

AUTO_JOBS = {}
AUTO_LOCK = threading.Lock()


def _auto_set(job, **kw):
    job.update(kw)
    job["ts"] = time.time()


def auto_t2i_frame(scene):
    """给一个镜头文生图定帧，返回 input 相对路径列表（失败返回空）。"""
    scene = prepare_scene(scene)
    en = _translate_prompt(scene["prompt"])
    if not en:
        return []
    stamp = time.strftime("%H%M%S") + "_" + str(random.randrange(100, 999))
    sid = re.sub(r"[^0-9a-zA-Z_-]", "", str(scene.get("id") or "scene"))
    prefix = "frames/auto_" + sid + "_" + stamp
    graph = build_graph_t2i(scene, en, 1,
                            scene.get("seed") or random.randrange(0, 2 ** 31), prefix)
    save_workflow_files(graph, "自动定帧_" + sid)
    busy, lock = _gen_acquire("自动定帧-" + sid, 900)
    if busy:
        return []
    try:
        r = _submit_prompt(graph, timeout=900, images_all=True)
    except Exception:
        r = {"ok": False}
    _gen_release(lock)
    out = []
    d = os.path.join(INPUT_DIR, "frames")
    os.makedirs(d, exist_ok=True)
    for rel in (r.get("files") or []):
        src = os.path.join(_COMFY_ROOT, "output", rel.replace("/", os.sep))
        if not os.path.isfile(src):
            continue
        shutil.move(src, os.path.join(d, os.path.basename(rel)))
        out.append("frames/" + os.path.basename(rel))
    return out


def _asset_view_files(a):
    """资产上的六视图文件。views 可能是 dict（多档）也可能是 list，都兼容。"""
    views = a.get("views") or {}
    if isinstance(views, dict):
        return [v for v in views.values() if isinstance(v, str)]
    return [v for v in views if isinstance(v, str)]


# ---- 资产池与镜头连线 --------------------------------------------------------------
#
# 池子里的每条资产是一条记录：kind 决定它是角色/设备/场景，name 决定它连给谁。
# 镜头通过 scene.refs 连到资产，数组顺序就是 <Subject N> 的编号。
#
# 连线是自动算的，不需要一个个镜头去挂：台词、主体、画面描述里写到资产名字的镜头连它；
# 没起名字的资产算这一类的「默认资产」（老项目里那个全局角色/场景就是这种），挂到每个
# 镜头上。手工排过引用的镜头（refs_manual）不再自动改，否则用户调好的顺序一存盘就被冲掉。

def _find_asset(st, aid):
    aid = str(aid)
    for a in (st.get("assets") or []):
        if str(a.get("id")) == aid:
            return a
    return None


def linked_assets(scene, st):
    """这个镜头连到的资产，顺序就是 scene.refs 的顺序。"""
    out = []
    for aid in (scene.get("refs") or []):
        a = _find_asset(st, aid)
        if a is not None:
            out.append(a)
    return out


def _asset_words(a):
    """资产的匹配词：名称 + 别名。空列表表示这条资产没有名字，按默认资产处理。"""
    words = [str(a.get("name") or "").strip()]
    words += [str(t).strip() for t in (a.get("tags") or []) if str(t).strip()]
    return [w for w in words if w]


def _scene_text(scene):
    """连线只看「画面里有什么」的字段：画面描述、台词、环境。

    环境字段要算进来——「面馆内部」这种镜头未必再写一遍地点名，但环境字段里一定有。
    主体字段不算：那里常常只是拿角色名给场景命名（「老陈的面馆」），照它连会把人物
    塞进一个空镜里，定帧就多出个人。"""
    return " ".join(str(scene.get(k) or "") for k in ("prompt", "subtitle", "environment"))


def _ref_entry(a):
    return {"id": str(a.get("id")), "file": a.get("file"), "kind": a.get("kind") or "character",
            "name": str(a.get("name") or ""), "description": str(a.get("description") or "")}


def link_scene_assets(scene, st, force=False):
    """把一个镜头的连线重算一遍，返回是否有改动。"""
    if scene.get("refs_manual") and not force:
        return False
    text = _scene_text(scene)
    picked = [a for a in (st.get("assets") or [])
              if not _asset_words(a) or any(w in text for w in _asset_words(a))]
    order = {k: i for i, k in enumerate(ASSET_SPECS)}
    picked.sort(key=lambda a: order.get(a.get("kind"), len(order)))   # 角色、设备、场景，顺序稳定
    refs = [str(a.get("id")) for a in picked]
    entries = [_ref_entry(a) for a in picked]
    # ref_images 也要比：资产刚出的底图/六视图没改名，光比 id 会漏掉这份新图
    if refs == list(scene.get("refs") or []) and entries == (scene.get("ref_images") or []):
        return False
    scene["refs"] = refs
    scene["ref_images"] = entries
    return True


def link_all_assets(st, force=False):
    """给所有镜头重算连线，返回改动的镜头数。"""
    return sum(1 for s in (st.get("scenes") or []) if link_scene_assets(s, st, force))


def _entity_id(kind, name):
    """实体 id 由「类型 + 名字」定死：同一篇稿子重拆一次不会又生成一批重复资产。"""
    h = 0
    for ch in (kind + "|" + name):
        h = (h * 131 + ord(ch)) % 1000000007
    return "e%08x" % h


def upsert_entities(st, items):
    """拆分镜时模型通读全文列出的实体 -> 资产池。同类同名的只补描述，不重复加。

    名字必须原样用稿子里的称呼：连线就是按这个名字在镜头文字里找。
    """
    made = 0
    for it in items or []:
        kind = str(it.get("kind") or "").strip()
        name = str(it.get("name") or "").strip()
        if kind not in ASSET_SPECS or not name:
            continue
        a = next((x for x in (st.get("assets") or [])
                  if x.get("kind") == kind and str(x.get("name") or "").strip() == name), None)
        if a is None:
            a = {"id": _entity_id(kind, name), "kind": kind, "name": name, "description": "",
                 "file": None, "views": {}, "candidates": [], "tags": []}
            st.setdefault("assets", []).append(a)
            made += 1
        desc = str(it.get("description") or "").strip()
        if desc and not str(a.get("description") or "").strip():
            a["description"] = desc
    return made


def shot_ref_images(scene, st):
    """镜头的参考图：连到的资产各出图。场景类只出底图 —— 它占的是 <Picture> 那个
    环境位（见 build_graph_h3），六视图塞进去会把环境位撑成一堆机位图；角色与设备出
    六视图，锁多角度外观，没有六视图就退回底图。"""
    out = []
    for a in linked_assets(scene, st):
        kind = a.get("kind") or "character"
        files = [a["file"]] if kind == "scene" else (_asset_view_files(a) or ([a["file"]] if a.get("file") else []))
        for f in files:
            if f:
                out.append({"file": f, "kind": kind, "name": str(a.get("name") or ""),
                            "description": str(a.get("description") or "")})
    return out


# ---- Agent 注册表 ------------------------------------------------------------------
#
# 导演台的每一步是一个 agent：有自己的名字、输入输出契约、模型和进度。前端按这张表
# 渲染流水线，可以单独跑一个，也可以串起来跑。两种走法都过 _run_agent 这一个分发口，
# 不会出现「按钮一条路、一键出片另一条路」的两份逻辑。
#
# 新增一步 = 加一条 _AGENT_DEFS + 一个 _agent_xxx 函数，调用方不用改。

_AGENT_DEFS = (
    ("split",  "拆分镜",         "脚本 / 一句话 → 分镜列表",              "lucide:scissors"),
    ("entity", "拆资产",         "剧本 / 分镜 → 人物 / 场景 / 道具资产",  "lucide:users"),
    ("prompt", "分镜提示词",     "分镜字段 → 该镜的画面描述",             "lucide:pen-line"),
    ("t2i",    "文生图",         "分镜词 → 定帧图（SDXL，一次一张）",     "lucide:image"),
    ("asset",  "图生资产",       "定帧图 → 角色 / 场景六视图资产",        "lucide:boxes"),
    ("frame",  "分镜提示词生帧", "分镜词 → 变化帧 / 关键帧序列",          "lucide:film"),
    ("video",  "帧+资产生视频",  "帧 + 资产参考 → 镜头视频",              "lucide:video"),
)


def _agent_name(aid):
    for k, name, _d, _i in _AGENT_DEFS:
        if k == aid:
            return name
    return aid


# 会调对话模型的 agent：引擎、模型、Key、URL 都能在「Agent 模型」里各配各的。
AGENT_LLM_IDS = ("split", "entity", "prompt", "frame")
# 跑本地 ComfyUI 的 agent：模型就是工作流里那个权重文件。t2i 和 asset 各只装了一个，
# 没有可选项（给个可编辑的框就是骗人）；video 的四个是真实可切的。
AGENT_LOCAL_MODELS = {
    "t2i":   (MODELS_SD["ckpt"], []),
    "asset": (MODELS_KONTEXT["unet"], []),
    "video": ("MiniMax H3", ["MiniMax H3", "Wan2.2 5B", "Wan2.2 14B", "Wan2.2 S2V"]),
}


def agent_defs():
    """给前端的注册表（不带函数引用）。kind 决定「Agent 模型」里那一行能不能改。"""
    out = []
    for k, n, d, i in _AGENT_DEFS:
        if k in AGENT_LLM_IDS:
            out.append({"id": k, "name": n, "desc": d, "icon": i,
                        "kind": "llm", "model": "", "options": []})
            continue
        model, options = AGENT_LOCAL_MODELS[k]
        out.append({"id": k, "name": n, "desc": d, "icon": i,
                    "kind": "comfy", "model": model, "options": options})
    return out


def _fresh_scenes(scenes):
    """把外部传进来的分镜整理成可用的内部形态：补 id、默认值、几何、清产物。"""
    out = []
    for i, s in enumerate(scenes or []):
        s = dict(s)
        s["id"] = "scene%d" % (i + 1)
        # 必须 setdefault：new_scene_defaults() 里有 shot/camera/angle，用 dict.update
        # 会把模型刚拆出来的景别运镜角度整个盖掉，拆一百次都是中景+固定+平视。
        for _k, _v in new_scene_defaults().items():
            s.setdefault(_k, _v)
        s["width"], s["height"] = resolve_geometry(s)
        s["output"] = None
        s["first_frame"] = None
        out.append(s)
    return out


def _backup_before_split(st):
    """拆分镜会整组替换分镜。替换前把「有产物」的那一版另存一份。

    save_state 的 .bak 只有一级：连跑两次拆分镜，第一次的备份就被第二次盖掉，
    原片就找不回来了。只在当前分镜真的有产物（output）时才备份，空项目不浪费磁盘。
    """
    scenes = st.get("scenes") or []
    if not any((s or {}).get("output") for s in scenes):
        return
    try:
        src = _state_path()
        if os.path.isfile(src):
            dst = src + ".before_split_" + time.strftime("%Y%m%d_%H%M%S")
            shutil.copyfile(src, dst)
            _log("拆分镜前已把带产物的 %d 镜备份到 %s" % (len(scenes), os.path.basename(dst)))
    except OSError as e:
        _log("拆分镜前备份失败：%s" % e)


# 「全局设置 → Agent 模型」里能选的对话模型引擎。三个都是 OpenAI 兼容接口或本机
# Ollama，走同一条调用路径，区别只在默认地址和默认模型。
LLM_ENGINE_DEFAULTS = {
    "deepseek": ("https://api.deepseek.com", "deepseek-chat"),
    "qwen":     ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3-vl-plus"),
}
LLM_ENGINES = ("ollama", "deepseek", "qwen")


def _default_llm():
    """没单独给 agent 配模型时用的对话模型：环境变量里的 DeepSeek，否则本机 Ollama。

    返回 (engine, api_key, base_url, model)。engine 是 "ollama" 走本机，其余按 OpenAI
    兼容接口调用。以前只有这一个来源，保留下来给没打开过设置面板的老项目兜底。
    """
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        return ("deepseek", key,
                os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"))
    return ("ollama", "", _OLLAMA_URL, _ollama_chat_model() or "")


def _agent_llm(job, aid):
    """取某个 agent 在「全局设置 → Agent 模型」里配的对话模型。

    整张表随每次运行发过来（和 api_key 一样不落盘在后端）。这一项没配就退回
    _default_llm()。引擎填了模型或地址就用填的，留空才吃默认值。
    """
    cfg = (job.get("agent_models") or {}).get(aid) or {}
    engine = str(cfg.get("engine") or "")
    if engine not in LLM_ENGINES:
        return _default_llm()
    model = str(cfg.get("model") or "").strip()
    url = str(cfg.get("base_url") or "").strip()
    if engine == "ollama":
        return ("ollama", "", url or _OLLAMA_URL, model or _ollama_chat_model() or "")
    default_url, default_model = LLM_ENGINE_DEFAULTS[engine]
    return (engine, str(cfg.get("api_key") or ""), url or default_url, model or default_model)


def _agent_split(job, scenes, st):
    text = (job.get("idea") or "").strip()
    if not text:
        return False, "这个 agent 需要一段脚本或一句话"
    _backup_before_split(st)
    engine, api_key, base_url, model = _agent_llm(job, "split")
    (made, entities), label = ai_split_scenes(text, "ollama" if engine == "ollama" else "deepseek",
                                              api_key, base_url, model)
    if not made:
        return False, "拆分镜失败：" + (label or "模型不可用")
    job["_scenes"] = _fresh_scenes(made)
    added = upsert_entities(st, entities)
    return True, "%s 拆出 %d 镜%s" % (label, len(made), ("、认出 %d 个实体" % added) if added else "")


def fit_states_seconds(states, total):
    """把变化帧的秒数按镜头总时长等比缩放。

    只有前 N-1 帧的 seconds 会变成视频片段，模型自己填的那组数往往对不上镜头时长
    （8 秒的镜头拆两帧，模型可能只给 3 秒），按总时长缩放一遍再存。
    """
    if not total or len(states) < 2:
        return states
    segs = states[:-1]
    cur = sum(s["seconds"] for s in segs) or len(segs)
    for s in segs:
        s["seconds"] = max(3, min(15, round(s["seconds"] * total / cur)))
    return states


def _scenes_text(scenes):
    """把已拆好的分镜倒成一段文本。没给原始剧本时，拆资产就读它。"""
    out = []
    for i, s in enumerate(scenes or []):
        parts = [str(s.get(k) or "").strip() for k in ("prompt", "subject", "environment")]
        out.append("镜头%d：%s" % (i + 1, "；".join(p for p in parts if p)))
    return "\n".join(out)


def _agent_entity(job, scenes, st):
    """拆资产：通读剧本（或已拆好的分镜）把人物、场景、道具列成资产，再按名字连线。"""
    text = (job.get("idea") or "").strip() or _scenes_text(scenes)
    if not text.strip():
        return False, "这个 agent 需要一段剧本，或者先拆出分镜"
    engine, api_key, base_url, model = _agent_llm(job, "entity")
    items, label = ai_split_entities(text, "ollama" if engine == "ollama" else "deepseek",
                                     api_key, base_url, model)
    if items is None:
        return False, "拆资产失败：" + (label or "模型不可用")
    added = upsert_entities(st, items)
    link_all_assets(st)
    return True, "%s 认出 %d 个实体（新增 %d 条，其余是同名补描述）" % (label, len(items), added)


def _agent_prompt(job, scenes, st):
    llm = _agent_llm(job, "prompt")
    n = 0
    for s in scenes:
        if job.get("cancel"):
            return False, "已取消"
        p = compile_shot_prompt(s, llm)
        if p:
            s["prompt"] = p
            n += 1
        _auto_set(job, done=job["done"] + 1, current=s.get("id"))
    return True, "%d 镜写了分镜词" % n


def _agent_t2i(job, scenes, st):
    n = 0
    for s in scenes:
        if job.get("cancel"):
            return False, "已取消"
        files = auto_t2i_frame(s)
        if files:
            # 自动流程不让人挑：直接取第一张。要换图去画布上点候选帧即可
            s["ding_candidates"] = files
            s["first_frame"] = files[0]
            n += 1
        _auto_set(job, done=job["done"] + 1, current=s.get("id"))
    return True, "%d 镜出了定帧图" % n


def _agent_asset(job, scenes, st):
    src = next((s.get("first_frame") for s in scenes if s.get("first_frame")), None)
    if not src:
        return False, "没有定帧图可做资产——先跑「文生图」"
    made = []
    for kind in ("character", "scene"):
        if job.get("cancel"):
            return False, "已取消"
        try:
            a = _asset_of_kind(st, kind)
            asset_views_job(a.get("id"), a.get("kind"), src, 6, 20)
            made.append(kind)
        except Exception as e:
            _log("六视图生成失败 %s: %s" % (kind, e))
    if not made:
        return False, "六视图生成失败（看 comfyui.log）"
    return True, "从定帧图生成了 %s 的六视图资产" % "、".join(made)


def _agent_frame(job, scenes, st):
    n = 0
    for s in scenes:
        if job.get("cancel"):
            return False, "已取消"
        if not (s.get("prompt") or "").strip():
            continue
        engine, api_key, base_url, model = _agent_llm(job, "frame")
        states, label = ai_split_states(s.get("prompt") or "",
                                        "ollama" if engine == "ollama" else "deepseek",
                                        api_key, base_url, model,
                                        level=job.get("granularity") or "fine",
                                        total_sec=int(s.get("seconds") or 5))
        if states:
            # 画布上拆变化帧会按镜头时长缩放，agent 这条以前没做：8 秒的镜头可能只存 3 秒
            s["states"] = fit_states_seconds(states, int(s.get("seconds") or 0))
            n += 1
        _auto_set(job, done=job["done"] + 1, current=s.get("id"))
    return True, "%d 镜拆出了变化帧" % n


def _agent_video(job, scenes, st):
    link_all_assets(st)           # 出片前先按名字连一遍资产，别让老陈的镜头挂上姑娘的设定
    _auto_set(job, step="帧+资产生视频", total=len(scenes), done=0)
    save_state(st)
    cancel_running_gen()          # 上一个生成占着位就先放开
    n = 0
    for s in scenes:
        if job.get("cancel"):
            return False, "已取消"
        if s.get("prompt"):
            s["auto_match"] = False
            refs = shot_ref_images(s, st)
            if refs:
                s["ref_images"] = refs
            else:
                # 没连到资产就退回用定帧图当首帧，链不会因此断
                _log("镜头 %s 没连到资产，视频按首帧 + 纯文字生成" % s.get("id"))
            generate_scene(s)
            n += 1
        _auto_set(job, done=job["done"] + 1, current=s.get("id"))
    return True, "%d 镜提交了视频生成" % n


_AGENT_RUNNERS = {
    "split": _agent_split, "entity": _agent_entity, "prompt": _agent_prompt,
    "t2i": _agent_t2i, "asset": _agent_asset, "frame": _agent_frame, "video": _agent_video,
}


def _run_agent(aid, job, scenes, st):
    """跑一个 agent。scenes 就地更新（split 会整组换成 job['_scenes']）。"""
    runner = _AGENT_RUNNERS.get(aid)
    if not runner:
        return False, "没有这个 agent：" + aid
    if aid != "split":
        _auto_set(job, total=len(scenes), done=0)
    ok, msg = runner(job, scenes, st)
    if aid == "split" and job.get("_scenes") is not None:
        scenes[:] = job["_scenes"]
        job.pop("_scenes", None)
    return ok, msg


def _agent_job(job):
    """后台跑一个 agent。scenes 由 job 带进来，没带就取当前项目的。"""
    try:
        aid = job.get("agent")
        st = load_state()
        scenes = job.get("scenes")
        if scenes is None:
            scenes = st.get("scenes") or []
        scenes = [dict(s) for s in scenes]
        # split 会把整组分镜换掉，所以 total 不能按旧的场景数算
        _auto_set(job, agent=aid, step=_agent_name(aid),
                  total=0 if aid == "split" else len(scenes), done=0)
        ok, msg = _run_agent(aid, job, scenes, st)
        st["scenes"] = scenes
        link_all_assets(st)      # 拆镜之后立刻连线，后面的生帧/出片拿到的就是连好的
        save_state(st)
        if not ok:
            _auto_set(job, step="失败", error=msg)
            return
        _auto_set(job, step="完成", scenes=list(scenes), result=msg)
    except Exception as e:
        _log("agent %s 失败: %s" % (job.get("agent"), e))
        _auto_set(job, step="失败", error="%s: %s" % (type(e).__name__, e))


def _auto_direct_job(job):
    """链式执行：按 job['chain'] 顺序跑多个 agent，一个 stop，剩下的不跑。"""
    try:
        st = load_state()
        scenes = [dict(s) for s in (job.get("scenes") or st.get("scenes") or [])]
        chain = job.get("chain") or ["split", "prompt", "image"]
        done = []
        for aid in chain:
            if job.get("cancel"):
                _auto_set(job, step="已取消", scenes=list(scenes))
                return
            _auto_set(job, agent=aid, step=_agent_name(aid), done=0,
                      total=len(scenes) or 0)
            ok, msg = _run_agent(aid, job, scenes, st)
            if not ok:
                st["scenes"] = scenes
                save_state(st)
                _auto_set(job, step="失败", error="%s：%s" % (_agent_name(aid), msg),
                          scenes=list(scenes))
                return
            done.append(_agent_name(aid))
        st["scenes"] = scenes
        save_state(st)
        _auto_set(job, step="完成", scenes=list(scenes),
                  result="跑完 " + " → ".join(done))
    except Exception as e:
        _log("链式执行失败: %s" % e)
        _auto_set(job, step="失败", error="%s: %s" % (type(e).__name__, e))


def build_graph_h3(scene):



    prompt = scene["prompt"] + H3_AVOID_SUFFIX
    length = scene.get("length") or seconds_to_length(scene.get("seconds", 5))
    width = scene.get("width", 864)
    height = scene.get("height", 480)
    seed = scene.get("seed") or random.randrange(0, 2 ** 31)
    mode_cfg = MODES.get(scene.get("mode") or "标准", MODES["标准"])
    steps = scene.get("steps") or mode_cfg["steps"]
    turbo = bool(mode_cfg.get("turbo"))
    prefix = scene.get("prefix", "video/script2video")
    sc = scene.get("scene") or {}
    # 逐镜引用：前端把 scene.refs（有序的素材 id 列表）解析成这里的 ref_images，
    # 数组下标即 <Subject N> 的编号 —— 和 LibTV「连线顺序即引用顺序」是同一套语义。
    # scene 类素材不占 Subject 编号：它挂 <Picture K> 描述环境，这是 _build_ref_prompt
    # 既有的契约，改了会打乱所有历史结果里的提示词结构。
    ref_entries = scene.get("ref_images") or []
    refs = [_stage_asset_ref(e["file"]) for e in ref_entries
            if e.get("kind") != "scene" and e.get("file")]
    refs += (scene.get("guides") or []) + (scene.get("auto_guides") or [])
    scene_ref = _stage_asset_ref(next((e["file"] for e in ref_entries
                                       if e.get("kind") == "scene" and e.get("file")), ""))
    need_audio = bool(scene.get("need_audio", True))

    graph = {
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": MODELS["clip"], "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["vae_video"]}},
    }
    if need_audio or refs or scene_ref:
        graph["4"] = {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["vae_audio"]}}
    if refs or scene_ref:
        graph["1"] = {"class_type": "UNETLoader", "inputs": {"unet_name": resolve_ref2va_unet(), "weight_dtype": "default"}}
        graph["15"] = {"class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch", "inputs": {"model": ["1", 0]}}
        graph["16"] = {"class_type": "MiniMaxH3SigmaShift", "inputs": {"model": ["15", 0], "shift_video": 12.0, "shift_audio": 3.0}}
        h3_5 = {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
            "clip": ["2", 0], "vae": ["3", 0], "audio_vae": ["4", 0],
            "prompt": _build_ref_prompt(prompt, len(refs), scene_ref),
            "width": width, "height": height, "length": length, "ref_image_size": "match"}}
        graph["5"] = h3_5
        nid = 40
        if refs:
            cutout = _bg_removal_available()
            if cutout:
                graph["30"] = {"class_type": "LoadBackgroundRemovalModel", "inputs": {"bg_removal_name": BG_REMOVAL_MODEL}}
            else:
                _log("BiRefNet 权重不可用，参考图跳过抠主体直接用原图")
            for i, g in enumerate(refs):
                lnid = str(nid)
                nid += 6 if cutout else 1
                graph[lnid] = {"class_type": "LoadImage", "inputs": {"image": g}}
                ref_src = [lnid, 0]
                if cutout:
                    mnid, szid, smid, mtid, cnid = [str(nid - 5 + k) for k in range(5)]
                    graph[mnid] = {"class_type": "RemoveBackground", "inputs": {"bg_removal_model": ["30", 0], "image": [lnid, 0]}}
                    graph[szid] = {"class_type": "GetImageSize", "inputs": {"image": [lnid, 0]}}
                    graph[smid] = {"class_type": "SolidMask", "inputs": {"value": 1.0, "width": [szid, 0], "height": [szid, 1]}}
                    graph[mtid] = {"class_type": "MaskToImage", "inputs": {"mask": [smid, 0]}}
                    graph[cnid] = {"class_type": "ImageCompositeMasked", "inputs": {"destination": [mtid, 0], "source": [lnid, 0], "x": 0, "y": 0, "resize_source": False, "mask": [mnid, 0]}}
                    ref_src = [cnid, 0]
                # autogrow 子输入的真实 id 是 父id.子名，且从 0 开始（见 comfy_api Autogrow）
                graph["5"]["inputs"]["ref_images.ref_image_%d" % i] = ref_src
        if scene_ref:
            lnid = str(nid)
            graph[lnid] = {"class_type": "LoadImage", "inputs": {"image": scene_ref}}
            graph["5"]["inputs"]["ref_images.ref_image_%d" % len(refs)] = [lnid, 0]
        model_src = ["16", 0]
        steps = max(12, int(steps))
    else:
        graph["1"] = {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS["unet"], "weight_dtype": "default"}}
        graph["5"] = {"class_type": "MiniMaxH3ImageToVideo", "inputs": {"clip": ["2", 0], "vae": ["3", 0], "prompt": prompt, "width": width, "height": height, "length": length}}
        graph["15"] = {"class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch", "inputs": {"model": ["16" if turbo else "1", 0]}}
        if turbo:
            graph["16"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": TURBO_LORA, "strength_model": 1.0}}
        first_frame = scene.get("first_frame")
        if first_frame:
            graph["17"] = {"class_type": "LoadImage", "inputs": {"image": first_frame}}
            graph["5"]["inputs"]["first_frame"] = ["17", 0]
        last_frame = scene.get("last_frame")
        if last_frame:
            graph["19"] = {"class_type": "LoadImage", "inputs": {"image": last_frame}}
            graph["5"]["inputs"]["last_frame"] = ["19", 0]
        model_src = ["15", 0]

    graph["6"] = {"class_type": "BasicGuider", "inputs": {"model": model_src, "conditioning": ["5", 0]}}
    graph["7"] = {"class_type": "BasicScheduler", "inputs": {"model": model_src, "scheduler": "simple", "steps": steps, "denoise": 1.0}}
    graph["8"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}}
    graph["9"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}}
    graph["10"] = {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["9", 0], "guider": ["6", 0], "sampler": ["8", 0], "sigmas": ["7", 0], "latent_image": ["5", 1]}}
    graph["11"] = {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["3", 0]}}
    video_inputs = {"images": ["11", 0], "fps": 24.0}
    if need_audio:
        graph["12"] = {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["10", 0], "vae": ["4", 0]}}
        video_inputs["audio"] = ["12", 0]
    graph["13"] = {"class_type": "CreateVideo", "inputs": video_inputs}
    graph["14"] = {"class_type": "SaveVideo", "inputs": {"video": ["13", 0], "filename_prefix": prefix, "format": "auto"}}
    voice = scene.get("voice")
    if voice and need_audio:
        graph["18"] = {"class_type": "LoadAudio", "inputs": {"audio": voice}}
        graph["13"]["inputs"]["audio"] = ["18", 0]
    return graph


# --- 分镜字段 -> 镜头描述 -------------------------------------------------------------
#
# 景别/运镜/角度/主体/动作/环境/光线/音效 是编剧填得动的字段；模型要读的是一个镜头的
# 一句话描述。这一层就是把前者变成后者，走的是 _translate_prompt 同一套引擎顺序：
# 本机 Ollama 对话模型优先，其次 DeepSeek，都没有就返回 None 让前端报错，不静默降级。
#
# 系统提示词刻意写得极短。本地小模型（qwen3:8b 一类）实测：四句话以内正常出稿；多加
# 一句就转成长篇思考、把输出预算整段吃掉、返回空串，而加大预算只会让前言更长。所以
# 本该写成规则的东西一律放到代码里——编号、时间码、字段名本来就不该由模型写。
#
# 一个必须遵守的接口约束：compose_prompt() 已经在末尾自动追加「景别：X。运镜：Y。」，
# 所以这里写出的描述里不能再出现它们，否则最终提示词会说两遍。

SHOT_COMPILE_SYS = (
    "你是影视分镜编剧。把镜头字段写成一段画面描述，只输出这段描述本身："
    "不要字段名、不要标题、不要镜头编号、不要时间码、不要 markdown。"
    "现在时，两到四句，写主体在做什么、在哪里、光线如何。"
)

# compose_prompt() 会自己追加这两个，所以只作为写作依据交给模型，不让它写进正文。
_SHOT_CONTEXT_FIELDS = (("景别", "shot"), ("运镜", "camera"))
_SHOT_BODY_FIELDS = (("角度", "angle"), ("主体", "subject"), ("动作", "action"),
                     ("环境", "environment"), ("光线", "lighting"), ("音效", "sfx"))


def build_shot_brief(scene):
    """字段组成的写作提纲。只列填了的字段——空字段留在提纲里等于邀请模型编一个。"""
    lines = []
    for label, key in _SHOT_CONTEXT_FIELDS:
        v = str(scene.get(key) or "").strip()
        if v:
            lines.append("%s：%s（正文里不要重复，系统会自动追加）" % (label, v))
    for label, key in _SHOT_BODY_FIELDS:
        v = str(scene.get(key) or "").strip()
        if v:
            lines.append("%s：%s" % (label, v))
    return "\n".join(lines)


def _strip_shot_scaffold(text):
    """去掉小模型明知故犯加回来的壳：字段名、标题、代码围栏、首尾引号。"""
    t = (text or "").strip()
    if not t:
        return ""
    fence = chr(96) * 3
    if t.startswith(fence):
        t = t.split("\n", 1)[-1].rsplit(fence, 1)[0].strip()
    heads = ("镜头描述", "画面描述", "描述", "输出", "回答", "答案", "正文")
    kept = []
    for line in t.splitlines():
        s = line.strip()
        if not s:
            continue
        head = s.split("：", 1)[0].split(":", 1)[0].strip()
        if head in heads and ("：" in s or ":" in s):
            s = s.split("：", 1)[-1].split(":", 1)[-1].strip()
            if not s:
                continue
        kept.append(s)
    return "".join(kept).strip().strip("“”\"'").strip()


def compile_shot_prompt(scene, llm=None):
    """镜头字段 -> 画面描述。

    llm 是 (engine, api_key, base_url, model)，来自「全局设置 → Agent 模型」里「分镜提示词」
    那一行；不传就退回 _default_llm()。云端先试，失败或返回空再用本机 Ollama 兜底；
    两边都不可用返回 None，让调用方报错而不是静默降级。
    """
    brief = build_shot_brief(scene)
    if not brief:
        return None
    engine, api_key, base_url, model = llm or _default_llm()
    if engine != "ollama" and api_key:
        try:
            out = _llm_chat(base_url, api_key, model,
                            [{"role": "system", "content": SHOT_COMPILE_SYS},
                             {"role": "user", "content": brief}],
                            max_tokens=800, timeout=180)
            out = _strip_shot_scaffold(out)
            if out:
                return out
        except Exception:
            pass
    # 选了 ollama 就用用户指定的模型；选了云端但云端没出东西，退回本机默认模型
    model = model if engine == "ollama" else _ollama_chat_model()
    if model:
        try:
            body = {"model": model, "stream": False, "think": False,
                    "messages": [{"role": "system", "content": SHOT_COMPILE_SYS},
                                 {"role": "user", "content": brief}],
                    # num_predict 必须给足：小模型先写一大段思考再动笔，预算不够就整段被
                    # 吃掉，返回空串。1500 是实测能稳定拿到答案的下限。
                    "options": {"temperature": 0.6, "num_ctx": 4096, "num_predict": 1500}}
            req = urllib.request.Request(
                _OLLAMA_URL.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                content = _strip_shot_scaffold(json.load(r)["message"]["content"])
            if content:
                return content
        except Exception:
            pass
    return None


def _translate_prompt(text, sys=None):
    """中文描述 -> SDXL 英文提示词。

    引擎顺序和 compile_shot_prompt 一致：配了 DEEPSEEK_API_KEY 就先走云端，本机 Ollama
    退成兜底。云端调用不可用时不报错，由本地接手 —— 文生图这一步不该因为没网就停。
    """
    sys = sys or ("你是影视分镜翻译。把用户的中文视频分镜提示词翻译成适合 SDXL 文生图的英文提示词："
                  "保留景别、构图、光线、人物与动作细节，去掉运镜与风格套话，末尾统一加 "
                  "'cinematic film still, photorealistic, highly detailed, 8k, film grain'。只输出英文提示词，不要解释。")
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if api_key:
        try:
            out = _llm_chat(os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"), api_key,
                            os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
                            [{"role": "system", "content": sys}, {"role": "user", "content": text}],
                            max_tokens=500, timeout=120)
            out = (out or "").strip()
            if out:
                return out
        except Exception:
            pass
    model = _ollama_chat_model()
    if model:
        try:
            body = {"model": model, "stream": False, "think": False,
                    "messages": [{"role": "system", "content": sys}, {"role": "user", "content": text}],
                    "options": {"temperature": 0.3, "num_ctx": 4096}}
            req = urllib.request.Request(
                _OLLAMA_URL.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                content = json.load(r)["message"]["content"].strip()
            if content:
                return content
        except Exception:
            pass
    return None


ASSET_SPECS = {
    "character": {
        "label": "角色设定图", "aspect": "9:16", "width": 768, "height": 1344,
        "sys": "你是影视人物设定师。把用户给的角色描述写成 SDXL 人物设定图英文提示词。"
               "必须先用 male 或 female 明确写出人物性别，并写出年龄段（描述里没直说时按姓名、称谓、身份合理判断，"
               "例如「老陈」「面馆老板」按中年男性处理）；再写脸型、发型发色、体型、服装与配饰的材质和颜色。"
               "只输出英文提示词，不要解释。",
        "suffix": "character design reference, single person alone, full body front view, head to toe, "
                  "neutral standing pose, plain seamless grey backdrop, soft even studio lighting, "
                  "sharp focus, photorealistic, detailed face hair and clothing",
    },
    "equipment": {
        "label": "设备/产品设定图", "aspect": "1:1", "width": 1024, "height": 1024,
        "sys": "你是工业视觉设计师。把用户给的设备或产品描述写成 SDXL 英文提示词："
               "写清外形、结构、材质、颜色、比例，以及能认出这台设备的标志性细节"
               "（面板、接口、铭牌、管路、按钮、底座、警示标识）。只输出英文提示词，不要解释。",
        "suffix": "industrial equipment product shot, single object alone, whole object in frame, "
                  "centered, plain seamless grey backdrop, soft even studio lighting, "
                  "sharp focus, photorealistic, high detail",
    },
    "scene": {
        "label": "场景设定图", "aspect": "16:9", "width": 1344, "height": 768,
        "sys": "你是影视美术指导。把用户给的场景描述写成 SDXL 场景设定图英文提示词："
               "交代时间、天气、空间结构、材质与光源。只输出英文提示词，不要解释。",
        "suffix": "empty location establishing shot, no people, no figures, wide angle, "
                  "clear foreground midground background layers, photorealistic, cinematic lighting",
    },
}


def build_graph_t2i(scene, prompt_en, batch, seed, prefix):
    """SDXL 文生图定帧：按镜头提示词出一批静帧，缩放成镜头分辨率后保存。"""
    cw, ch = SD_CANVAS.get(scene.get("aspect") or "16:9", SD_CANVAS["16:9"])
    w, h = scene.get("width", 864), scene.get("height", 480)
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": MODELS_SD["ckpt"]}},
        "2": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS_SD["vae"]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt_en, "clip": ["1", 1]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": DING_NEGATIVE, "clip": ["1", 1]}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": cw, "height": ch, "batch_size": batch}},
        "6": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "seed": seed, "steps": 28, "cfg": 4.5,
            "sampler_name": "dpmpp_2m", "scheduler": "karras",
            "positive": ["3", 0], "negative": ["4", 0], "latent_image": ["5", 0], "denoise": 1.0}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["2", 0]}},
        "8": {"class_type": "ImageScale", "inputs": {"image": ["7", 0], "upscale_method": "lanczos", "width": w, "height": h, "crop": "center"}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": prefix}},
    }


def keyframe_references(scene, st, prev=""):
    """故事版关键帧的参考图列表（首张当编辑源，决定画幅）。

    每一帧都把连线上的资产重新拉一遍（角色／设备／场景），谁的图就锁谁的样子。
    首张是编辑源：
      · 有 prev（同一镜上一帧的关键帧）时用它 —— 相邻两帧本来就是同一个人的连续状态，
        从上一帧改，脸、衣服和背景都留在原地；每帧都从空景重画一遍，人脸每次都会重新
        长一个（实测）；
      · 没有 prev（第一帧、整镜关键帧）时用场景空景 —— 它本来就是按镜头比例出的，
        画幅与成片一致。
    其余参考只锁外观、不决定画幅：设备与角色出六视图的前／后两张（背对镜头的戏才有依据），
    没有六视图就退回底图。
    参考资料太多会互相稀释，最多取 4 张；一张都没有则退化成 Flux 文生图。
    """
    linked = linked_assets(scene, st)
    plate = next((a["file"] for a in linked if a.get("kind") == "scene" and a.get("file")), "")
    refs = []
    if prev:
        refs.append(prev)                       # 编辑源：上一帧
    if plate and plate not in refs:
        refs.append(plate)                      # 场景资产照样拉：背景的身份由它兜着

    def add_views(a, picks):
        views = a.get("views") or {}
        hit = False
        for key in picks:
            v = views.get(key)
            if v and v not in refs:
                refs.append(v)
                hit = True
        if not hit and a.get("file") and a["file"] not in refs:
            refs.append(a["file"])

    for a in linked:
        if a.get("kind") == "equipment":        # 工业场景里，最先要锁住的是设备
            add_views(a, ("front", "back"))
    for a in linked:
        if a.get("kind") == "character":
            add_views(a, ("front", "back"))
    return refs[:4]


# 资产六视图：以输入图为编辑源，只改机位，主体的一切特征保持不变
ASSET_VIEWS = (("front", "前"), ("back", "后"), ("left", "左"), ("right", "右"), ("top", "上"), ("bottom", "下"))

# 机位说明与保持规则按资产类型分开写。三种资产以前共用一句「保持脸部/发型/服装/全身不变」，
# 场景那条的「后视图」就被模型当成人物设定图，给面馆出了一个背对镜头站在灰底前的男人：
# 对一间铺子来说「拍到它的背面」本来就说不通，模型只能照人物那套编。场景要的是同一个
# 空间的反打，而且画面里不许有人。
ASSET_VIEW_ANGLE = {
    "character": {
        "front": "Show the same person from the front, facing the camera straight on.",
        "back": "Show the same person from behind — we see their back, the camera is behind them.",
        "left": "Show the same person from their left side, a full side profile.",
        "right": "Show the same person from their right side, a full side profile.",
        "top": "Show the same person from directly above, looking straight down at them.",
        "bottom": "Show the same person from directly below, looking straight up at them.",
    },
    "equipment": {
        "front": "Show the same object from the front, facing the camera straight on.",
        "back": "Show the same object from behind — we see its back side.",
        "left": "Show the same object from its left side, a full side profile.",
        "right": "Show the same object from its right side, a full side profile.",
        "top": "Show the same object from directly above, looking straight down at it.",
        "bottom": "Show the same object from directly below, looking straight up at it.",
    },
    "scene": {
        "front": "Show the same building from the front, facing the camera straight on.",
        "back": "Show the same building from the back: the camera has moved around behind it, so we see the rear "
                "side of the same structure — same roof edge, same awning, same warm lights.",
        "left": "Show the same building from its left side, a full side profile.",
        "right": "Show the same building from its right side, a full side profile.",
        "top": "Show the same building from directly above, looking straight down at it.",
        "bottom": "Show the same building from directly below, looking straight up at it.",
    },
}
ASSET_VIEW_RULE = {
    "character": ("Keep this person's identity, face, hairstyle, clothing, colours, materials and proportions exactly "
                  "the same. Only the camera angle changes. Plain neutral seamless background, single person alone, "
                  "full body in frame, photorealistic, sharp focus."),
    "equipment": ("Keep this object's shape, structure, materials, colours, panel details and proportions exactly the "
                  "same. Only the camera angle changes. Single object alone, whole object in frame, centred, "
                  "plain seamless grey backdrop, soft even studio lighting, photorealistic, sharp focus."),
    "scene": ("Keep this exact building unchanged: same structure and shape, same awning, same counter, same "
              "materials and colours, same warm interior lights. Only the camera angle changes. The building alone, "
              "whole building in frame, on a plain seamless neutral grey backdrop, no people and no figures, "
              "photorealistic, sharp focus."),
}

ASSET_VIEW_STATUS = {"running": False, "kind": "", "stage": "", "done": 0, "total": 0,
                     "current": "", "error": None, "views": {}}


def build_graph_kontext_edit(source_rel, instruction, seed, steps, prefix):
    """Flux Kontext 按指令编辑一张图：以输入图为编辑源，不强制输出画幅（保留原图比例）。

    资产六视图（换机位）和「换物体」（改画面里的某个东西）都走这里。
    """
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS_KONTEXT["unet"], "weight_dtype": "fp8_e4m3fn"}},
        "2": {"class_type": "DualCLIPLoader", "inputs": {
            "clip_name1": MODELS_KONTEXT["clip_l"], "clip_name2": MODELS_KONTEXT["t5"], "type": "flux"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS_KONTEXT["vae"]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": instruction, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}},
        "6": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["4", 0], "guidance": 2.5}},
        "11": {"class_type": "LoadImage", "inputs": {"image": source_rel}},
        "12": {"class_type": "FluxKontextImageScale", "inputs": {"image": ["11", 0]}},
        "13": {"class_type": "VAEEncode", "inputs": {"pixels": ["12", 0], "vae": ["3", 0]}},
        "15": {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["6", 0], "latent": ["13", 0]}},
        "7": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "seed": seed, "steps": steps, "cfg": 1.0,
            "sampler_name": "euler", "scheduler": "simple",
            "positive": ["15", 0], "negative": ["5", 0], "latent_image": ["13", 0], "denoise": 1.0}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": prefix}},
    }
    return g


def asset_views_job(asset_id, kind, source_rel, want, steps):
    """按六视图逐张出图，每张完成就写回池子里那条资产，中途失败保留已生成的。"""
    status = ASSET_VIEW_STATUS
    slug = re.sub(r"[^0-9a-zA-Z_-]", "", str(asset_id)) or "asset"
    kind = kind if kind in ASSET_VIEW_RULE else "character"
    angles, rule = ASSET_VIEW_ANGLE[kind], ASSET_VIEW_RULE[kind]
    try:
        status.update(running=True, asset_id=str(asset_id), kind=kind, stage="views", done=0, total=len(want),
                      current="", error=None, views={})
        for key, label in ASSET_VIEWS:
            if key not in want:
                continue
            status["current"] = "%s视图" % label
            stamp = time.strftime("%H%M%S")
            graph = build_graph_kontext_edit(source_rel, angles[key] + " " + rule,
                                           random.randrange(0, 2 ** 31), steps,
                                           "frames/view_%s_%s_%s" % (slug, key, stamp))
            save_workflow_files(graph, "资产六视图")
            busy, job = _gen_acquire("六视图-%s-%s" % (asset_id, key), 3600)
            if busy:
                raise RuntimeError("ComfyUI 正忙：%s" % busy)
            try:
                r = _submit_prompt(graph, timeout=1800, images_all=True)
            finally:
                _gen_release(job)
            if not r.get("ok"):
                raise RuntimeError("%s视图生成失败：%s" % (label, r.get("error")))
            src = os.path.join(_COMFY_ROOT, "output", (r["files"][0]).replace("/", os.sep))
            frames_dir = os.path.join(INPUT_DIR, "frames")
            os.makedirs(frames_dir, exist_ok=True)
            name = "view_%s_%s.png" % (slug, key)
            shutil.move(src, os.path.join(frames_dir, name))
            fresh = load_state()
            a = _find_asset(fresh, asset_id)
            if a is None:
                raise RuntimeError("资产 %s 已被删除" % asset_id)
            a.setdefault("views", {})[key] = "frames/" + name
            link_all_assets(fresh)      # 新出的视图立刻进连线的参考图
            save_state(fresh)
            status["views"][key] = "frames/" + name
            status["done"] += 1
        status["stage"] = "done"
    except Exception as e:
        status["stage"] = "error"
        status["error"] = str(e)
    finally:
        status["running"] = False


def build_graph_vace_inpaint(frames_dir, target_text, prompt, width, height, length,
                             seed, steps, mask_expand, threshold, prefix):
    """Wan2.1-VACE 掩膜视频修补：SAM3 按 target_text 圈出要改的地方，VACE 只在掩膜里重画。

    frames_dir 必须是**绝对路径**（LoadImagesFromFolderKJ 不认 input 相对路径），
    里面是按 16fps 抽好的图片序列（VACE 是 16fps 模型，喂 24fps 会变慢动作）。
    接线照官方 video_wan_vace_inpainting 模板：UNETLoader → ModelSamplingSD3(shift=5) → KSampler(uni_pc, cfg=1)。
    """
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS_VACE["unet"], "weight_dtype": "fp8_e4m3fn_fast"}},
        "2": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": VACE_SHIFT}},
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": MODELS_VACE["clip"], "type": "wan", "device": "default"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS_VACE["vae"]}},
        # image_load_cap/start_index/include_subfolders 在 object_info 里标着可选，
        # 但这个节点的 Python 签名把它们当必填位置参数，不传就 TypeError
        "5": {"class_type": "LoadImagesFromFolderKJ", "inputs": {
            "folder": frames_dir, "width": width, "height": height, "keep_aspect_ratio": "stretch",
            "image_load_cap": 0, "start_index": 0, "include_subfolders": False}},
        "6": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": MODELS_VACE["sam3"]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": target_text, "clip": ["6", 1]}},
        "8": {"class_type": "SAM3_Detect", "inputs": {
            "model": ["6", 0], "image": ["5", 0], "conditioning": ["7", 0],
            "threshold": threshold, "refine_iterations": 2, "individual_masks": False}},
        "9": {"class_type": "GrowMask", "inputs": {"mask": ["8", 0], "expand": mask_expand, "tapered_corners": True}},
        "10": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["3", 0]}},
        "11": {"class_type": "CLIPTextEncode", "inputs": {"text": WAN_DEFAULT_NEGATIVE, "clip": ["3", 0]}},
        "12": {"class_type": "WanVaceToVideo", "inputs": {
            "positive": ["10", 0], "negative": ["11", 0], "vae": ["4", 0],
            "width": width, "height": height, "length": length, "batch_size": 1, "strength": 1.0,
            "control_video": ["5", 0], "control_masks": ["9", 0]}},
        "13": {"class_type": "KSampler", "inputs": {
            "model": ["2", 0], "seed": seed, "steps": steps, "cfg": 1.0,
            "sampler_name": "uni_pc", "scheduler": "simple",
            "positive": ["12", 0], "negative": ["12", 1], "latent_image": ["12", 2], "denoise": 1.0}},
        "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["4", 0]}},
        "15": {"class_type": "CreateVideo", "inputs": {"images": ["14", 0], "fps": float(VACE_FPS)}},
        "16": {"class_type": "SaveVideo", "inputs": {"video": ["15", 0], "filename_prefix": prefix,
                                                     "format": "auto", "codec": "auto"}},
    }
    return g


def prep_vace_frames(clip_abs, tag, width, height):
    """按 VACE 的 16fps 和目标分辨率把成片抽成图片序列，返回 (绝对目录, 帧数)。

    必须给 LoadImagesFromFolderKJ 绝对路径：它只把相对路径拼到 KJNodes 自己的 --base-directory 上，
    没设那个参数时就按进程工作目录找，传 "frames/xxx" 会直接 FileNotFoundError。
    """
    name = "vace_" + tag
    out_dir = os.path.join(INPUT_DIR, "frames", name)
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    r = _ffmpeg_run([_ffmpeg(), "-y", "-i", clip_abs,
                     "-vf", "fps=%d,scale=%d:%d" % (VACE_FPS, width, height),
                     os.path.join(out_dir, "%05d.png")])
    if r.returncode != 0:
        raise RuntimeError("抽帧失败：" + (r.stderr or "")[-300:])
    n = len([f for f in os.listdir(out_dir) if f.lower().endswith(".png")])
    if not n:
        raise RuntimeError("抽帧失败：没有输出帧")
    return out_dir, n


def build_graph_keyframe(scene, prompt_en, refs, seed, steps, batch, prefix):
    """Flux.1 Kontext 故事版关键帧：资产图 + 分镜提示词 → 该镜静帧。

    refs 首张是编辑源（决定画幅与编辑起点），其余用 ReferenceLatent 追加为附加参考。
    无参考图时退化成 Flux 文生图，直接出镜头分辨率的空 latent。
    """
    w, h = scene.get("width", 864), scene.get("height", 480)
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS_KONTEXT["unet"], "weight_dtype": "fp8_e4m3fn"}},
        "2": {"class_type": "DualCLIPLoader", "inputs": {
            "clip_name1": MODELS_KONTEXT["clip_l"], "clip_name2": MODELS_KONTEXT["t5"], "type": "flux"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS_KONTEXT["vae"]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt_en, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}},
        "6": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["4", 0], "guidance": 2.5}},
    }
    cond, source, n = "6", None, 20
    for ref in refs:
        load, scal, enc, rl = str(n), str(n + 1), str(n + 2), str(n + 3)
        n += 4
        g[load] = {"class_type": "LoadImage", "inputs": {"image": ref}}
        g[scal] = {"class_type": "FluxKontextImageScale", "inputs": {"image": [load, 0]}}
        g[enc] = {"class_type": "VAEEncode", "inputs": {"pixels": [scal, 0], "vae": ["3", 0]}}
        g[rl] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": [cond, 0], "latent": [enc, 0]}}
        cond = rl
        source = source or enc
    if source:
        g["18"] = {"class_type": "RepeatLatentBatch", "inputs": {"samples": [source, 0], "amount": batch}}
        latent = ["18", 0]
    else:
        g["18"] = {"class_type": "EmptySD3LatentImage", "inputs": {"width": w, "height": h, "batch_size": batch}}
        latent = ["18", 0]
    g["7"] = {"class_type": "KSampler", "inputs": {
        "model": ["1", 0], "seed": seed, "steps": steps, "cfg": 1.0,
        "sampler_name": "euler", "scheduler": "simple",
        "positive": [cond, 0], "negative": ["5", 0], "latent_image": latent, "denoise": 1.0}}
    g["8"] = {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}}
    g["9"] = {"class_type": "ImageScale", "inputs": {"image": ["8", 0], "upscale_method": "lanczos",
                                                     "width": w, "height": h, "crop": "center"}}
    g["10"] = {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": prefix}}
    return g


# ---- 工作流导出：把 API 图转成 UI 图形并同步到 ComfyUI Workflows 列表 ----
UI_WORKFLOWS_DIR = os.path.join(_COMFY_ROOT, "user", "default", "workflows")
_object_info_cache = {}
_WIDGET_SCALARS = {"INT", "FLOAT", "STRING", "BOOLEAN"}
_MISSING = object()


def _get_object_info():
    if not _object_info_cache:
        try:
            _object_info_cache.update(_get(COMFY_URL.rstrip("/") + "/object_info"))
        except Exception:
            pass
    return _object_info_cache


def _is_link_token(x):
    if not isinstance(x, str) or not x:
        return False
    return x == x.upper() and all(ch.isalnum() or ch == "_" for ch in x)


def _is_widget(tspec):
    if isinstance(tspec, str):
        return tspec in _WIDGET_SCALARS or tspec == "COMBO"
    if isinstance(tspec, (list, tuple)):
        if not tspec:
            return True
        return any(not _is_link_token(t) for t in tspec)
    return True


def _input_specs(info, pin=None):
    inp = info.get("input") or {}
    for section in ("required", "optional"):
        for name, spec in (inp.get(section) or {}).items():
            if isinstance(spec, (list, tuple)) and len(spec) >= 1:
                tspec = spec[0]
                opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            else:
                tspec, opts = spec, {}
            if tspec == "COMFY_AUTOGROW_V3":
                tmpl = (opts or {}).get("template") or {}
                prefix = tmpl.get("prefix", "")
                inner = (tmpl.get("input") or {}).get("required") or {}
                itype = "IMAGE"
                if inner:
                    raw = next(iter(inner.values()), None)
                    if isinstance(raw, (list, tuple)) and raw:
                        raw = raw[0]
                    itype = str(raw or "IMAGE").strip() or "IMAGE"
                if pin is not None:
                    keys = sorted((k for k in pin if k.startswith(prefix) and k[len(prefix):].isdigit()), key=lambda k: (len(k), k))
                    for k in keys:
                        yield k, itype, {}, section == "optional"
                else:
                    yield name, tspec, opts, section == "optional"
                continue
            yield name, tspec, opts, section == "optional"


def _link_type_str(tspec):
    if isinstance(tspec, str):
        return tspec
    if isinstance(tspec, (list, tuple)) and tspec:
        return ",".join(str(x) for x in tspec)
    return "ANY"


def _widget_default(tspec, opts):
    if isinstance(opts, dict) and opts.get("default") is not None:
        return opts["default"]
    if isinstance(tspec, str):
        if tspec == "COMBO" and isinstance(opts, dict) and isinstance(opts.get("options"), (list, tuple)) and opts["options"]:
            return opts["options"][0]
        return {"INT": 0, "FLOAT": 0.0, "STRING": "", "BOOLEAN": False}.get(tspec)
    if isinstance(tspec, (list, tuple)) and tspec:
        return tspec[0]
    return None


def _classify(tspec, pv):
    if isinstance(pv, list) and len(pv) == 2 and isinstance(pv[0], str) and isinstance(pv[1], int):
        return "link"
    return "widget" if _is_widget(tspec) else "link"


def graph_to_ui(graph):
    oi = _get_object_info()
    ids = list(graph.keys())
    idmap = {sid: i + 1 for i, sid in enumerate(ids)}
    nodes = []
    for order, sid in enumerate(ids):
        pn = graph[sid]
        ctype = pn.get("class_type")
        info = oi.get(ctype) or {}
        pin = pn.get("inputs") or {}
        inputs_arr, widgets_values = [], []
        for name, tspec, opts, _opt in _input_specs(info, pin):
            pv = pin.get(name, _MISSING)
            if _classify(tspec, pv) == "link":
                inputs_arr.append({"name": name, "type": _link_type_str(tspec), "link": None})
            else:
                widgets_values.append(pv if pv is not _MISSING else _widget_default(tspec, opts))
        out_names = info.get("output_name") or info.get("output") or []
        outputs = []
        for idx, ot in enumerate(info.get("output") or []):
            on = out_names[idx] if idx < len(out_names) else ot
            outputs.append({"name": on, "type": ot, "links": []})
        nodes.append({
            "id": idmap[sid], "type": ctype, "pos": [0, 0], "size": [360, 100], "flags": {},
            "order": order, "mode": 0, "inputs": inputs_arr, "outputs": outputs,
            "properties": {"Node name for S&R": ctype}, "widgets_values": widgets_values,
        })
    links = []
    for i, sid in enumerate(ids):
        pn = graph[sid]
        info = oi.get(pn.get("class_type")) or {}
        pin = pn.get("inputs") or {}
        node = nodes[i]
        input_index = {inp["name"]: j for j, inp in enumerate(node["inputs"])}
        for name, _tspec, _opts, _opt in _input_specs(info, pin):
            pv = pin.get(name, _MISSING)
            if not (isinstance(pv, list) and len(pv) == 2 and isinstance(pv[0], str) and isinstance(pv[1], int)):
                continue
            from_str, from_slot = pv[0], pv[1]
            if from_str not in idmap or name not in input_index:
                continue
            to_index = input_index[name]
            lid = len(links) + 1
            links.append([lid, idmap[from_str], from_slot, idmap[sid], to_index, None])
            node["inputs"][to_index]["link"] = lid
            src = nodes[idmap[from_str] - 1]
            if 0 <= from_slot < len(src["outputs"]):
                src["outputs"][from_slot]["links"].append(lid)
    n = len(nodes)
    indeg = [0] * n
    adj = [[] for _ in range(n)]
    for l in links:
        f, t = l[1] - 1, l[3] - 1
        adj[f].append(t)
        indeg[t] += 1
    depth = [0] * n
    q = deque(i for i in range(n) if indeg[i] == 0)
    while q:
        u = q.popleft()
        for v in adj[u]:
            if depth[v] < depth[u] + 1:
                depth[v] = depth[u] + 1
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)
    col = {}
    for i in range(n):
        c = col.get(depth[i], 0)
        col[depth[i]] = c + 1
        nodes[i]["pos"] = [40 + depth[i] * 460, 40 + c * 150]
    return {
        "id": str(uuid.uuid4()), "revision": 0, "last_node_id": len(nodes), "last_link_id": len(links),
        "nodes": nodes, "links": links, "groups": [], "config": {}, "extra": {}, "version": 0.4,
    }


def save_workflow_files(graph, name):
    try:
        os.makedirs(UI_WORKFLOWS_DIR, exist_ok=True)
        # 这个名字会直接当文件名用，必须挡掉路径分隔符与非法字符
        # （「设备/产品设定图」里的斜杠曾被当成目录，整个保存失败）
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "workflow"
        ui_path = os.path.join(UI_WORKFLOWS_DIR, safe + ".json")
        json.dump(graph_to_ui(graph), open(ui_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        json.dump(graph, open(os.path.join(UI_WORKFLOWS_DIR, safe + "_api.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("workflow saved:", ui_path)
    except Exception as e:
        print("workflow save failed:", e)


try:
    import requests
    _HTTP = requests.Session()
except ImportError:
    _HTTP = None


def _comfy_error_text(status, reason, body):
    """ComfyUI 校验不过时回 400 + node_errors，把节点/输入名翻出来，别只给个 HTTP 状态。"""
    try:
        data = json.loads(body)
    except Exception:
        return "%s %s: %s" % (status, reason, body[:300])
    parts = []
    for nid, info in (data.get("node_errors") or {}).items():
        cls = info.get("class_type") or "?"
        for e in info.get("errors") or []:
            parts.append("%s#%s %s %s" % (cls, nid, e.get("message") or "", e.get("details") or ""))
    if not parts:
        err = data.get("error") or {}
        parts.append("%s %s" % (err.get("message") or body[:300], err.get("details") or ""))
    return "ComfyUI 拒绝这次提交: " + "；".join(p.strip() for p in parts)


def _post(url, data):
    # /interrupt 这类接口只回 200 空体，不能一律按 JSON 解析
    if _HTTP is not None:
        r = _HTTP.post(url, json=data, timeout=120)
        if r.status_code >= 400:
            raise RuntimeError(_comfy_error_text(r.status_code, r.reason, r.text))
        return r.json() if r.text.strip() else {}
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(_comfy_error_text(e.code, e.reason, e.read().decode("utf-8", "replace")))


def _get(url):
    if _HTTP is not None:
        r = _HTTP.get(url, timeout=30)
        r.raise_for_status()
        return r.json()
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def _comfy_prompt_state(base, prompt_id):
    """问 ComfyUI 这个 prompt 现在怎么样，返回 (状态, 历史条目)。
    done=历史里已有结果，alive=还在跑或还在排队，gone=队列和历史里都没有，
    说明它已经在 ComfyUI 那边结束了（网页端删了任务、ComfyUI 重启过等），别再傻等超时。"""
    h = _get(base + "/history/" + prompt_id)
    if h.get(prompt_id):
        return "done", h[prompt_id]
    q = _get(base + "/queue")
    for item in (q.get("queue_running") or []) + (q.get("queue_pending") or []):
        if len(item) > 1 and item[1] == prompt_id:
            return "alive", None
    time.sleep(4)   # 跑完到写历史之间有个很短的空档，隔几秒再确认一次，别把刚完成的误判成被删
    h = _get(base + "/history/" + prompt_id)
    return ("done", h[prompt_id]) if h.get(prompt_id) else ("gone", None)


def _submit_prompt(graph, timeout=1800, images_all=False):
    base = COMFY_URL.rstrip("/")
    job = _gen_job()
    if job["abort"].is_set():
        return {"ok": False, "cancelled": True, "error": "已取消生成"}
    r = _post(base + "/prompt", {"prompt": graph, "client_id": _COMFY_CLIENT})
    pid = r["prompt_id"]
    _log("ComfyUI 已收下 prompt_id=%s" % pid)
    _live_track(graph)
    if not _gen_bind_prompt(job, pid):
        _comfy_cancel_prompt(pid)
        return {"ok": False, "cancelled": True, "error": "已取消生成"}
    deadline = time.time() + timeout
    lost = 0
    while time.time() < deadline:
        if job["abort"].is_set():
            _log("ComfyUI prompt %s 已取消" % pid)
            return {"ok": False, "cancelled": True, "error": "已取消生成"}
        try:
            state, e = _comfy_prompt_state(base, pid)
        except Exception:
            lost += 1
            if lost >= 5:
                _log("ComfyUI 连续 %d 次问不到 prompt %s，按已断开处理" % (lost, pid))
                return {"ok": False, "error": "ComfyUI 连接中断，任务已结束"}
            job["abort"].wait(15)
            continue
        lost = 0
        if state == "gone":
            _log("ComfyUI 队列和历史里都没有 prompt %s，按已在 ComfyUI 侧结束处理" % pid)
            return {"ok": False, "error": "任务已在 ComfyUI 侧结束（队列里被删掉，或 ComfyUI 重启过）"}
        if e:
            st = e.get("status", {})
            if st.get("status_str") == "error":
                msgs = [m for m in st.get("messages", []) if isinstance(m, list) and m and m[0] == "execution_error"]
                if msgs:
                    err = msgs[-1][1].get("exception_message")
                elif any(isinstance(m, list) and m and m[0] == "execution_interrupted" for m in st.get("messages", [])):
                    err = "任务在 ComfyUI 界面被中断"
                else:
                    err = "unknown error"
                _log("ComfyUI prompt %s 执行出错: %s" % (pid, err))
                return {"ok": False, "error": err}
            if st.get("status_str") == "success":
                files = []
                for oid, out in e.get("outputs", {}).items():
                    for img in out.get("images", []):
                        sub = img.get("subfolder", "").strip("/")
                        fn = img["filename"]
                        rel_dir = os.path.basename(os.path.normpath(OUTPUT_DIR))
                        if sub == rel_dir:
                            file = fn
                        elif sub.startswith(rel_dir + "/"):
                            file = sub[len(rel_dir) + 1:] + "/" + fn
                        else:
                            file = (sub + "/" if sub else "") + fn
                        files.append(file)
                if files:
                    _log("ComfyUI prompt %s 完成" % pid)
                    return {"ok": True, "files": files} if images_all else {"ok": True, "file": files[0]}
        job["abort"].wait(15)
    _log("ComfyUI prompt %s 超时（%d 分钟无结果）" % (pid, timeout // 60))
    return {"ok": False, "error": "生成超时：%d 分钟无结果，ComfyUI 可能卡住，请到 http://127.0.0.1:8188 查看队列，必要时重启 ComfyUI" % (timeout // 60)}


def _extract_refine_frames(rel):
    """二采：抽成片首/尾帧作为 fl2va 关键帧，返回 (first_rel, last_rel) 或 (None, None)。"""
    src = os.path.join(OUTPUT_DIR, (rel or "").lstrip("/")) if not os.path.isabs(rel or "") else rel
    if not rel or not os.path.isfile(src):
        return None, None
    frames_dir = os.path.join(INPUT_DIR, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    stem = time.strftime("%H%M%S") + "_" + str(random.randrange(1000, 9999))
    first = os.path.join(frames_dir, "refine_" + stem + "_first.png")
    last = os.path.join(frames_dir, "refine_" + stem + "_last.png")
    try:
        extract_first_frame(src, first)
        extract_last_frame(src, last)
    except Exception:
        return None, None
    return ("frames/" + os.path.basename(first), "frames/" + os.path.basename(last))


def segment_scene(scene, states, idx, first=None, last=None, seed=None):
    """把相邻两个变化帧包成一个可生成的「镜头」：首帧/尾帧就是这两张关键帧。

    first/last 可覆盖关键帧（「换物体」就是拿改过的帧重出这一段）；seed 传原片用过的值能让运动尽量对上。
    """
    a, b = states[idx], states[idx + 1]
    seg = dict(scene)
    seg.update(first_frame=first or a["keyframe"], last_frame=last or b["keyframe"],
               seconds=a.get("seconds") or 4,
               prompt=a.get("prompt") or scene.get("prompt") or "",
               shot=a.get("shot") or scene.get("shot") or "中景",
               camera=a.get("camera") or scene.get("camera") or "固定镜头",
               subtitle=a.get("subtitle") or "", seed=seed,
               prefix="video/seg_%s_%d_%s" % (re.sub(r"[^0-9a-zA-Z_-]", "", str(scene.get("id"))),
                                               idx, time.strftime("%H%M%S")))
    seg.pop("output", None)
    return prepare_scene(seg)


def generate_scene(scene):
    """scene 需已 prepare。提交生成并轮询；second_pass=True 时抽首尾帧做 fl2va 二采精炼。"""
    try:
        graph = build_graph(scene)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    suffix = {"Wan2.2 5B": "Wan22", "Wan2.2 S2V": "S2V"}.get(scene.get("model"), "H3")
    save_workflow_files(graph, "Director_最新镜头_" + suffix)
    r1 = _submit_prompt(graph, scene.get("timeout", 1800))
    if not r1.get("ok") or not scene.get("second_pass") or scene.get("model") != "MiniMax H3":
        return r1
    refs = (scene.get("guides") or []) + (scene.get("auto_guides") or [])
    sc = scene.get("scene") or {}
    if refs or (sc.get("image") if scene.get("use_scene_ref") else None):
        return r1  # ref2va 无首尾帧输入，二采不适用
    first, last = _extract_refine_frames(r1["file"])
    if not first or not last:
        return r1
    refine = dict(scene)
    refine["first_frame"] = first
    refine["last_frame"] = last
    refine["second_pass"] = False
    refine["prefix"] = (scene.get("prefix") or "video/script2video") + "_refine"
    graph2 = build_graph(refine)
    save_workflow_files(graph2, "Director_最新镜头_" + suffix + "_二采")
    return _submit_prompt(graph2, scene.get("timeout", 1800))


def _ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def list_assets():
    """列出素材库（assets 目录）下的图片素材；生成产物（output）不属于素材库。"""
    base = os.path.abspath(ASSETS_DIR)
    exts = (".png", ".jpg", ".jpeg", ".webp", ".gif")
    out = []
    if os.path.isdir(base):
        for root, _dirs, files in os.walk(base):
            for f in files:
                if f.lower().endswith(exts):
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, base).replace(os.sep, "/")
                    st = os.stat(full)
                    out.append({"name": f, "path": rel, "size": st.st_size, "mtime": int(st.st_mtime)})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    threading.Thread(target=_sync_asset_stores, args=(out,), daemon=True).start()
    return out


def _sync_asset_stores(assets):
    """素材索引同步（30s 节流）：MySQL 事实表与 Neo4j Asset/Tag 图（id 取文件名主干）。"""
    global _LAST_ASSET_SYNC, _LAST_ASSET_FP
    now = time.time()
    if now - _LAST_ASSET_SYNC < 30:
        return
    tags = _load_json(TAGS_FILE, {})
    fp = (tuple(sorted((a["path"], a["mtime"], a["size"]) for a in assets)),
          tuple(sorted(tags.items())))
    if fp == _LAST_ASSET_FP:
        _LAST_ASSET_SYNC = now
        return
    ok2 = False
    if _mysql_ensure():
        rows = []
        for a in assets:
            rows.append("(%s,%s,%d,%d,%s,'manual')" % (_sql_quote(a["path"]), _sql_quote(a["name"]), int(a.get("size") or 0), int(a.get("mtime") or 0), _sql_quote(tags.get(a.get("path"), ""))))
        if rows:
            ok, _ = _mysql_run("INSERT INTO assets (path,name,size,mtime,tags,source) VALUES " + ",".join(rows) + " ON DUPLICATE KEY UPDATE name=VALUES(name),size=VALUES(size),mtime=VALUES(mtime)")
        else:
            ok = True
        if ok:
            paths = ",".join(_sql_quote(a["path"]) for a in assets)
            _mysql_run("DELETE FROM assets WHERE path NOT IN (%s)" % (paths or "''"))
            ok2, out = _mysql_run("SELECT path,name,tags FROM assets")
    if ok2:
        g_rows = []
        for line in out.splitlines():
            parts = line.split("\t", 2)
            if len(parts) >= 3 and parts[0]:
                g_rows.append({"path": parts[0], "name": parts[1], "tags": parts[2]})
    else:
        g_rows = [{"path": a["path"], "name": a["name"], "tags": tags.get(a.get("path"), "")} for a in assets]
    _neo4j_index_assets(g_rows)
    _LAST_ASSET_FP = fp
    _LAST_ASSET_SYNC = now


def resolve_asset_path(rel):
    """把素材相对路径解析为绝对路径，并做包含性校验。"""
    base = os.path.abspath(ASSETS_DIR)
    p = os.path.abspath(os.path.join(base, (rel or "").replace("/", os.sep)))
    if p != base and not p.startswith(base + os.sep):
        raise ValueError("非法路径")
    return p


def list_outputs():
    """列出保存区（OUTPUT_DIR）下的成片/镜头视频；跳过智能合成的桥接过渡帧等中间产物。"""
    base = os.path.abspath(OUTPUT_DIR)
    exts = (".mp4", ".webm", ".mov", ".mkv", ".avi")
    out = []
    if os.path.isdir(base):
        for root, _dirs, files in os.walk(base):
            for f in files:
                if f.lower().endswith(exts) and not f.startswith(("bridge_", "_")):
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, base).replace(os.sep, "/")
                    st = os.stat(full)
                    out.append({"name": f, "path": rel, "size": st.st_size, "mtime": int(st.st_mtime)})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def resolve_output_path(rel):
    """把保存区相对路径解析为绝对路径，并做包含性校验。"""
    base = os.path.abspath(OUTPUT_DIR)
    p = os.path.abspath(os.path.join(base, (rel or "").replace("/", os.sep)))
    if p != base and not p.startswith(base + os.sep):
        raise ValueError("非法路径")
    return p


def _looks_like_image(raw, ext):
    if ext == "png":
        return raw[:4] == bytes([0x89, 0x50, 0x4E, 0x47])
    if ext in ("jpg", "jpeg"):
        return raw[:3] == bytes([0xFF, 0xD8, 0xFF])
    if ext == "webp":
        return len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    return False

# ---- AI 大脑（本地知识图谱 + DeepSeek / Qwen-VL） ----
TAGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets_tags.json")

# ---- MySQL 素材库（pymysql + SQLAlchemy 连接池；默认 root 无密码，库 comfyui_assets） ----
MYSQL_HOST = os.environ.get("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = os.environ.get("MYSQL_PORT", "3306")
MYSQL_USER = os.environ.get("MYSQL_USER", "root")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
MYSQL_DB = os.environ.get("MYSQL_DB", "comfyui_assets")

_MYSQL_READY = None
_MYSQL_CHECKED = 0.0
_LAST_ASSET_SYNC = 0.0
_LAST_ASSET_FP = None
_MYSQL_ENGINE = None
_MYSQL_ENGINE_ERR = None


def _sql_quote(v):
    if v is None:
        return "NULL"
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'").replace("\x00", "") + "'"


def _mysql_engine():
    """懒加载 SQLAlchemy 连接池（pymysql 驱动），失败返回 None。"""
    global _MYSQL_ENGINE, _MYSQL_ENGINE_ERR
    if _MYSQL_ENGINE is not None:
        return _MYSQL_ENGINE
    if _MYSQL_ENGINE_ERR is not None:
        return None
    try:
        from sqlalchemy import create_engine
        from urllib.parse import quote_plus
    except ImportError as e:
        _MYSQL_ENGINE_ERR = str(e)
        return None
    url = "mysql+pymysql://%s:%s@%s:%s/%s?charset=utf8mb4" % (
        quote_plus(MYSQL_USER), quote_plus(MYSQL_PASSWORD), MYSQL_HOST, MYSQL_PORT, MYSQL_DB)
    try:
        _MYSQL_ENGINE = create_engine(url, pool_size=5, max_overflow=10, pool_recycle=3600, pool_pre_ping=True)
    except Exception as e:
        _MYSQL_ENGINE_ERR = str(e)
        return None
    return _MYSQL_ENGINE


def _mysql_run(sql, db=True, timeout=30):
    """用连接池执行单条 SQL，返回 (ok, output)；output 与 mysql.exe -N -B 一致（制表符分隔）。"""
    if not re.fullmatch(r"[A-Za-z0-9_]+", MYSQL_DB):
        return False, "MYSQL_DB 含非法字符"
    if db:
        eng = _mysql_engine()
        if eng is None:
            return False, _MYSQL_ENGINE_ERR or "pymysql/sqlalchemy 不可用"
        try:
            with eng.begin() as conn:
                res = conn.exec_driver_sql(sql)
                if res.returns_rows:
                    return True, "\n".join("\t".join("" if v is None else str(v) for v in row) for row in res)
                return True, ""
        except Exception as e:
            return False, str(e)
    import pymysql
    try:
        conn = pymysql.connect(host=MYSQL_HOST, port=int(MYSQL_PORT), user=MYSQL_USER,
                               password=MYSQL_PASSWORD, charset="utf8mb4", autocommit=True,
                               connect_timeout=timeout)
    except Exception as e:
        return False, str(e)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            if cur.description:
                return True, "\n".join("\t".join("" if v is None else str(v) for v in row) for row in cur.fetchall())
            return True, ""
    except Exception as e:
        return False, str(e)
    finally:
        conn.close()


def _mysql_ensure():
    """惰性建库建表；失败 30 秒内不重试。旧版无自增 id 的表会被重建。"""
    global _MYSQL_READY, _MYSQL_CHECKED
    now = time.time()
    if _MYSQL_READY or now - _MYSQL_CHECKED < 30:
        return bool(_MYSQL_READY)
    _MYSQL_CHECKED = now
    ok, _ = _mysql_run("CREATE DATABASE IF NOT EXISTS `%s` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" % MYSQL_DB, db=False)
    if not ok:
        _MYSQL_READY = False
        return False
    ok, cols = _mysql_run("SHOW COLUMNS FROM assets")
    if ok:
        names = [line.split("\t")[0] for line in cols.splitlines()]
        if "id" not in names:
            _mysql_run("DROP TABLE IF EXISTS assets")
            _mysql_run("DROP TABLE IF EXISTS auto_match_log")
    ok, _ = _mysql_run(
        "CREATE TABLE IF NOT EXISTS assets ("
        " id BIGINT AUTO_INCREMENT PRIMARY KEY,"
        " path VARCHAR(512) NOT NULL UNIQUE,"
        " name VARCHAR(255) NOT NULL,"
        " size BIGINT NOT NULL DEFAULT 0,"
        " mtime INT NOT NULL DEFAULT 0,"
        " tags TEXT,"
        " source VARCHAR(32) NOT NULL DEFAULT 'upload',"
        " used_count INT NOT NULL DEFAULT 0,"
        " used_by TEXT,"
        " last_used_at DATETIME NULL,"
        " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
        " updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;")
    if ok:
        ok, _ = _mysql_run(
            "CREATE TABLE IF NOT EXISTS auto_match_log ("
            " id BIGINT AUTO_INCREMENT PRIMARY KEY,"
            " prompt TEXT,"
            " matched_id BIGINT NULL,"
            " matched_path VARCHAR(512),"
            " matched_name VARCHAR(255),"
            " score INT NOT NULL DEFAULT 0,"
            " tags TEXT,"
            " hit TINYINT NOT NULL DEFAULT 0,"
            " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;")
    _MYSQL_READY = ok
    return bool(ok)


def _mysql_set_asset(path, name, size=0, mtime=0, tags="", source="upload"):
    """写入/更新素材行，返回自增 id；MySQL 不可用返回 None。"""
    if not _mysql_ensure():
        return None
    ok, _ = _mysql_run("INSERT INTO assets (path,name,size,mtime,tags,source) VALUES (%s,%s,%d,%d,%s,%s) ON DUPLICATE KEY UPDATE name=VALUES(name),size=VALUES(size),mtime=VALUES(mtime),tags=VALUES(tags)"
                       % (_sql_quote(path), _sql_quote(name), int(size or 0), int(mtime or 0), _sql_quote(tags), _sql_quote(source)))
    if not ok:
        return None
    ok2, out = _mysql_run("SELECT id FROM assets WHERE path=%s" % _sql_quote(path))
    if ok2 and out.strip().isdigit():
        return int(out.strip())
    return None


def _record_asset_usage(path, scene_id, prompt):
    """图片被输入生成时记录使用信息：次数 +1、最近使用时间、用途（镜头/提示词）。"""
    if not _mysql_ensure():
        return
    _mysql_run("UPDATE assets SET used_count=used_count+1, last_used_at=NOW(), used_by=CONCAT_WS(';', used_by, %s) WHERE path=%s"
               % (_sql_quote((scene_id or "")[:120] + ": " + (prompt or "")[:80]), _sql_quote(path)))


def _mysql_delete_asset(path):
    if not _mysql_ensure():
        return
    _mysql_run("DELETE FROM assets WHERE path=%s" % _sql_quote(path))


def _mysql_load_assets():
    """读素材索引（含自增 id）；MySQL 不可用返回 None（调用方回退本地）。"""
    if not _mysql_ensure():
        return None
    ok, out = _mysql_run("SELECT id,path,name,tags FROM assets")
    if not ok:
        return None
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 3)
        if len(parts) >= 2 and parts[1]:
            rows.append({"id": int(parts[0]) if parts[0].isdigit() else None, "path": parts[1], "name": parts[2], "tags": parts[3] if len(parts) > 3 else ""})
    return rows


def _mysql_log_match(prompt, result):
    """每次自动匹配都写流水（命中 hit=1，未命中 hit=0）。"""
    if not _mysql_ensure():
        return
    if result:
        _mysql_run("INSERT INTO auto_match_log (prompt,matched_id,matched_path,matched_name,score,tags,hit) VALUES (%s,%s,%s,%s,%d,%s,1)"
                   % (_sql_quote(prompt), _sql_quote(result.get("id")), _sql_quote(result.get("path")), _sql_quote(result.get("name")),
                      int(result.get("score") or 0), _sql_quote(result.get("tags"))))
    else:
        _mysql_run("INSERT INTO auto_match_log (prompt,hit) VALUES (%s,0)" % _sql_quote(prompt))

DEFAULT_KNOWLEDGE_GRAPH = {
    "nodes": [
        {"id": "shot_wide", "type": "景别", "label": "远景空镜", "text": "远景/空镜用于开场建场或段落过渡，交代时间地点氛围，常作情绪留白。", "keywords": ["远景", "空镜", "开场", "城市", "风景", "天际线", "街道"]},
        {"id": "shot_medium", "type": "景别", "label": "中景", "text": "中景交代人物动作与周围环境的关系，是叙事主力景别。", "keywords": ["中景", "动作", "人物"]},
        {"id": "shot_closeup", "type": "景别", "label": "特写", "text": "特写放大情绪与细节（手指、眼神、器物），浅景深，突出主体。", "keywords": ["特写", "细节", "情绪", "眼神", "手指"]},
        {"id": "camera_push", "type": "运镜", "label": "推镜", "text": "缓慢推近营造紧张、聚焦或情绪递进。", "keywords": ["推近", "推镜", "聚焦", "紧张", "递进"]},
        {"id": "camera_pan", "type": "运镜", "label": "摇镜", "text": "水平/垂直摇镜用于展示环境、跟随视线。", "keywords": ["摇镜", "环境", "视线"]},
        {"id": "style_cinematic", "type": "风格", "label": "电影感", "text": "电影质感，真实实拍风格，浅景深，胶片颗粒，冷暖对比光。", "keywords": ["电影感", "电影", "写实"], "always": True},
        {"id": "style_neon", "type": "风格", "label": "霓虹夜", "text": "夜景霓虹色调，冷蓝+品红对比，湿润反光，赛博氛围。", "keywords": ["夜景", "霓虹", "夜晚", "雨夜", "深夜", "城市夜"]},
        {"id": "style_warm", "type": "风格", "label": "温情暖调", "text": "暖色轮廓光，逆光发丝，柔和高光，温馨治愈氛围。", "keywords": ["温情", "温暖", "治愈", "暖心", "爱"]},
        {"id": "struct_open", "type": "结构", "label": "开场", "text": "开场 1-2 镜用远景/特写建立场景与情绪基调。", "keywords": ["开场", "开始"], "always": True},
        {"id": "struct_close", "type": "结构", "label": "收尾", "text": "结尾用空镜或回眸定格，留白，配字幕点题。", "keywords": ["结尾", "收尾", "结束"], "always": True}
    ],
    "edges": [
        {"from": "style_neon", "to": "shot_wide", "relation": "常搭配夜城空镜"},
        {"from": "style_warm", "to": "shot_closeup", "relation": "温情常用特写"},
        {"from": "shot_closeup", "to": "camera_push", "relation": "特写常配推镜"},
        {"from": "struct_open", "to": "shot_wide", "relation": "开场用远景"},
        {"from": "struct_close", "to": "shot_wide", "relation": "收尾用空镜"}
    ]
}


_NEO4J_DRIVER = None


def _neo4j_driver():
    global _NEO4J_DRIVER
    if _NEO4J_DRIVER is not None:
        return _NEO4J_DRIVER
    try:
        from neo4j import GraphDatabase
    except Exception:
        return None
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7688")
    user = os.environ.get("NEO4J_USER", "neo4j")
    pwd = os.environ.get("NEO4J_PASSWORD", "comfyui123")
    try:
        _NEO4J_DRIVER = GraphDatabase.driver(uri, auth=(user, pwd), connection_timeout=5)
    except Exception:
        return None
    return _NEO4J_DRIVER


def _neo4j_read_graph():
    d = _neo4j_driver()
    if not d:
        return {"nodes": [], "edges": []}
    try:
        with d.session() as s:
            nodes = s.run("MATCH (n:KGNode) RETURN n.id AS id, n.type AS type, n.label AS label, n.text AS text, n.keywords AS keywords, n.always AS always").data()
            edges = s.run("MATCH (a:KGNode)-[r:RELATES]->(b:KGNode) RETURN a.id AS from, b.id AS to, r.relation AS relation").data()
        return {"nodes": nodes, "edges": edges}
    except Exception:
        return {"nodes": [], "edges": []}


def _neo4j_write_graph(nodes, edges):
    d = _neo4j_driver()
    if not d:
        return False, "Neo4j 未连接"
    try:
        with d.session() as s:
            s.run("MATCH (n:KGNode) DETACH DELETE n")
            for n in nodes:
                s.run(
                    "CREATE (a:KGNode) SET a.id=$id, a.type=$type, a.label=$label, a.text=$text, a.keywords=$keywords, a.always=$always",
                    id=str(n.get("id") or ("n" + str(int(time.time() * 1000)))),
                    type=n.get("type", ""), label=n.get("label", ""),
                    text=n.get("text", ""), keywords=list(n.get("keywords") or []), always=bool(n.get("always")),
                )
            for e in edges:
                s.run(
                    "MATCH (a:KGNode {id:$f}), (b:KGNode {id:$t}) MERGE (a)-[:RELATES {relation:$r}]->(b)",
                    f=e.get("from"), t=e.get("to"), r=e.get("relation", ""),
                )
        return True, ""
    except Exception as e:
        return False, str(e)


def ensure_knowledge_graph():
    d = _neo4j_driver()
    if not d:
        return
    try:
        with d.session() as s:
            cnt = s.run("MATCH (n:KGNode) RETURN count(n) AS c").single()
            if cnt is None or cnt["c"] == 0:
                for n in DEFAULT_KNOWLEDGE_GRAPH["nodes"]:
                    s.run(
                        "MERGE (a:KGNode {id:$id}) SET a.type=$type, a.label=$label, a.text=$text, a.keywords=$keywords, a.always=$always",
                        id=n.get("id"), type=n.get("type", ""), label=n.get("label", ""),
                        text=n.get("text", ""), keywords=n.get("keywords", []), always=bool(n.get("always")),
                    )
                for e in DEFAULT_KNOWLEDGE_GRAPH["edges"]:
                    s.run(
                        "MATCH (a:KGNode {id:$f}), (b:KGNode {id:$t}) MERGE (a)-[:RELATES {relation:$r}]->(b)",
                        f=e.get("from"), t=e.get("to"), r=e.get("relation", ""),
                    )
    except Exception:
        pass


def _asset_graph_id(path):
    """Asset 节点 id：素材文件名主干（XX_time 结构），不依赖 MySQL 自增 id。"""
    return os.path.splitext(os.path.basename(path or ""))[0]


def _neo4j_index_assets(rows, prune=True):
    """素材入图：Asset 以 path 唯一、id 取文件名主干（XX_time），标签拆 Tag 节点连 HAS_TAG；
    prune=True 时移除已不在素材库的节点（全量同步用，单条更新必须传 False）。"""
    d = _neo4j_driver()
    if not d:
        return
    try:
        with d.session() as s:
            if prune:
                s.run("MATCH (a:Asset) WHERE NOT a.path IN $paths DETACH DELETE a", paths=[r["path"] for r in rows])
            for r in rows:
                p = r["path"]
                gid = _asset_graph_id(p)
                if not gid:
                    continue
                s.run("MERGE (a:Asset {path:$p}) SET a.id=$id, a.name=$n", p=p, id=gid, n=r.get("name") or os.path.basename(p))
                for t in re.split(r"[,，|。:：]+", (r.get("tags") or "").strip()):
                    t = t.strip()
                    if t:
                        s.run("MATCH (a:Asset {path:$p}) MERGE (t:Tag {name:$t}) MERGE (a)-[:HAS_TAG]->(t)", p=p, t=t)
    except Exception:
        pass


def _neo4j_unindex_asset(path):
    d = _neo4j_driver()
    if not d:
        return
    try:
        with d.session() as s:
            s.run("MATCH (a:Asset {path:$p}) DETACH DELETE a", p=path)
    except Exception:
        pass


def _neo4j_read_asset_graph():
    """素材图谱：Asset（XX_time id）与 Tag 节点、HAS_TAG/FITS 关系，供图谱面板素材视图展示。"""
    d = _neo4j_driver()
    if not d:
        return {"nodes": [], "edges": []}
    try:
        with d.session() as s:
            assets = s.run("MATCH (a:Asset) RETURN a.id AS id, a.path AS path, a.name AS name").data()
            tags = s.run("MATCH (t:Tag) RETURN t.name AS name").data()
            has_tag = s.run("MATCH (a:Asset)-[:HAS_TAG]->(t:Tag) RETURN a.id AS aid, t.name AS tag").data()
            fits = s.run("MATCH (a:Asset)-[:FITS]->(k:KGNode) RETURN a.id AS aid, k.id AS kid, k.label AS label").data()
        nodes = [{"id": a["id"], "type": "asset", "label": a["id"], "path": a["path"], "name": a["name"]} for a in assets]
        nodes += [{"id": "tag:" + t["name"], "type": "tag", "label": t["name"]} for t in tags]
        for r in fits:
            if not any(n["id"] == r["kid"] and n["type"] == "kg" for n in nodes):
                nodes.append({"id": r["kid"], "type": "kg", "label": r["label"] or r["kid"]})
        edges = [{"from": r["aid"], "to": "tag:" + r["tag"], "relation": "HAS_TAG"} for r in has_tag]
        edges += [{"from": r["aid"], "to": r["kid"], "relation": "FITS" + ("·" + r["label"] if r["label"] else "")} for r in fits]
        return {"nodes": nodes, "edges": edges}
    except Exception:
        return {"nodes": [], "edges": []}


def _neo4j_vocab():
    """一次读齐自动匹配用词表：全部 Tag 名 + KGNode 关键词组；失败返回 None。"""
    d = _neo4j_driver()
    if not d:
        return None
    try:
        with d.session() as s:
            tags = [str(r["name"]).lower() for r in s.run("MATCH (t:Tag) RETURN t.name AS name").data()]
            rows = s.run("MATCH (n:KGNode) RETURN n.id AS id, n.keywords AS keywords").data()
        groups = {r["id"]: [str(k).lower() for k in (r.get("keywords") or [])] for r in rows}
        return {"tags": tags, "groups": groups}
    except Exception:
        return None


def _neo4j_match_tags(kws):
    """图算法检索：提示词关键词沿 Tag<-HAS_TAG-Asset 路径命中素材，返回 {path: 命中标签数}；失败返回 None。"""
    if not kws:
        return {}
    d = _neo4j_driver()
    if not d:
        return None
    try:
        with d.session() as s:
            rows = s.run("MATCH (t:Tag)<-[:HAS_TAG]-(a:Asset) WHERE toLower(t.name) IN $kws RETURN a.path AS path, count(t) AS c", kws=kws).data()
        return {r["path"]: r["c"] for r in rows}
    except Exception:
        return None


def _neo4j_asset_fits(path, node_ids):
    """沉淀匹配知识：素材与命中的镜头语义节点建立 FITS 关系。"""
    if not node_ids:
        return
    d = _neo4j_driver()
    if not d:
        return
    try:
        with d.session() as s:
            for nid in node_ids:
                s.run("MATCH (a:Asset {path:$p}), (k:KGNode {id:$i}) MERGE (a)-[:FITS]->(k)", p=path, i=nid)
    except Exception:
        pass


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _proxy_reachable(proxy):
    """环境里配的那个代理还活着吗。"""
    try:
        u = urlparse(proxy if "://" in proxy else "http://" + proxy)
        if not u.hostname:
            return True
        with socket.create_connection((u.hostname, u.port or 80), timeout=1.5):
            return True
    except Exception:
        return False


_PROXY_OK = None            # None＝还没探过；探一次记住，别每次调用都去连一下
_DIRECT_OPENER = None


def _llm_urlopen(req, timeout):
    """调大模型接口用的 urlopen：环境代理连不上就直接连。

    Windows 上 HTTP(S)_PROXY 常常是某个代理客户端留下的配置（Clash 之类），客户端没开着的
    时候 urllib 会把请求发给没人监听的 127.0.0.1:7897，报 WinError 10061「目标计算机积极拒绝」。
    浏览器有自己的代理设置、不受影响，所以看起来就像「只有这个程序连不上网」。
    DeepSeek、通义这些地址本来就该直连，代理只在它真的活着的时候才用。
    """
    global _PROXY_OK, _DIRECT_OPENER
    if _PROXY_OK is None:
        proxies = sorted({v for k, v in urllib.request.getproxies().items() if k != "no" and v})
        _PROXY_OK = all(_proxy_reachable(p) for p in proxies)
        if not _PROXY_OK:
            _log("环境里的代理连不上（%s），大模型接口改直连" % "、".join(proxies))
    if _PROXY_OK:
        return urllib.request.urlopen(req, timeout=timeout)
    if _DIRECT_OPENER is None:
        _DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _DIRECT_OPENER.open(req, timeout=timeout)


def _llm_chat(base_url, api_key, model, messages, max_tokens=2000, timeout=180):
    url = base_url.rstrip("/") + "/chat/completions"
    data = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": False}
    req = urllib.request.Request(
        url, data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        method="POST",
    )
    with _llm_urlopen(req, timeout) as r:
        resp = json.load(r)
    return resp["choices"][0]["message"]["content"]


def retrieve_script_knowledge(topic):
    ensure_knowledge_graph()
    d = _neo4j_driver()
    if not d:
        return ""
    try:
        with d.session() as s:
            nodes = s.run("MATCH (n:KGNode) RETURN n.id AS id, n.label AS label, n.text AS text, n.keywords AS keywords, n.always AS always").data()
            edges = s.run("MATCH (a:KGNode)-[:RELATES]->(b:KGNode) RETURN a.id AS f, b.id AS t").data()
        node_map = {n["id"]: n for n in nodes}
        adj = {}
        for e in edges:
            adj.setdefault(e["f"], []).append(e["t"])
            adj.setdefault(e["t"], []).append(e["f"])
        matched = set()
        for n in nodes:
            kws = n.get("keywords") or []
            if any(kw and kw in topic for kw in kws):
                matched.add(n["id"])
        expanded = set(matched)
        for mid in matched:
            for nb in adj.get(mid, []):
                expanded.add(nb)
        picks = []
        for nid in expanded:
            n = node_map.get(nid)
            if n:
                picks.append("[" + n["label"] + "] " + n["text"])
        if not picks:
            for n in nodes:
                if n.get("always"):
                    picks.append("[" + n["label"] + "] " + n["text"])
        return "\n".join(picks[:8])
    except Exception:
        return ""


def gen_script_with_ai(topic, api_key=None, base_url=None, model=None, sys_prompt=None):
    api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
    base_url = base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    if not api_key:
        return {"ok": False, "error": "未配置 DeepSeek API Key（请在前端「设置」里填写）"}
    know = retrieve_script_knowledge(topic)
    if not sys_prompt:
        sys_prompt = (
            "你是一名资深视频导演与编剧。根据用户主题生成一个 30 秒以内的短视频分镜脚本。"
            "必须输出 Markdown 表格，列名固定为：时间 | 画面 | 台词/文案 | 音效/BGM。"
            "5-8 个镜头，每镜 3-8 秒，时间连续（如 0-5s、5-10s）。"
            "画面描述要具体、有电影感；台词列用“画外音：…”或“字幕：…”。"
            "只输出分镜表本身，不要任何解释或多余文字。"
        )
    user_prompt = "主题：" + topic + "\n\n参考镜头语言（来自本地知识图谱）：\n" + know
    try:
        text = _llm_chat(base_url, api_key, model, [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ])
        return {"ok": True, "script": text.strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _thumbnail_b64(abs_path):
    ext = os.path.splitext(abs_path)[1].lower()
    if ext in (".png", ".jpg", ".jpeg", ".webp"):
        with open(abs_path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    if ext in (".mp4", ".webm", ".mov", ".mkv", ".avi"):
        ff = _ffmpeg()
        tmp = os.path.join(OUTPUT_DIR, "_thumb_" + str(int(time.time() * 1000)) + ".jpg")
        try:
            subprocess.run([ff, "-y", "-i", abs_path, "-frames:v", "1", "-vf", "scale=512:-1", "-q:v", "3", tmp],
                           check=True, capture_output=True, timeout=60)
            with open(tmp, "rb") as f:
                return base64.b64encode(f.read()).decode()
        except Exception:
            return ""
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass
    return ""


def tag_asset_with_ai(path, api_key=None, base_url=None, model=None, prompt=None):
    api_key = api_key or os.environ.get("QWEN_VL_API_KEY", "")
    base_url = base_url or os.environ.get("QWEN_VL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    model = model or os.environ.get("QWEN_VL_MODEL", "qwen3-vl-plus")
    if not api_key:
        return {"ok": False, "error": "未配置 Qwen-VL API Key（请在前端「设置」里填写）"}
    try:
        abs_path = resolve_asset_path(path)
    except ValueError as ve:
        return {"ok": False, "error": str(ve)}
    if not os.path.isfile(abs_path):
        return {"ok": False, "error": "文件不存在"}
    img_b64 = _thumbnail_b64(abs_path)
    if not img_b64:
        return {"ok": False, "error": "无法生成缩略图"}
    if not prompt:
        prompt = "用一句话描述这张画面，然后给出 4-6 个中文标签词（逗号分隔），格式：描述||标签1,标签2"
    content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + img_b64}},
    ]
    try:
        text = _llm_chat(base_url, api_key, model, [{"role": "user", "content": content}], max_tokens=500)
        tags = text.strip()
        data = _load_json(TAGS_FILE, {})
        data[path] = tags
        try:
            json.dump(data, open(TAGS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        except Exception:
            pass
        _mysql_set_asset(path, os.path.basename(path), tags=tags, source="manual")
        _neo4j_index_assets([{"path": path, "name": os.path.basename(path), "tags": tags}], prune=False)
        return {"ok": True, "tags": tags}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def search_assets(q):
    q = (q or "").strip().lower()
    assets = list_assets()
    if not q:
        return assets
    tags = _load_json(TAGS_FILE, {})
    result = []
    for a in assets:
        name = (a.get("name") or "").lower()
        tag_text = (tags.get(a.get("path"), "") or "").lower()
        if q in name or q in tag_text or q in a.get("path", "").lower():
            result.append(a)
    return result


_TAG_SPLIT = re.compile(r"[,，|。:：;；/\s]+")
_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
_BGE_MODEL = os.environ.get("BGE_MODEL", "bge-m3:latest")
_BGE_COS_MIN = float(os.environ.get("BGE_COS_MIN", "0.45"))
_OLLAMA_CHAT_MODEL = os.environ.get("OLLAMA_CHAT_MODEL", "qwen3:8b")
_OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")
AI_SPLIT_MAX_CHARS = int(os.environ.get("AI_SPLIT_MAX_CHARS", "6000"))
AI_SPLIT_MAX_TOKENS = int(os.environ.get("AI_SPLIT_MAX_TOKENS", "3000"))
AI_SPLIT_TIMEOUT = int(os.environ.get("AI_SPLIT_TIMEOUT", "900"))
_AI_MODEL = ""
_OLLAMA_OK = True
_OLLAMA_CHECKED = 0.0


def _segment_prompt(prompt_text, vocab, tags_json):
    """提示词分词：jieba 精确模式 + 图词典（Tag 名/KG 关键词/素材标签词），
    保证「天安门广场」这类标签词整体切出；jieba 缺失时退回正则切分。"""
    if jieba is not None:
        words = []
        if vocab:
            words += [w for w in (vocab.get("tags") or []) if len(w) >= 2]
            for gkws in (vocab.get("groups") or {}).values():
                words += [w for w in (gkws or []) if len(w) >= 2]
        for tag_text in (tags_json or {}).values():
            words += [w for w in _TAG_SPLIT.split(str(tag_text).lower()) if len(w) >= 2]
        for w in words:
            jieba.add_word(w)
        segs = jieba.lcut(prompt_text)
    else:
        segs = re.split(r"[^0-9a-z一-鿿]+", prompt_text)
    return [w for w in segs if len(w) >= 2]


def _bge_similarities(prompt, rows):
    """bge-m3 批量算提示词与各素材标签文本的余弦相似度，返回 {path: cos}；Ollama 不可用返回 None。"""
    global _OLLAMA_OK, _OLLAMA_CHECKED
    texts = [((r.get("tags") or "").strip() + " " + (r.get("name") or "").split(".")[0]).strip() for r in rows]
    pairs = [(r["path"], t) for r, t in zip(rows, texts) if t]
    if not pairs:
        return {}
    if not _OLLAMA_OK and time.time() - _OLLAMA_CHECKED < 60:
        return None
    try:
        req = urllib.request.Request(
            _OLLAMA_URL + "/api/embed",
            data=json.dumps({"model": _BGE_MODEL, "input": [prompt] + [t for _p, t in pairs], "keep_alive": _OLLAMA_KEEP_ALIVE}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            embs = json.load(r)["embeddings"]

        def cos(a, b):
            d = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
            return sum(x * y for x, y in zip(a, b)) / (d or 1)

        out = {}
        for (p, _t), e in zip(pairs, embs[1:]):
            out[p] = cos(embs[0], e)
        _OLLAMA_OK = True
        _OLLAMA_CHECKED = time.time()
        return out
    except Exception:
        _OLLAMA_OK = False
        _OLLAMA_CHECKED = time.time()
        return None


def auto_match_image(prompt):
    """自动匹配素材图：jieba 分词（图词典对齐 Tag 词表）→ Neo4j 沿 提示词词→Tag→Asset 路径检索计命中数，
    bge-m3 向量相似度做语义兜底，叠加知识图谱扩展与文件名打分取综合最高分；每次调用写 MySQL 匹配流水（带素材 id），
    命中素材与镜头语义沉淀 FITS。任一环节不可用时回退规则打分，功能不中断。"""
    prompt_text = (prompt or "").lower()
    assets = list_assets()
    tags_json = _load_json(TAGS_FILE, {})
    rows = _mysql_load_assets()
    if rows is None:
        rows = [{"id": None, "path": a["path"], "name": a["name"], "tags": tags_json.get(a["path"], "")} for a in assets]
    vocab = _neo4j_vocab()
    kws = _segment_prompt(prompt_text, vocab, tags_json)
    extra_kws = []
    hit_nodes = []
    if vocab:
        for nid, gkws in (vocab.get("groups") or {}).items():
            if any(kw and kw in prompt_text for kw in gkws):
                hit_nodes.append(nid)
                for kw in gkws:
                    if kw and kw not in extra_kws:
                        extra_kws.append(kw)
    graph_hits = _neo4j_match_tags(kws)
    sims = _bge_similarities(prompt_text, rows) if prompt_text else None
    best = None
    for r in rows:
        path = r["path"]
        score = 4 * int(graph_hits.get(path, 0)) if graph_hits is not None else 0
        stem = (r.get("name") or "").split(".")[0].lower()
        for kw in re.split(r"[^0-9a-z一-鿿]+", stem):
            if len(kw) >= 2 and kw in prompt_text:
                score += 2
        tag_text = (r.get("tags") or "").lower()
        if graph_hits is None:
            for kw in _TAG_SPLIT.split(tag_text):
                if len(kw) >= 2 and kw in prompt_text:
                    score += 4
        for kw in extra_kws:
            if kw and kw in tag_text:
                score += 2
        cosv = sims.get(path) if sims is not None else None
        if cosv is not None:
            score += 6 * cosv
        hit = graph_hits is not None and graph_hits.get(path, 0) > 0
        if score > 0 and (hit or cosv is None or cosv >= _BGE_COS_MIN) and (best is None or score > best[0]):
            best = (score, r)
    result = None
    if best:
        score, r = best
        result = {"id": r.get("id"), "path": r["path"], "name": r["name"], "score": round(score, 1), "tags": r.get("tags", "")}
        _neo4j_asset_fits(result["path"], hit_nodes)
    _mysql_log_match(prompt or "", result)
    return result


# ---- 脚本解析/优化 ----
IMG_EXTS = ("png", "jpg", "jpeg", "webp")
LOCAL_IMG_RE = re.compile(r"""!\[[^\]]*\]\(([^)]+)\)|([^\s|<>"'，。；]*\.(?:png|jpe?g|webp))""", re.I)


def _local_image_file(token):
    """脚本里写到的图片路径：绝对路径、相对 ComfyUI 根、素材库、input 都认。"""
    t = unquote((token or "").strip().strip("\"'"))
    if t.startswith("file://"):
        t = unquote(urlparse(t).path).lstrip("/")
    for p in (t, os.path.join(_COMFY_ROOT, t), os.path.join(ASSETS_DIR, t), os.path.join(INPUT_DIR, t)):
        if p and os.path.splitext(p)[1].lower().lstrip(".") in IMG_EXTS and os.path.isfile(p):
            return p
    return None


def _save_scene_frame(raw, ext, scene_id):
    """把分镜表里的图片落到 input/frames，返回可直接当首帧的相对路径。"""
    ext = "jpg" if ext == "jpeg" else ext
    if ext not in ("png", "jpg", "webp") or not _looks_like_image(raw, ext):
        return None
    frames_dir = os.path.join(INPUT_DIR, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    name = "story_" + re.sub(r"[^0-9a-zA-Z_-]", "", scene_id) + "_" + time.strftime("%H%M%S") + "_" + str(random.randrange(1000, 9999)) + "." + ext
    with open(os.path.join(frames_dir, name), "wb") as f:
        f.write(raw)
    return "frames/" + name


def attach_story_images(scenes, images):
    """分镜表里的图片自动当首帧：先按单元格 [图片N] 标记对位，剩下的按镜头顺序补位；
    脚本里写到的本地图片路径（含 Markdown 图片语法）也一并识别。返回带入的图片数。"""
    left = list(images)
    attached = 0
    for sc in scenes:
        prompt = sc.get("prompt") or ""
        raw = ext = None
        for m in re.finditer(r"\[图片(\d+)\]", prompt):
            i = int(m.group(1))
            if 0 <= i < len(left) and left[i]:
                raw, ext = left[i]
                left[i] = None
                break
        if not raw:
            for m in LOCAL_IMG_RE.finditer(prompt):
                p = _local_image_file(m.group(1) or m.group(2))
                if p:
                    with open(p, "rb") as f:
                        raw, ext = f.read(), os.path.splitext(p)[1].lower().lstrip(".")
                    prompt = prompt.replace(m.group(0), " ")
                    break
        prompt = re.sub(r"\s*\[图片\d+\]\s*", " ", prompt)
        sc["prompt"] = re.sub(r"\s{2,}", " ", prompt).strip()
        if not raw:
            for i, im in enumerate(left):
                if im:
                    raw, ext = im
                    left[i] = None
                    break
        rel = _save_scene_frame(raw, ext, sc.get("id") or "scene") if raw else None
        if rel:
            sc["first_frame"] = rel
            attached += 1
    return attached


def _docx_rels(z):
    try:
        xml = z.read("word/_rels/document.xml.rels").decode("utf-8", "ignore")
    except KeyError:
        return {}
    return {m.group(1): m.group(2) for m in re.finditer(r'<Relationship\b[^>]*Id="([^"]+)"[^>]*?Target="([^"]+)"', xml)}


def _docx_media(z, target):
    """关系目标 -> (图片字节, 扩展名)；不是图片或部件缺失返回 None。"""
    if not target:
        return None
    name = unquote(target).replace("\\", "/").lstrip("/")
    if not name.startswith("word/"):
        name = "word/" + name
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    if ext not in IMG_EXTS:
        return None
    try:
        return z.read(name), ext
    except KeyError:
        return None


def _docx_paragraph(p, z, rels, images):
    """段落文本；段内图片写成 [图片N] 标记（N 是 images 下标，供分镜表按行对位）。"""
    line = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, re.S)).strip()
    for rid in re.findall(r'r:embed="([^"]+)"', p):
        got = _docx_media(z, rels.get(rid))
        if got:
            images.append(got)
            line += "[图片%d]" % (len(images) - 1)
    return line


def _extract_docx_raw(raw, images):
    """docx -> 文本：表格按 | 拼成一行，行内图片留 [图片N] 标记。"""
    z = zipfile.ZipFile(io.BytesIO(raw))
    xml = z.read("word/document.xml").decode("utf-8", "ignore")
    rels = _docx_rels(z)
    out = []
    for block in re.findall(r"<w:tbl[ >].*?</w:tbl>|<w:p[ >].*?</w:p>", xml, re.S):
        if not block.startswith("<w:tbl"):
            line = _docx_paragraph(block, z, rels, images)
            if line:
                out.append(line)
            continue
        for tr in re.findall(r"<w:tr[ >].*?</w:tr>", block, re.S):
            cells = []
            for tc in re.findall(r"<w:tc[ >].*?</w:tc>", tr, re.S):
                cells.append("".join(_docx_paragraph(p, z, rels, images) for p in re.findall(r"<w:p[ >].*?</w:p>", tc, re.S)).strip())
            if any(cells):
                out.append(" | ".join(cells))
    if not images:
        for name in sorted(z.namelist()):
            if re.match(r"word/media/[^/]+$", name):
                got = _docx_media(z, name)
                if got:
                    images.append(got)
    return chr(10).join(out)


def _extract_xlsx_raw(raw, images):
    """xlsx -> 文本：多列表格按 | 拼成一行（分镜表），单列仍一行一个值；图片按部件顺序带上。"""
    z = zipfile.ZipFile(io.BytesIO(raw))
    shared = []
    try:
        xml = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
        shared = ["".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S))
                  for si in re.findall(r"<si>.*?</si>", xml, re.S)]
    except KeyError:
        pass
    rows = []
    for name in sorted(z.namelist()):
        if not re.match(r"xl/worksheets/sheet\d+\.xml$", name):
            continue
        xml = z.read(name).decode("utf-8", "ignore")
        for row in re.findall(r"<row\b.*?</row>", xml, re.S):
            vals = []
            for cell in re.findall(r"<c\b.*?</c>", row, re.S):
                inline = "".join(re.findall(r"<t[^>]*>(.*?)</t>", cell, re.S))
                if inline:
                    vals.append(inline)
                    continue
                m = re.search(r"<v[^>]*>(.*?)</v>", cell, re.S)
                if m and re.search(r't="s"', cell) and m.group(1).isdigit():
                    vals.append(shared[int(m.group(1))])
            if vals:
                rows.append(vals)
    for name in sorted(z.namelist()):
        if re.match(r"xl/media/[^/]+$", name):
            ext = os.path.splitext(name)[1].lower().lstrip(".")
            if ext in IMG_EXTS:
                images.append((z.read(name), ext))
    if any(len(r) >= 2 for r in rows):
        return chr(10).join(" | ".join(r) for r in rows)
    # 单列时一行一个镜头（空行分隔，避免整列并成一个分镜）
    return (chr(10) * 2).join(v for r in rows for v in r)


def _rtf_to_text(raw):
    s = raw.decode("latin-1")
    blob = bytearray()
    pos = 0
    for m in re.finditer(r"\\'([0-9a-fA-F]{2})", s):
        blob += s[pos:m.start()].encode("latin-1")
        blob.append(int(m.group(1), 16))
        pos = m.end()
    blob += s[pos:].encode("latin-1")
    text = bytes(blob).decode("gbk", "ignore")
    text = text.replace(r"\par ", chr(10)).replace(r"\par", chr(10)).replace(r"\line", chr(10)).replace(r"\tab", chr(9))
    text = re.sub(r"\\[a-zA-Z]+-?[0-9]* ?", "", text)
    text = re.sub(r"\\(.)", r"\1", text)
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"[ \t]*[\r\n]+[ \t]*", chr(10), text).strip()


def _extract_binary_text(raw):
    """Word 97+/BIFF8 文本多为 UTF-16LE；解不出内容时退到 GBK。"""
    good = "[\u4e00-\u9fffA-Za-z0-9 \uff0c\u3002\u3001\uff1b\uff1a\uff01\uff1f\uff08\uff09\u300a\u300b\u300c\u300d\u201c\u201d\u00b7\u2014\u2026|()\\-+%:,.!?~'\"#/=_]+"
    def _runs(s):
        out = []
        for m in re.finditer(good, s):
            seg = m.group(0).strip()
            if len(re.findall(r"[\u4e00-\u9fff]", seg)) >= 2 or len(re.findall(r"[A-Za-z0-9]", seg)) >= 4:
                out.append(seg)
        return out
    segs = []
    for cut in (0, 1):
        segs.extend(_runs(raw[cut:].decode("utf-16-le", "ignore")))
    if not segs:
        segs = _runs(raw.decode("gbk", "ignore"))
    return chr(10).join(dict.fromkeys(segs))


def extract_document(b64, filename=""):
    """md/doc/docx/xls -> (纯文本, 内嵌图片列表)（标准库尽力而为）。"""
    raw = base64.b64decode(b64)
    ext = os.path.splitext(filename or "")[1].lower().lstrip(".")
    images = []
    if ext == "docx" or (ext == "doc" and raw[:2] == b"PK"):
        return _extract_docx_raw(raw, images), images
    if ext == "xlsx" or (ext == "xls" and raw[:2] == b"PK"):
        return _extract_xlsx_raw(raw, images), images
    if ext in ("md", "markdown", "txt"):
        for enc in ("utf-8-sig", "gbk"):
            try:
                return raw.decode(enc), images
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "ignore"), images
    if ext == "doc" and raw.lstrip()[:5].lower() == b"{\\rtf":
        return _rtf_to_text(raw), images
    if ext in ("doc", "xls"):
        return _extract_binary_text(raw), images
    if raw[:2] == b"PK":  # 扩展名不靠谱（.doc/.xls 里装的是 OOXML）时按压缩包部件判断
        if any(n.startswith("word/") for n in zipfile.ZipFile(io.BytesIO(raw)).namelist()):
            return _extract_docx_raw(raw, images), images
        return _extract_xlsx_raw(raw, images), images
    return _extract_binary_text(raw), images


TIME_CELL_RE = re.compile(r"^[0-9]{1,3}(\.[0-9]+)?\s*([-~—－至到]\s*[0-9]{1,3}(\.[0-9]+)?|[sS秒])")
NUM_CELL_RE = re.compile(r"^[0-9.、]+$")
VOICE_TAG_RE = re.compile(r"^[（(【\[]*\s*(旁白|配音|画外音|解说|独白|台词|对白)\s*[）)】\]]*\s*[：:]?\s*")
VOICE_NONE_RE = re.compile(r"^[（(【\[]*\s*(无|没有|略|空)[^）)】\]]*[）)】\]]*$")
NARRATION_KEYS = ("旁白", "配音", "画外音", "解说", "独白")


def _row_seconds(t):
    """时间/时长单元格 -> 秒：0-5s / 5秒 / 5-11秒 / 00:05 / 5（含各种连字符、全角写法）。"""
    t = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uff0d~\uff5e\u81f3\u5230]", "-", (t or "").strip())
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*[-~—－至到]\s*([0-9]+(?:\.[0-9]+)?)", t)
    if m:
        return max(3, min(15, int(round(float(m.group(2)) - float(m.group(1))))))
    m = re.search(r"([0-9]+)\s*[:：]\s*([0-9]{1,2})", t)
    if m:
        return max(3, min(15, int(m.group(1)) * 60 + int(m.group(2))))
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", t)
    return max(3, min(15, int(round(float(m.group(1)))))) if m else 5


def is_storyboard_table(text):
    """已经是分镜表（Markdown/Excel/Word 表格）：行数够多就别再让 AI 重拆——模型会把长表压成 8 个镜头。"""
    return len([l for l in text.splitlines() if "|" in l or "	" in l]) >= 3


def optimize_script(text):
    """把脚本解析成分镜列表（画面描述，不含风格后缀）。"""
    text = text.strip()
    lines = [l for l in text.splitlines() if l.strip()]
    # 1) 分镜表：含 | 或制表符的行
    tab_rows = []
    for l in lines:
        if "|" in l:
            cells = [c.strip() for c in l.strip().strip("|").split("|")]
        elif "	" in l:
            cells = [c.strip() for c in l.split("	")]
        else:
            continue
        if all(re.fullmatch(r":?-+:?", c) for c in cells if c):  # Markdown 分隔行
            continue
        tab_rows.append(cells)
    if len(tab_rows) >= 2:
        header = [h.lower() for h in tab_rows[0]]
        idx = {"t": -1, "pic": -1, "voice": -1, "audio": -1, "sub": -1}
        narr = False  # 台词列写的是旁白/配音 -> 这列的话要上屏当字幕
        for i, h in enumerate(header):
            if "字幕" in h: idx["sub"] = i
            if "时间" in h or "时长" in h or "秒" in h or "s" == h: idx["t"] = i
            if "画面" in h or "分镜" in h or "描述" in h or "内容" in h or "提示词" in h: idx["pic"] = i
            if "台词" in h or "文案" in h or "对白" in h or "旁白" in h or "独白" in h or "配音" in h or "解说" in h: idx["voice"] = i
            if any(k in h for k in NARRATION_KEYS): narr = True
            if "音效" in h or "bgm" in h or "音乐" in h or "配乐" in h: idx["audio"] = i
        # 表头一个列名都没认出来时，首行也是数据（很多表格第一行直接就是镜头）
        rows = tab_rows[1:] if any(v >= 0 for v in idx.values()) else tab_rows
        if idx["t"] < 0:  # 没写时间列时按内容认：这一列多数格子长得像 0-5s / 5秒
            for i in range(max(len(r) for r in rows)):
                cells = [r[i].strip() for r in rows if i < len(r) and r[i].strip()]
                if cells and sum(1 for c in cells if TIME_CELL_RE.match(c)) * 2 > len(cells):
                    idx["t"] = i
                    break
        scenes = []
        for row in rows:
            if len(row) < 2 or not any(row): continue
            def cell(i): return row[i] if 0 <= i < len(row) else ""
            pic = cell(idx["pic"]) if idx["pic"] >= 0 else ""
            if not pic:  # 没有画面列（或该格空）时：本行最长的文字格当画面描述
                skip = {idx["t"], idx["voice"], idx["audio"], idx["sub"]}
                cands = [c for i, c in enumerate(row) if i not in skip and c and not NUM_CELL_RE.match(c)]
                pic = max(cands, key=len) if cands else ""
            voice = cell(idx["voice"]) if idx["voice"] >= 0 else ""
            audio = cell(idx["audio"]) if idx["audio"] >= 0 else ""
            sec = _row_seconds(cell(idx["t"]) if idx["t"] >= 0 else "")
            subtitle = cell(idx["sub"]).strip() if idx["sub"] >= 0 else ""
            voice_tag = bool(VOICE_TAG_RE.match(voice))
            voice_text = VOICE_TAG_RE.sub("", voice).strip()
            if VOICE_NONE_RE.match(voice_text):  # （无旁白）/（无）/（略）
                voice_text = ""
            voice_part = ""
            if voice_text:
                if voice_text.startswith("字幕") or "字幕：" in voice[:6]:
                    subtitle = re.sub(r"^[^：:]*[：:] *", "", voice_text).strip()
                else:
                    voice_part = "画外音：" + voice_text
                # 旁白/配音的原话同时当底部字幕（表格另有字幕列时不覆盖）
                if not subtitle and (narr or voice_tag):
                    subtitle = voice_text
            parts = []
            if pic: parts.append(pic)
            if voice_part: parts.append(voice_part)
            if audio: parts.append("音效：" + audio)
            prompt = "。".join(parts)
            if not prompt:  # 列名全认不出来时也别吐空镜头
                prompt = "。".join(c for c in row if c and not NUM_CELL_RE.match(c))
            scenes.append({"seconds": sec, "length": seconds_to_length(sec), "prompt": prompt, "subtitle": subtitle})
        if scenes and any(s["prompt"] for s in scenes):
            return scenes
    # 2) 纯文字/故事：按空行分段，段内按句子攒镜，每镜不超过 15 秒（约 60 字 5 秒）
    blocks = [b.strip() for b in text.replace(chr(13), "").split(chr(10) + chr(10)) if b.strip()]
    if not blocks:
        blocks = [text]
    shots = []
    for b in blocks:
        cur = ""
        for s in re.split(r"(?<=[。！？!?…；;])", b.replace(chr(10), "")):
            if not s.strip():
                continue
            if cur and len(cur) + len(s) > 60:
                shots.append(cur)
                cur = s
            else:
                cur += s
        if cur:
            shots.append(cur)
    scenes = []
    for s in shots:
        sec = max(5, min(15, 5 + (len(s) // 60) * 5))
        scenes.append({"seconds": sec, "length": seconds_to_length(sec), "prompt": s, "subtitle": ""})
    return scenes


AI_SPLIT_SYS = (
    "你是资深影视分镜指导，为 AI 视频模型（MiniMax H3，单镜 5-15 秒）设计可直接拍摄的分镜。\n"
    "【导演层：先诊断，再拆】拆之前先看这场戏立不立得住：主角想要什么（欲望）、什么挡着他（障碍）、"
    "人物在空间里的高低远近关系、镜头要把观众的视线引到哪、这场戏的呼吸快慢。"
    "哪一样缺了就按合理判断补足，再动手拆——缺要素的场次拆出来只是装饰品。\n"
    "【节拍图：决定拆几镜、每镜多久】按「钩子 → 压力 → 裂痕 → 冲击 → 余波」分配时长："
    "钩子 10-15%（抓注意力）、压力 30-40%（建立冲突）、裂痕 15-20%（情绪转折）、"
    "冲击 15-20%（顶点或反转）、余波 10-15%（释放回味）。镜头数量与时长照着这个比例走。\n"
    "【切点优先级（Murch 六规则）】切在哪由这个权重决定：情感 51% > 故事 23% > 节奏 10% > "
    "视线追踪 7% > 轴线 5% > 三维空间 4%。铁律是切在情绪点上，不为节奏而切——"
    "纯为卡点切的镜头是废的。\n"
    "【情绪越重，景别越近】大远景=孤独渺小，全景=交代关系与空间，中景=正常叙事，"
    "中近景=对话情绪，近景=亲密与对峙，特写=情绪顶点，大特写=决定性瞬间。\n"
    "【运镜必须回答「镜头为什么动」】答不出来就用固定镜头。不为情绪服务的运镜是炫技。\n"
    "【表演要长在身体上】不要写「他很伤心」这类空词，落到微表情（嘴角/眉头/眼皮）、"
    "身体朝向、呼吸节奏。委屈不是皱眉，是下巴绷紧、喉结滚动、呼吸变浅。\n"
    "【台词 100% 保真】subtitle 里的台词必须与原稿一字不差，不漏字、不改字、不润色。\n"
    "【第一优先：忠于原文】用户写到的场景、时间、天气、人物、动作、物件，必须原样使用，"
    "一个都不能换。用户说「雨夜茶馆」就不许写成澡堂、老巷、木工房；说「她端起杯子又放下」就不许换成别的动作。"
    "场景名、时间、天气、核心动作与用户输入不一致的镜头是废品。\n"
    "【输入多短，输出就多短】用户只给一两句话时，只拆 1-3 镜，全部围绕那件事本身起承转合；"
    "不要另外编一个故事，不要补充用户没提的第二场景、第二人物。用户给的越长，才允许拆得越多。\n"
    "【第一步：通读全稿，把实体全列出来】动手拆镜之前先把稿子从头读到尾，数清里面出现过的东西，"
    "写进 assets：人物（kind=character）、场景与地点（kind=scene）、反复出现或需要固定外观的道具/设备"
    "（kind=equipment，例：那张蜡笔画、灶台、面碗）。只列真的要跨镜头锁住外观的：人物、主要场景、"
    "反复出现的关键道具；出现过一次的小物件（一根烟、一只杯子）不要列，列了只会把资产池塞满。\n"
    "name 必须用稿子里出现过的称呼（「姑娘」「老陈」「面馆」），不要另起名字——镜头就是按这个名字连到你这条资产的；"
    "description 只写外观与固定特征（年龄、发型、服装、材质、颜色、空间陈设、光源），"
    "不写动作、不写剧情、不写运镜，它会被原样注入每一镜的提示词，用来锁住同一个人、同一个地方。"
    "稿子里没出现过的实体不要编；同一个人/地方只列一次。\n"
    "【工作方法】通读的同时定下主题与情绪基调、情绪曲线的起承转合；"
    "再按叙事节点切镜，每个镜头只承担一个明确任务（建置/推进/反应/转折/收尾）；最后逐镜写画面。\n"
    "【用户没写的才归你】光线、构图、环境细节、材质、景深、情绪推进——这些用户通常不写，可以补；"
    "但补的是细节，不是事实。\n"
    "【三个固定字段，单独填，不要塞进 prompt】\n"
    "subject：这一镜的主体是什么——谁或什么物件，外观特征写清楚（颜色、材质、穿着、状态）。"
    "同一个主体在全片每一镜里这段文字必须一字不差地重复，不许换说法。\n"
    "environment：这一镜的环境与背景——地点、背景的颜色与材质、环境里固定的陈设。"
    "原稿写「纯白背景」就照抄「纯白背景」，不许改成别的底色或别的场景。"
    "同一场景里的连续几镜，这段文字必须一字不差。\n"
    "lighting：这一镜的光线，必须交代三件事，缺一不可——"
    "①光源：光从哪来，而且必须是这场戏里真实存在、镜头里看得见或能解释的东西"
    "（吊灯、窗、灶火、霓虹招牌、手机屏幕、路灯、台灯、水面反光），不许凭空打光；"
    "②方向：顺光/侧光/侧逆光/逆光/顶光/底光；"
    "③光质与色温：硬光还是柔光，冷还是暖，高反差还是低反差。"
    "禁止写「漂亮的光线」「电影感光线」这类没有信息量的词。时间与天气决定光："
    "正午是硬顶光、黄昏是低角度暖光、阴天是柔和平光、夜景靠画内实用光源。"
    "同一场景的连续几镜，光源、方向与色温必须一致，只有剧情真的换了时间或地点才允许换。\n"
    "【prompt 怎么写】用一段连贯的自然语句白描这一镜的画面，60-100 字，只写本镜特有的内容，依次交代："
    "主体在做什么动作（写动作的瞬间，不用「似乎」「正在」这类模糊词）；构图"
    "（三分法/中心构图/前景遮挡/框架式/引导线）；前景与背景的环境细节、材质、道具；景深虚实。\n"
    "【prompt 不要写】不要写「主体与外貌：」「光线：」这类标签，也不要分点列条，直接写成一段画面描述；"
    "不要重复全片固定的角色长相与场景设定（系统会统一注入，重复写只会让模型跑偏）；"
    "**不要重复 subject / environment / lighting 的内容**（它们会被单独注入，重复写会让模型跑偏）；"
    "不要写景别与运镜（由 shot/camera 字段给出，系统自动拼接）；"
    "不要写风格套话（电影感/8K/大片等，系统自动追加）；不要写镜头编号与时间码。\n"
    "【参考写法】例：老陈站在灶台前，双手把面团按在案板上反复揉压，袖口沾着白面，"
    "侧后方的吊灯在他肩头拉出一道暖黄轮廓光，蒸汽在灯下浮成薄雾，前景是半碗没动过的面，背景桌椅虚化。\n"
    "【shot 只能取】远景/全景/中景/近景/特写/大特写。\n"
    "【camera 只能取】固定镜头/缓慢推近/缓慢拉远/水平摇镜/垂直摇镜/跟随镜头/手持晃动/环绕运镜。\n"
    "【角度是权力关系，不是审美选择】仰视=被摄者强、观众弱（压迫感、敬畏）；俯视=被摄者弱、被观众压制"
    "（可怜、被审判）；平视=平等客观、不带判断；过肩=带出对峙双方的关系与视线方向；"
    "鸟瞰=命运感、渺小、被环境吞没。角度必须与这一镜的情绪立场一致——这一镜是在压制、在屈服、"
    "还是在平视对峙，角度照着选。同一场戏里角度要有变化，不要全程平视。\n"
    "【angle 只能取】平视/仰视/俯视/过肩/鸟瞰。\n"
    "【景别序列（照套路排，不要平铺）】开场建立用「大远景 → 中景 → 特写」；"
    "揭示反转用「特写 → 拉远至全景」；对话场景用「中景 → 正反打特写 → 双人中景」；"
    "动作段落用「中景 → 手持跟拍 → 快速甩镜」。\n"
    "【按情绪配镜头】紧张对话用 中近景+三分线+微推；史诗开场用 大远景+航拍+慢推；"
    "恐怖或揭示用 极特写+摇+荷兰角；动作追击用 中景+手持+跟拍；"
    "安静抒情用 中近景+中心对称+慢推；收尾留白用 大远景+固定。\n"
    "【动作戏不要用「打」一笔带过】写清武器、对手数量、动作序列"
    "（例：「右拳直击最近一人下颚，对方头部侧仰」，不要写「主角打过去」）。\n"
    "【景别与运镜必须变化】绝对不允许所有镜头用同一个景别、同一个运镜。"
    "写之前先规划一遍：一条片子至少要覆盖四种景别、三种运镜。"
    "建置镜用远景或全景，叙事用中景或近景，情绪和细节用特写或大特写；"
    "运镜也要交替，固定、推近、跟随、摇镜轮着用，不要连续两个镜头同一个组合。\n"
    "【变化示范】同一场戏的正确节奏：远景固定 → 中景固定（建置）→ 近景缓慢推近（发现）"
    "→ 特写固定（细节）→ 中景跟随（动作）→ 近景固定（对白）→ 特写缓慢推近（情绪）"
    "→ 全景固定（收尾留白）。照着这个节奏感分配，不要八个镜头全是中景固定。\n"
    "【时长】seconds 只能是 5、8、10、15 之一：建置空镜与过渡用 5；常规叙事与对话用 5 或 8；"
    "情绪递进、完整对话回合、调度复杂的用 10 或 15。镜头数宁少勿碎，"
    "同一个场景里的连续动作尽量用一个镜头完成，不要拆成好几镜。\n"
    "【台词与声音】台词写进 prompt，格式「画外音：原话」或「角色名说：原话」；"
    "音效与环境声写进 prompt，格式「音效：…」；同时把台词原话一字不改填进 subtitle"
    "（只填原话，不要带「画外音：」「角色名说：」这类前缀），无台词则填空字符串。\n"
    "【屏幕文字】短信、消息、标题、招牌等画面上的文字只放 subtitle，绝对不要写进 prompt（模型画中文会乱码）。\n"
    "【连续性】相邻镜头轴线一致（人物朝向、视线方向不跳），动作无缝衔接，服装道具不变。\n"
    '只输出 JSON，不要任何解释：{"assets":[{"kind":"character|scene|equipment","name":"稿子里的称呼",'
    '"description":"外观与固定特征"}],"scenes":[{"seconds":5,"shot":"中景","camera":"固定镜头",'
    '"angle":"平视","subject":"…","environment":"…","lighting":"…","prompt":"…","subtitle":"…"}]}'
)


# 「拆资产」agent 的提示词：只列实体，不拆镜。分镜已经拆好的项目也能跑——把分镜倒成
# 一段文本喂给它（见 _scenes_text），所以剧本丢了也还能把资产补回来。
AI_ENTITY_SYS = (
    "你是影视制片里的资产统筹。用户给你一段剧本或一份分镜，你把里面要跨镜头保持一致的东西"
    "全部找出来，列成资产表。\n"
    "【分三类】character＝人物；scene＝场景／地点；equipment＝反复出现或需要固定外观的道具与设备"
    "（例：那张蜡笔画、灶台、面锅、烤红薯）。\n"
    "【名字用原文的称呼，一字不改】「姑娘」「老陈」「面馆」——镜头就是按这个名字连到你这条资产的，"
    "换成「女孩」「店主」「小面馆」就连不上，那一镜就锁不住人。\n"
    "【描述只写外观与固定特征】年龄、性别、发型、服装、材质、颜色、空间陈设、光源。"
    "不写动作、不写剧情、不写情绪、不写运镜——这段文字会被原样注入每一镜的提示词，"
    "它唯一的任务就是让同一个角色、同一个地方在每一镜里长得一样。\n"
    "【列多少】人物一个不漏，哪怕只出场一次。场景按地点合并：「面馆」「面馆门口」「巷口的面馆」"
    "是同一个地方，只列一条，用最短、出现最多的那个称呼；同一个地点的不同时段也算同一个场景。"
    "道具只列要在镜头之间反复出现、并且需要锁住外观的（那张蜡笔画、那口面锅）；"
    "场景里的固定陈设（桌椅、灯泡、瓷砖、水洼）属于那个场景的一部分，不要单独列成道具；"
    "只出场一次的小物件（一根烟、一只杯子）也不要列——多列一条，资产池里就多一条会往每镜提示词里塞东西的资产。\n"
    "【只列稿子里有的】没出现过的人、地点、道具一律不要编；同一个人／地方只列一次。\n"
    '只输出 JSON，不要解释：{"assets":[{"kind":"character|scene|equipment","name":"原文里的称呼",'
    '"description":"外观与固定特征"}]}'
)


# ---- 变化帧的细度分级 --------------------------------------------------------------
#
# 「细致到每一个变化」在 H3 上有个硬上限：单次生成只能 4-15 秒，所以一个镜头最多拆成
# floor(时长 / 4) + 1 个变化帧 —— 8 秒镜头最多 3 帧，15 秒最多 4 帧。硬拆更多只会得到
# 短于模型训练下沿的片段，画质反而崩。分级表把这笔账写明，由调用方按镜头时长挑。
STATES_GRANULARITY = {
    "coarse": "只取最关键的两三个状态，中间过程交给生成模型自己补",
    "normal": "取几个关键状态",
    "fine":   "把画面每一次实质变化都单独切出来，一个变化一帧，不许合并",
}


def states_system(level="fine"):
    """按细度拼变化帧的系统提示词。"""
    how = STATES_GRANULARITY.get(level, STATES_GRANULARITY["fine"])
    return (
        "你是影视分镜师。用户给你一个镜头段落的描述，"
        "你要把它拆成「变化帧」——这个连续动作与运镜过程中画面发生实质变化的状态。\n"
        "【拆多少帧】" + how + "。判断标准就是「画面有没有变」：主体换了一个动作、"
        "手/头/身体移到了新位置、表情变了、画面里多了或少了一样东西 —— "
        "每出现一次这样的变化才是一帧；只是镜头换了个景别、同一处细节又推近一档，不算变化。\n"
        "帧数上限在用户消息里给了，那是**上限不是指标**：这个镜头有几次真实变化就拆几帧，"
        "一次都没有就只拆 1 帧。\n"
    ) + AI_STATES_BODY


# 帧这一层照 visual-video-director 的关键帧章法写（animatic-keyframes.md + universal-rules.md）：
# 一帧是一张要在一眼之内读懂的画面，所以「只讲一件事、只留一个重点、动作停在势上」；
# 运镜与光线照 cinematic-video-prompt 的规矩收窄成画面里看得见、说得清的东西。
AI_STATES_BODY = (
    "【最关键的要求】把整段拆成一串连续画面状态：后一个状态要能接上前一个状态的结束画面，"
    "连起来看是一段流畅的镜头（例如「拿起水杯喝水」拆成：远景桌上的水杯 → 推近到握住水杯的手 → 上移到喝水者的面部特写）。\n"
    "【一帧只讲一件事】写之前先用一句话说清这一帧在干什么（例：「她端着杯子的手停在嘴边」），"
    "说不出来就是凑数的帧。画面里只留一个重点：最要紧的那样东西（脸、手、物件）放在最清楚的位置，"
    "前景、主体、背景三层分开写清（原镜头描述里有的照抄，不添新东西），背景要比主体虚一点。"
    "三层都同样清楚，等于没有重点。\n"
    "【动作停在看得出下一步的姿势上】这一帧要拿去当视频的首尾帧，画面必须是干净的定格："
    "不写晃动、模糊、拖影。运动靠姿势交代——动作只做到一半（手抬到一半、重心已经压到前脚、"
    "身体前倾半寸、杯子刚离开桌面），看一眼就知道接下来往哪走。\n"
    "【情绪长在身体和物件上】不许写「紧张」「难过」「生气」这类情绪词——写身体：下颌绷紧、"
    "喉结滚动、呼吸变浅、指节发白；或者写物件正在变的那个状态：杯里的水面在晃、烟灰将断、"
    "门缝的光被挡住。没在变的物件是摆设，别写。\n"
    "【背景必须原样沿用，每帧都要写明】原镜头描述里的环境、背景颜色与材质、光线方向，"
    "每一帧照搬，一个字都不许换：原镜头写「纯白背景」，那每一帧的 prompt 里都必须出现"
    "「纯白背景」，不许变成黑色背景、深色背景或别的场景。"
    "而且背景不能省略不写——漏写背景等于让生成模型自己编一个，帧与帧之间就会换底。\n"
    "【帧间只变这些】相邻变化帧之间只允许变：主体的动作、重点对象、景别、构图。"
    "环境、背景、光线、色调、同一批道具必须完全一致，连在一起看才像同一个镜头。\n"
    "【一帧要带来一件新的事】相邻两帧之间必须有上一帧里看不到的东西：新的动作（手从攥着到抬起来）、"
    "新的对象（从人的上半身切到手里那张画）、新的位置（人从画面偏左走到桌前）。"
    "把同一件东西拍得更近、同一颗水珠又往下掉一点，都不是新的一帧——那是同一幅画，合并掉。\n"
    "【上一帧写过的东西，这一帧要么不写，要么写它的结果】上一帧已经写了「睫毛上挂着水珠」，这一帧"
    "就不许再写「水珠往下坠」——同一处细节连着两帧，观众看到的是同一幅画动了动；要写就写它落地了、"
    "消失了、位置换了。同一个微表情（嘴角、眼皮、喉咙）一镜里只准当一次重点，"
    "「嘴唇动了动 → 喉咙滚一下」这种面部小动作的堆叠不算两次变化，合成一帧。\n"
    "【同一件东西只准当一次重点】发梢那颗水珠、攥纸的那只手、手里那张画，各自只出一帧；"
    "重点对象一路要换：人 → 手或物件 → 表情或环境。连着两帧盯同一处细节的，删掉后面那帧。\n"
    "【光只有一个来源，而且看得见】每帧写明光源是什么（窗、吊灯、灶火、屏幕、霓虹、路灯）、"
    "从哪个方向来、是硬光还是柔光。光源必须是这场戏里真实存在、画面里解释得通的东西，"
    "不许凭空打光，也不许写「漂亮的光线」这类空话。\n"
    "【画面里不要中文】不要在 prompt 里要求画面出现中文文字（气泡、招牌、标签都算），"
    "生成模型画中文一定乱码；需要文字就写成英文字符，或者留到成片时用字幕叠加。\n"
    "【画面重复的两帧是废品】每一帧在成片里都是独立的一段画面，内容一样的两帧，"
    "观众看到的就是同一幅画停了两次。所以每一帧的 prompt 必须和上一帧有肉眼可见的差别，"
    "而且你能一句话说出差在哪（手指移到了新位置、水珠落下了、表情变了、画面里多了或少了一样东西）。"
    "说不出差别的帧就是废帧，删掉。特别禁止两种凑数：把原镜头描述按句号拆开、换个顺序轮流贴"
    "——那是同一幅画不是变化；把同一句话复制几遍、只改 shot 和 camera。\n"
    "【静止的镜头就只出 1 帧】如果这个镜头本身没有变化（空镜、固定机位、没有人物动作、"
    "没有物体位移），就只输出 1 帧，把画面写完整。宁可少给帧，也不要用重复帧凑数。\n"
    "【最后一帧是这一镜的落点】最后一个变化帧要把这一镜的结果交代清楚——动作做完的样子、"
    "情绪的落点；前面几帧不许提前把它演完。\n"
    "【输出前自检】逐帧过一遍：画面内容一样的，删掉后面那帧；能用一句话说清这一帧在干什么、"
    "重点只有一处、情绪都落在身体或物件上、光源写清了，这一帧才留下。"
    "再把每帧那句话说成一列读一遍：读起来是同一件事的，只留信息量最大的那一帧。"
    "删完剩下的帧数才是最终答案，可以远少于上限。\n"
    "【每个变化帧写清】\n"
    "prompt：这一帧画面里能看到的东西——主体、动作的瞬间、环境、背景、光线。"
    "绝对不要出现「镜头」「推进」「拉远」「摇」「跟随」「移动」这类运镜词，也不要写景别（景别和运镜由 shot/camera 字段负责）。\n"
    "shot：这一帧的景别，只能取 远景/全景/中景/近景/特写/大特写。\n"
    "angle：这一镜的拍摄角度，只能取 平视/仰视/俯视/过肩/鸟瞰（仰视=压迫、俯视=被压制）。\n"
    "camera：从这一帧推进到下一帧所用的运镜，只能取 固定镜头/缓慢推近/缓慢拉远/水平摇镜/垂直摇镜/跟随镜头/手持晃动/环绕运镜。"
    "一帧只配一个主运镜，答不出「镜头为什么动」就用固定镜头；相邻两帧不要连着用同一种运镜。"
    "用户描述里说到的运镜方向必须照做：说「推进/推近」就用缓慢推近，说「拉远/拉出」才用缓慢拉远，说「上移/抬起来」用跟随镜头，不要反过来。\n"
    "seconds：从这一帧推进到下一帧需要几秒，整数 3-8。"
    "只有前 N-1 个变化帧的 seconds 会用来生成视频片段，它们加起来要接近用户给的总时长；"
    "最后一个变化帧没有下一帧，seconds 不影响生成，照常填一个数即可。\n"
    "subtitle：台词或旁白原话，没有就填空字符串。\n"
    '只输出 JSON，不要解释：{"states":[{"prompt":"…","shot":"中景","camera":"缓慢推近",'
    '"angle":"平视","seconds":4,"subtitle":""}]}'
)


def _state_chars(text):
    """归一化一帧的画面描述：只留实义字符，标点和空格不参与比较。"""
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text or "")


def _states_dedupe(states):
    """丢掉画面内容重复的变化帧。

    模型凑帧有固定的两个套路：把原镜头描述按句号拆开、换个顺序轮流贴；或者干脆把同一句
    复制几遍，只改 shot 和 camera。两种都会让成片里同一幅画停两次。提示词里已经写明了
    禁止，但模型照样会犯，所以这层兜底必须留在代码里。

    判重只看画面描述的字符集合：一帧被前面某帧整段包住（拆句轮流贴），或者两帧的字重合度
    超过 0.95（复制时抄错一两个字），都算同一幅画。阈值定得高是故意的——每帧都要原样
    重复同一段背景和光线，共同部分本来就长，阈值低了会把真的变化误判成重复。
    """
    kept, keys = [], []

    def same(a, b):
        if a == b:
            return True
        short, long = (a, b) if len(a) <= len(b) else (b, a)
        # 太短的描述不做包含判断，否则「空镜」这种两字帧会被长帧误伤
        if len(short) >= 12 and short in long:
            return True
        union = set(a) | set(b)
        return bool(union) and len(set(a) & set(b)) / len(union) >= 0.95

    for s in states:
        key = _state_chars(s.get("prompt"))
        if not key:
            continue
        if any(same(key, k) for k in keys):
            continue
        kept.append(s)
        keys.append(key)
    return kept


def _ai_states_from_json(content):
    """模型返回的 JSON 文本 -> 变化帧列表；不可解析返回 None。"""
    try:
        data = json.loads(re.search(r"[\[{].*[\]}]", content, re.S).group(0))
    except Exception:
        return None
    raw = data.get("states") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return None
    states = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        prompt = str(s.get("prompt") or "").strip()
        if not prompt:
            continue
        try:
            sec = int(float(s.get("seconds") or 4))
        except (TypeError, ValueError):
            sec = 4
        shot = str(s.get("shot") or "").strip()
        camera = str(s.get("camera") or "").strip()
        angle = str(s.get("angle") or "").strip()
        states.append({"prompt": prompt,
                       "shot": shot if shot in SHOT_SIZES else "中景",
                       "angle": angle if angle in CAMERA_ANGLES else "平视",
                       "camera": camera if camera in CAMERA_MOVES else "固定镜头",
                       "seconds": max(3, min(15, sec)),
                       "subtitle": str(s.get("subtitle") or "").strip()})
    # 和拆镜、拆资产一样返回 (结果, 附加)：引擎包装统一按两元组解包
    return (states or None), []


def _ollama_chat_model():
    """本机可用的对话模型：优先 OLLAMA_CHAT_MODEL，否则取 Ollama 里第一个非向量模型。"""
    global _AI_MODEL
    if _AI_MODEL:
        return _AI_MODEL
    try:
        with urllib.request.urlopen(_OLLAMA_URL.rstrip("/") + "/api/tags", timeout=5) as r:
            names = [m.get("name") or "" for m in json.load(r).get("models", [])]
    except Exception:
        return None
    if _OLLAMA_CHAT_MODEL in names:
        _AI_MODEL = _OLLAMA_CHAT_MODEL
    else:
        _AI_MODEL = next((n for n in names if "bge" not in n and "embed" not in n), "")
    return _AI_MODEL or None


def _entity_list(data):
    """从模型返回的 JSON 里取实体表，认不出的条目直接丢掉。"""
    raw = (data or {}).get("assets") if isinstance(data, dict) else data
    out = []
    for e in (raw or []):
        if not isinstance(e, dict):
            continue
        kind = str(e.get("kind") or "").strip()
        name = str(e.get("name") or "").strip()
        if kind not in ASSET_SPECS or not name:
            continue
        out.append({"kind": kind, "name": name,
                    "description": str(e.get("description") or "").strip()})
    return out


def _ai_entities_from_json(content):
    """拆资产 agent 的解析：返回 (实体列表, [])，形状和拆镜那条路一致。"""
    try:
        data = json.loads(re.search(r"[\[{].*[\]}]", content, re.S).group(0))
    except Exception:
        return None, []
    return (_entity_list(data) or None), []


def _ai_scenes_from_json(content):
    """模型返回的 JSON 文本 -> (分镜列表, 实体列表)；不可解析返回 (None, [])。

    实体是拆镜时通读全文顺带列出来的人物/场景/道具，直接进资产池：先有资产，镜头再按
    名字连上去，两个人物同片才不会串脸、背景才不会一镜换一个。
    """
    try:
        data = json.loads(re.search(r"[\[{].*[\]}]", content, re.S).group(0))
    except Exception:
        return None, []
    entities = _entity_list(data)
    if isinstance(data, dict):
        raw = data.get("scenes")
    else:
        raw = data
    if not isinstance(raw, list):
        return None, entities
    scenes = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        prompt = str(s.get("prompt") or "").strip()
        if not prompt:
            continue
        try:
            sec = int(float(s.get("seconds") or 5))
        except (TypeError, ValueError):
            sec = 5
        sec = max(5, min(15, sec))
        shot = str(s.get("shot") or "").strip()
        camera = str(s.get("camera") or "").strip()
        angle = str(s.get("angle") or "").strip()
        # 这里以前是白名单式构造，只搬 seconds/length/shot/camera/prompt/subtitle ——
        # 模型填的 angle 和后来的 subject/environment/lighting 全被静默丢掉，
        # 表现就是「提示词里明明要求了，拆出来永远是空的」。
        scenes.append({"seconds": sec, "length": seconds_to_length(sec),
                       "shot": shot if shot in SHOT_SIZES else "中景",
                       "camera": camera if camera in CAMERA_MOVES else "固定镜头",
                       "angle": angle if angle in CAMERA_ANGLES else "平视",
                       "subject": str(s.get("subject") or "").strip(),
                       "environment": str(s.get("environment") or "").strip(),
                       "lighting": str(s.get("lighting") or "").strip(),
                       "prompt": prompt, "subtitle": str(s.get("subtitle") or "").strip()})
    return (scenes or None), entities


def _split_user_message(text):
    """拆镜请求的用户消息。

    硬约束放用户消息里，而不是只写进系统提示词：系统提示词已经很长（景别取值、时长取值、
    节奏、连续性、屏幕文字……），第一条「忠于原文」会被稀释掉。用户消息紧贴生成，
    是模型最后看到、也最听话的一句。两种引擎共用。
    """
    t = (text or "").strip()
    if not t:
        return t
    return ("请把下面这段内容拆成分镜。先把全稿通读一遍，把里面的人物、场景、道具/设备都找出来列进 "
            "assets（名字用稿子里的称呼），再拆镜；漏掉一个反复出现的人物或场景，成片里它就会一镜一个样。\n"
            "原封不动地使用其中写到的场景、时间、天气、人物、动作和物件，一个都不要替换成别的；"
            "用户没写的细节才可以补。原文越长才允许拆得越多，原文只有一两句就只拆 1-3 镜。\n"
            "另外：景别和运镜必须有变化——建置用远景或全景，叙事用中景或近景，情绪和细节用"
            "特写；运镜也要轮换，固定、推近、跟随、摇镜交替用。不要所有镜头都写成中景加固定"
            "镜头，那样剪出来是死的。\n"
            "还有：lighting 不许写空话——光源必须是这场戏里真实存在、镜头里看得见的东西"
            "（吊灯、窗、灶火、霓虹招牌、手机屏幕、路灯），要写清「光源 + 方向 + 硬光还是柔光」，"
            "不要写「漂亮的光线」。angle 只取 平视/仰视/俯视/过肩/鸟瞰，并跟着情绪走"
            "（压制别人用仰视，被压制用俯视），不要整场戏清一色平视。\n\n" + t)


def _entity_user_message(text):
    """拆资产的用户消息：紧贴生成的那一句只说要资产，别让它顺手把镜也拆了。"""
    return ("请通读下面这段内容，把它里面要跨镜头保持一致的东西列成资产表（人物／场景／道具）。"
            "只输出 assets，不要拆镜、不要写分镜。名字用原文里的称呼，描述只写外观。\n\n"
            + (text or "").strip())


def _ai_split_ollama(text, sys=AI_SPLIT_SYS, parse=_ai_scenes_from_json, um=None):
    """本机 Ollama 拆镜/拆变化帧，返回 (结果, 引擎名)。"""
    model = _ollama_chat_model()
    if not model:
        raise RuntimeError("本机 Ollama 没有可用的对话模型")
    body = {
        "model": model, "stream": False, "think": False, "format": "json", "keep_alive": _OLLAMA_KEEP_ALIVE,
        "messages": [{"role": "system", "content": sys},
                     {"role": "user", "content": (um or _split_user_message)(text)}],
        "options": {"temperature": 0.3, "num_ctx": 8192, "num_predict": AI_SPLIT_MAX_TOKENS},
    }
    try:
        req = urllib.request.Request(
            _OLLAMA_URL.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=AI_SPLIT_TIMEOUT) as r:
            content = json.load(r)["message"]["content"]
    except Exception as e:
        raise RuntimeError("本地 Ollama 调用失败：" + str(e))
    payload, extra = parse(content)
    if not payload:
        raise RuntimeError("本地 Ollama（%s）没有返回可解析的 JSON" % model)
    return (payload, extra), "本地 Ollama " + model


def _ai_split_deepseek(text, api_key, base_url, model, sys=AI_SPLIT_SYS, parse=_ai_scenes_from_json, um=None):
    """OpenAI 兼容接口的拆镜/拆变化帧，返回 (结果, 引擎名)。

    现在服务 DeepSeek 和通义千问两家——调用方式完全一样，只有地址不同，所以标题里的
    厂商名按地址认，不写死。
    """
    api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
    model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    base_url = base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    name = "通义千问" if "dashscope" in base_url else "DeepSeek"
    if not api_key:
        raise RuntimeError("未配置 %s API Key（在「全局设置 → Agent 模型」里填）" % name)
    try:
        content = _llm_chat(base_url, api_key, model,
                            [{"role": "system", "content": sys},
                             {"role": "user", "content": (um or _split_user_message)(text)}],
                            max_tokens=3000, timeout=300)
    except Exception as e:
        raise RuntimeError(name + " 调用失败：" + str(e))
    payload, extra = parse(content)
    if not payload:
        raise RuntimeError("%s（%s）没有返回可解析的 JSON" % (name, model))
    return (payload, extra), name + " " + model


def ai_split_scenes(text, provider="auto", api_key="", base_url="", model=""):
    """AI 拆镜：本机 Ollama 或 DeepSeek（auto = 本地优先，失败转 DeepSeek）。
    返回 ((scenes, entities), 引擎名)：entities 是模型通读全文列出的实体，调用方把它并进
    资产池再连线。拆不出来返回 ((None, []), 失败原因)，由调用方回退格式规则解析。"""
    text = text.strip()[:AI_SPLIT_MAX_CHARS]

    def deepseek(t):
        return _ai_split_deepseek(t, api_key, base_url, model)

    engines = {"ollama": [_ai_split_ollama], "deepseek": [deepseek]}.get(provider, [_ai_split_ollama, deepseek])
    notes = []
    for fn in engines:
        try:
            return fn(text)
        except Exception as e:
            notes.append(str(e))
    return (None, []), "；".join(notes)


def ai_split_entities(text, provider="ollama", api_key="", base_url="", model=""):
    """剧本或分镜 -> 实体列表。返回 (entities, 引擎名)；失败返回 (None, 失败原因)。

    和拆镜共用同一对引擎包装，只是换成「拆资产」那一套提示词和解析。
    """
    text = (text or "").strip()[:AI_SPLIT_MAX_CHARS]
    if not text:
        return None, "没有可读的文本"

    def ollama(t):
        return _ai_split_ollama(t, AI_ENTITY_SYS, _ai_entities_from_json, _entity_user_message)

    def deepseek(t):
        return _ai_split_deepseek(t, api_key, base_url, model, AI_ENTITY_SYS,
                                  _ai_entities_from_json, _entity_user_message)

    engines = {"ollama": [ollama], "deepseek": [deepseek]}.get(provider, [ollama, deepseek])
    notes = []
    for fn in engines:
        try:
            (items, _extra), label = fn(text)
            return items, label
        except Exception as e:
            notes.append(str(e))
    return None, "；".join(notes)


def ai_split_states(text, provider="ollama", api_key="", base_url="", model="",
                    level="fine", total_sec=0):
    """把一段镜头描述拆成连续的变化帧。返回 (states, 引擎名)；拆不出来返回 (None, 失败原因)。

    level 是细度（coarse/normal/fine）。total_sec 给镜头总时长：H3 单次只能 4-15 秒，
    帧数上限是 floor(总时长/4)+1，超过就会拆出短于训练下沿的片段，所以这个上限要
    直接写进提示词，别让模型自由发挥。
    """
    sys = states_system(level)
    max_frames = (int(total_sec // 4) + 1) if total_sec else 0

    def with_budget(t):
        # 短镜头也要给上限：5 秒镜头最多 2 帧，不讲清楚模型会拆出 4 帧，
        # 每段只剩 1.7 秒，远低于 H3 的 4 秒下沿。
        head = ""
        if max_frames:
            head = ("这个镜头总长约 %d 秒。每段视频最少要 4 秒，所以最多只能拆出 %d 个变化帧"
                    "（%d 段），不要超过这个数。\n" % (int(total_sec), max_frames, max_frames - 1))
        # 这两条最容易被丢：系统提示词一长，模型就先把「别重复」和「写光源」省掉。
        # 放进用户消息（模型最后读到的一段），比留在系统提示词里管用。
        return (head + "拆的时候盯住两条：每帧都要有上一帧里没有的东西——新的动作或新的拍摄对象，"
                "把同一件东西拍得更近、同一颗水珠又往下掉一点，都不算新的一帧；"
                "每帧都要照抄这一镜的光源（门灯/窗/灶火这类画面里看得见的光）、方向、硬光还是柔光，"
                "别只写「暖光」这种没有出处的说法。\n\n" + t)

    def ollama(t):
        return _ai_split_ollama(with_budget(t), sys, _ai_states_from_json)

    def deepseek(t):
        return _ai_split_deepseek(with_budget(t), api_key, base_url, model, sys, _ai_states_from_json)

    def dedupe(result):
        # 引擎包装返回 ((states, 附加), 引擎名)：附加这一格拆帧用不上（实体只在拆镜/拆资产那两条路上）
        (states, _extra), label = result
        if not states:
            return result
        kept = _states_dedupe(states)
        if len(kept) < len(states):
            label += "（%d 帧画面重复，已合并）" % (len(states) - len(kept))
        return kept, label

    engines = {"ollama": [ollama], "deepseek": [deepseek]}.get(provider, [ollama, deepseek])
    notes = []
    for fn in engines:
        try:
            return dedupe(fn(text))
        except Exception as e:
            notes.append(str(e))
    return None, "；".join(notes)


def new_scene_defaults():
    return {"shot": "中景", "camera": "固定镜头", "angle": "平视",
            "subject": "", "environment": "", "lighting": "", "aspect": "16:9", "resolution": "480p", "mode": "标准", "steps": 25, "seed": None, "negative_prompt": "", "model": "MiniMax H3", "second_pass": False}


def add_version(slot, field, files, kind="", seed=None):
    """把一次生成的产物作为「衍生节点」追加进历史，而不是覆盖。

    LibTV 的节点工作流是「每次生成分叉出新节点、多版本并存」，这里用两个数组表达：
    <field>_batches 记录「第几次生成、产出了哪几张」（画布上渲染成一个个宫格衍生节点），
    <field>_versions 是扁平版本清单（供在节点上来回切换）。
    """
    batches = slot.setdefault(field + "_batches", [])
    batches.append({"n": len(batches), "ts": time.strftime("%H%M%S"), "files": list(files),
                    "kind": kind, "seed": seed})
    vers = slot.setdefault(field + "_versions", [])
    for f in files:
        if f not in vers:
            vers.append(f)
    return batches[-1]


# ---- 状态持久化 ----
def _current_project_id():
    try:
        return str((json.load(open(CURRENT_PROJECT_FILE, encoding="utf-8")) or {}).get("id") or "default")
    except Exception:
        return "default"


def _state_path():
    pid = _current_project_id()
    if pid == "default":
        return STATE_FILE
    return os.path.join(PROJECTS_DIR, pid + ".json")


def list_projects():
    """列出全部项目：默认项目(state.json) + projects 目录，最近更新在前。"""
    out = []
    try:
        st = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        st = {}
    def_scenes = st.get("scenes") or []
    out.append({"id": "default", "name": st.get("name") or "默认项目",
                "scenes": len(def_scenes),
                "done": sum(1 for s in def_scenes if s.get("output")),
                "updated": int(os.path.getmtime(STATE_FILE)) if os.path.isfile(STATE_FILE) else 0})
    if os.path.isdir(PROJECTS_DIR):
        for f in sorted(os.listdir(PROJECTS_DIR)):
            if not f.endswith(".json"):
                continue
            pid = f[:-5]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", pid):
                continue
            p = os.path.join(PROJECTS_DIR, f)
            try:
                st = json.load(open(p, encoding="utf-8"))
            except Exception:
                continue
            pscenes = st.get("scenes") or []
            out.append({"id": pid, "name": st.get("name") or pid,
                        "scenes": len(pscenes),
                        "done": sum(1 for s in pscenes if s.get("output")),
                        "updated": int(os.path.getmtime(p))})
    out.sort(key=lambda x: x["updated"], reverse=True)
    return out


def _migrate_assets(st):
    """把旧的 character/scene/equipment 三个单例并进资产池，只做一次。

    三个槽位是「每类只能有一个资产」时代的产物。并进来之后它们就是池子里 id 与类型同名的
    三条记录，全局设置面板继续编辑这三条——编辑的是池子里那一条，不再是第二份数据。
    """
    for kind in ASSET_SPECS:
        slot = st.pop(kind, None)
        if not isinstance(slot, dict):
            continue
        has_image = str(slot.get("image") or "").strip()
        has_text = str(slot.get("name") or "").strip() or str(slot.get("description") or "").strip()
        if not (has_image or has_text):
            continue
        a = _find_asset(st, kind)
        if a is None:
            a = {"id": kind, "kind": kind, "name": "", "description": "", "file": None,
                 "views": {}, "candidates": [], "tags": []}
            st.setdefault("assets", []).append(a)
        a["name"] = a.get("name") or str(slot.get("name") or "").strip()
        a["description"] = a.get("description") or str(slot.get("description") or "").strip()
        a["file"] = a.get("file") or (has_image or None)
        a["candidates"] = a.get("candidates") or (slot.get("candidates") or [])
        a["views"] = a.get("views") or (slot.get("views") or {})


def _asset_of_kind(st, kind):
    """某一类的第一条资产。全局设置面板里的角色/场景/设备编辑的就是这条记录。"""
    for a in (st.get("assets") or []):
        if a.get("kind") == kind:
            return a
    a = {"id": kind, "kind": kind, "name": "", "description": "", "file": None,
         "views": {}, "candidates": [], "tags": []}
    st.setdefault("assets", []).append(a)
    return a


def _asset_for_write(st, body):
    """端点要写的那条资产：asset_id 优先；只给 kind 的就取该类第一条（没有就现建一条，
    全局设置面板里的角色/场景/设备按钮还在走这条路）。"""
    a = _find_asset(st, str(body.get("asset_id") or ""))
    if a is not None:
        return a
    kind = str(body.get("kind") or "").strip()
    return _asset_of_kind(st, kind) if kind in ASSET_SPECS else None


def _asset_slot(st, kind):
    """给全局设置面板看的槽位视图：池子里该类型的第一条，没有就返回空槽。"""
    a = next((x for x in (st.get("assets") or []) if x.get("kind") == kind), None) or {}
    return {"name": str(a.get("name") or ""), "description": str(a.get("description") or ""),
            "image": a.get("file"), "candidates": a.get("candidates") or [],
            "views": a.get("views") or {}}


def load_state():
    try:
        st = json.load(open(_state_path(), encoding="utf-8"))
    except Exception:
        st = {}
    st.setdefault("scenes", [])
    st.setdefault("assets", [])
    st.setdefault("global_negative_prompt", "")
    _migrate_assets(st)
    # 连线每次读盘都重算一遍：画布、关键帧、视频、自动出片看到的是同一份连线，
    # 不用每个调用方各算一次。手工排过引用的镜头（refs_manual）不动。
    link_all_assets(st)
    scenes = st["scenes"]
    accepted = st.get("accepted") or {}
    for sc in scenes:
        # 迁移旧格式 accepted -> scene.output
        if not sc.get("output") and sc.get("id") in accepted:
            sc["output"] = accepted[sc["id"]]
        # 迁移旧输出路径：去掉相对 ComfyUI output 根的 "video/" 前缀，统一相对 OUTPUT_DIR
        out = sc.get("output")
        if out and isinstance(out, str) and out.startswith("video/"):
            sc["output"] = out[len("video/"):]
        # 迁移旧提示词：去掉已内嵌的风格后缀，交给 compose_prompt 统一处理
        p = sc.get("prompt") or ""
        for _sfx in (STYLE_SUFFIX, FLAT_STYLE_SUFFIX):
            if p.endswith(_sfx):
                sc["prompt"] = p[: -len(_sfx)].rstrip("。")
                break
        # 补齐导演参数与几何信息（兼容旧数据）
        for k, v in new_scene_defaults().items():
            sc.setdefault(k, v)
        sc["width"], sc["height"] = resolve_geometry(sc)
    st["scenes"] = scenes
    return st


def save_state(st):
    p = _state_path()
    if p != STATE_FILE:
        os.makedirs(PROJECTS_DIR, exist_ok=True)
    try:
        if os.path.isfile(p):
            shutil.copyfile(p, p + ".bak")
    except OSError:
        pass
    json.dump(st, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


# ---- 智能合成：镜头之间用 H3 首尾关键帧补过渡帧 ----
BRIDGE_LENGTH = 73          # 17k+5 帧网格，约 3.0 秒 @24fps
BRIDGE_STEPS = 25
BRIDGE_CANVAS = (864, 480)  # 必须是 32 的倍数，否则首尾关键帧路径 patchify 会崩
SMART_CONCAT_STATUS = {"running": False, "stage": "", "done": 0, "total": 0,
                       "current": "", "result": None, "error": None, "log": []}


def _ffmpeg_run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def extract_first_frame(src_abs, out_abs):
    r = _ffmpeg_run([_ffmpeg(), "-y", "-i", src_abs, "-frames:v", "1", "-q:v", "2", out_abs])
    if r.returncode != 0 or not os.path.isfile(out_abs):
        raise RuntimeError("抽首帧失败: " + r.stderr[-300:])


def extract_last_frame(src_abs, out_abs):
    r = _ffmpeg_run([_ffmpeg(), "-y", "-sseof", "-0.05", "-i", src_abs,
                     "-frames:v", "1", "-q:v", "2", out_abs])
    if r.returncode != 0 or not os.path.isfile(out_abs):
        raise RuntimeError("抽尾帧失败: " + r.stderr[-300:])


def chain_first_frame(st, scene):
    """自动续接：抽上一镜成片的尾帧，作为本镜首帧。"""
    scenes = st.get("scenes") or []
    idx = next((i for i, s in enumerate(scenes) if s.get("id") == scene.get("id")), -1)
    prev = scenes[idx - 1].get("output") if idx > 0 else None
    src = os.path.join(OUTPUT_DIR, prev) if prev else ""
    if not src or not os.path.isfile(src):
        return None
    frames_dir = os.path.join(INPUT_DIR, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    name = "chain_" + re.sub(r"[^0-9a-zA-Z_-]", "", str(scene.get("id") or "scene")) + "_" + time.strftime("%H%M%S") + ".png"
    extract_last_frame(src, os.path.join(frames_dir, name))
    return "frames/" + name


VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm")
MAX_VIDEO_BYTES = 200 * 1024 * 1024


def _resolve_video_path(rel):
    """前端给的视频相对路径 -> 磁盘绝对路径。只允许 input/videos 与 output 两个目录，越界返回 None。"""
    rel = (rel or "").replace("\\", "/").strip().lstrip("/")
    if not rel or ".." in rel.split("/") or not rel.lower().endswith(VIDEO_EXTS):
        return None
    if rel.startswith("videos/"):
        base, rel = os.path.join(INPUT_DIR, "videos"), rel[len("videos/"):]
    elif rel.startswith("output/"):
        base, rel = os.path.join(_COMFY_ROOT, "output"), rel[len("output/"):]
    else:
        base = OUTPUT_DIR
    base_abs = os.path.abspath(base)
    full = os.path.abspath(os.path.join(base_abs, rel.replace("/", os.sep)))
    if os.path.commonpath([full, base_abs]) != base_abs or not os.path.isfile(full):
        return None
    return full


def make_continue_scene(st, scene_id, video_rel, prompt, seconds, mode, seed):
    """续接视频：抽给定视频的尾帧作新镜首帧，插到源镜头之后并落盘 state。"""
    src = _resolve_video_path(video_rel)
    if not src:
        raise ValueError("视频无效：只支持 input/videos 与 output 下的 mp4/mov/mkv/webm")
    scenes = st.get("scenes") or []
    base = next((s for s in scenes if s.get("id") == scene_id), {}) or {}
    frames_dir = os.path.join(INPUT_DIR, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S") + "_" + str(random.randrange(100, 999))
    name = "cont_" + re.sub(r"[^0-9a-zA-Z_-]", "", str(scene_id or "scene")) + "_" + stamp + ".png"
    extract_last_frame(src, os.path.join(frames_dir, name))
    scene = {
        "id": "cont_" + stamp,
        "prompt": (prompt or base.get("prompt") or "").strip(),
        "seconds": max(3, min(15, int(seconds or base.get("seconds") or 5))),
        "shot": base.get("shot") or "中景",
        "camera": base.get("camera") or "固定镜头",
        "aspect": base.get("aspect") or "16:9",
        "resolution": base.get("resolution") or "480p",
        "mode": mode or base.get("mode") or "标准",
        "steps": (MODES.get(mode or base.get("mode") or "标准") or MODES["标准"])["steps"],
        "seed": int(seed) if seed else None,
        "subtitle": "",
        "negative_prompt": base.get("negative_prompt") or "",
        "first_frame": "frames/" + name,
        "output": None,
    }
    idx = next((i for i, s in enumerate(scenes) if s.get("id") == scene_id), len(scenes) - 1)
    scenes.insert(idx + 1, scene)
    st["scenes"] = scenes
    save_state(st)
    return scene


def build_bridge_graph(first_frame, last_frame, prompt, seed, prefix, need_audio=True):
    w, h = BRIDGE_CANVAS
    graph = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": MODELS["clip"], "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["vae_video"]}},
        "5": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {
            "clip": ["2", 0], "vae": ["3", 0], "prompt": prompt,
            "width": w, "height": h, "length": BRIDGE_LENGTH,
            "first_frame": ["17", 0], "last_frame": ["18", 0]}},
        "17": {"class_type": "LoadImage", "inputs": {"image": first_frame}},
        "18": {"class_type": "LoadImage", "inputs": {"image": last_frame}},
        "15": {"class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch", "inputs": {"model": ["1", 0]}},
        "6": {"class_type": "BasicGuider", "inputs": {"model": ["15", 0], "conditioning": ["5", 0]}},
        "7": {"class_type": "BasicScheduler", "inputs": {"model": ["15", 0], "scheduler": "simple", "steps": BRIDGE_STEPS, "denoise": 1.0}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "10": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["9", 0], "guider": ["6", 0], "sampler": ["8", 0],
            "sigmas": ["7", 0], "latent_image": ["5", 1]}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["3", 0]}},
    }
    video_inputs = {"images": ["11", 0], "fps": 24.0}
    if need_audio:
        graph["4"] = {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["vae_audio"]}}
        graph["12"] = {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["10", 0], "vae": ["4", 0]}}
        video_inputs["audio"] = ["12", 0]
    graph["13"] = {"class_type": "CreateVideo", "inputs": video_inputs}
    graph["14"] = {"class_type": "SaveVideo", "inputs": {"video": ["13", 0], "filename_prefix": prefix, "format": "auto"}}
    return graph


def _submit_and_wait(graph, timeout=1500):
    base = COMFY_URL.rstrip("/")
    r = _post(base + "/prompt", {"prompt": graph, "client_id": "bridge"})
    pid = r["prompt_id"]
    _log("ComfyUI 已收下过渡帧 prompt_id=%s" % pid)
    deadline = time.time() + timeout
    lost = 0
    while time.time() < deadline:
        try:
            state, e = _comfy_prompt_state(base, pid)
        except Exception:
            lost += 1
            if lost >= 5:
                _log("ComfyUI 连续 %d 次问不到过渡帧 prompt %s，按已断开处理" % (lost, pid))
                return {"ok": False, "error": "ComfyUI 连接中断，过渡帧生成已中止"}
            time.sleep(12)
            continue
        lost = 0
        if state == "gone":
            _log("ComfyUI 队列和历史里都没有过渡帧 prompt %s，按已在 ComfyUI 侧结束处理" % pid)
            return {"ok": False, "error": "任务已在 ComfyUI 侧结束（队列里被删掉，或 ComfyUI 重启过）"}
        if not e:
            continue
        st = e.get("status", {})
        if st.get("status_str") == "error":
            msgs = [m for m in st.get("messages", [])
                    if isinstance(m, list) and m and m[0] == "execution_error"]
            if msgs:
                err = msgs[-1][1].get("exception_message")
            elif any(isinstance(m, list) and m and m[0] == "execution_interrupted" for m in st.get("messages", [])):
                err = "任务在 ComfyUI 界面被中断"
            else:
                err = "unknown error"
            return {"ok": False, "error": err}
        if st.get("status_str") == "success":
            rel_dir = os.path.basename(os.path.normpath(OUTPUT_DIR))
            for oid, out in e.get("outputs", {}).items():
                for img in out.get("images", []):
                    sub = img.get("subfolder", "").strip("/")
                    fn = img["filename"]
                    if sub == rel_dir:
                        return {"ok": True, "file": fn}
                    if sub.startswith(rel_dir + "/"):
                        return {"ok": True, "file": sub[len(rel_dir) + 1:] + "/" + fn}
                    return {"ok": True, "file": (sub + "/" if sub else "") + fn}
        time.sleep(12)
    return {"ok": False, "error": "timeout"}


def _has_audio(path):
    r = _ffmpeg_run([_ffmpeg(), "-i", path])
    return "Audio:" in (r.stderr or "")


def _esc_drawtext(s):
    """转义 ffmpeg filtergraph 内 drawtext 文本的特殊字符。"""
    for ch in ("\\", ":", "'", ",", ";", "[", "]", "%"):
        s = s.replace(ch, "\\" + ch)
    return s


SUB_FONT_SIZE = 26   # 成片画布固定 848x480，26px 约画面高的 5%（1080p 相当于 58px）
SUB_MARGIN = 24      # 字幕底边距
SUB_LINE_CHARS = 22  # 每行最多几个字，超了自动折行


def _wrap_subtitle(text, per_line=SUB_LINE_CHARS):
    """字幕折行：中文按字数断，英文单词不拆。"""
    lines = []
    for para in (text or "").splitlines():
        cur = ""
        for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9'’.,%:/-]*|.", para):
            if tok.isspace():
                if cur:
                    cur += " "
                continue
            if cur and len(cur) + len(tok) > per_line:
                lines.append(cur.rstrip())
                cur = ""
            cur += tok
        if cur.strip():
            lines.append(cur.strip())
    return lines


def smart_transcode(seq, subtitles, with_audio=True):
    """镜头 + 过渡片统一转码拼接（归一到 848x480@24fps）。with_audio=False 时丢弃全部音轨。"""
    ff = _ffmpeg()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    final = os.path.join(OUTPUT_DIR, "script2video_final_" + time.strftime("%Y%m%d_%H%M%S") + ".mp4")
    cmd = [ff, "-y"]
    for s in seq:
        cmd += ["-i", s]
    n = len(seq)
    vparts = ["[%d:v]scale=848:480,fps=24,setsar=1[v%d]" % (i, i) for i in range(n)]
    if with_audio:
        aparts = ["[%d:a]aresample=44100[a%d]" % (i, i) for i in range(n)]
        fc = ";".join(vparts + aparts)
        fc += ";" + "".join("[v%d][a%d]" % (i, i) for i in range(n)) + "concat=n=%d:v=1:a=1[vv][aa]" % n
    else:
        fc = ";".join(vparts)
        fc += ";" + "".join("[v%d]" % i for i in range(n)) + "concat=n=%d:v=1:a=0[vv]" % n
    vf = []
    for s in subtitles or []:
        lines = _wrap_subtitle(s.get("text") or "")
        if not lines:
            continue
        size = int(s.get("fontsize") or SUB_FONT_SIZE)
        lh = size + 8
        pos = s.get("position") or "bottom"
        for k, line in enumerate(lines):
            if pos == "top":
                y = "%d+%d*%d" % (SUB_MARGIN, k, lh)
            elif pos == "middle":
                y = "(h-%d*%d)/2+%d*%d" % (len(lines), lh, k, lh)
            else:  # 默认贴底居中，多行往上叠
                y = "h-%d-%d*%d-text_h" % (SUB_MARGIN, len(lines) - 1 - k, lh)
            vf.append(
                "drawtext=fontfile='" + _esc_drawtext(FONT) + "':text='" + _esc_drawtext(line) + "':"
                "fontcolor=white:fontsize=" + str(size) + ":borderw=2:bordercolor=black:shadowcolor=black@0.5:shadowx=1:shadowy=1:"
                "x=(w-text_w)/2:y=" + y + ":enable='between(t," + str(s.get("start", 0)) + "," + str(s.get("end", 9999)) + ")'"
            )
    out_v = "vv"
    if vf:
        fc += ";[vv]" + ",".join(vf) + "[vvsub]"
        out_v = "vvsub"
    cmd += ["-filter_complex", fc, "-map", "[" + out_v + "]",
            "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p"]
    if with_audio:
        cmd += ["-map", "[aa]", "-c:a", "aac", "-b:a", "192k", "-ar", "44100"]
    else:
        cmd += ["-an"]
    cmd.append(final)
    r = _ffmpeg_run(cmd)
    if r.returncode != 0:
        raise RuntimeError("合成失败: " + r.stderr[-800:])
    return os.path.basename(final)


def start_smart_concat(clips, subtitles, need_audio=True, subtitle=True, bridge=True):
    """启动后台合成。bridge=False 直接串接镜头；否则两镜之间用 H3 首尾关键帧补过渡片。"""
    st = load_state()
    abs_clips = []
    if bridge:
        segs = [s for s in (st.get("segments") or [])
                if os.path.isfile(os.path.join(OUTPUT_DIR, s))]
        if len(segs) >= 2:
            abs_clips = [os.path.join(OUTPUT_DIR, s) for s in segs]
            subtitles = _seg_subtitles(st) if subtitle else []
    if not abs_clips:
        if clips:
            for c in clips:
                p = os.path.join(OUTPUT_DIR, c.lstrip("/")) if not os.path.isabs(c) else c
                if os.path.isfile(p):
                    abs_clips.append(p)
        else:
            for sc in st["scenes"]:
                out = sc.get("output")
                if not out:
                    continue
                p = os.path.join(OUTPUT_DIR, out)
                if os.path.isfile(p):
                    abs_clips.append(p)
    if len(abs_clips) < 2:
        return {"ok": False, "error": "至少需要 2 个已生成的镜头才能合成"}
    status = SMART_CONCAT_STATUS
    if status.get("running"):
        return {"ok": True, "started": False, "busy": True}
    status.update(running=True, stage="bridging" if bridge else "concat", done=0,
                  total=(len(abs_clips) - 1) if bridge else 0,
                  current="", result=None, error=None, log=[])
    threading.Thread(target=smart_concat_job, args=(abs_clips, subtitles, st, need_audio, bridge), daemon=True).start()
    return {"ok": True, "started": True}


def smart_concat_job(clips, subtitles, st, need_audio=True, bridge=True):
    status = SMART_CONCAT_STATUS
    try:
        stamp = time.strftime("%H%M%S")
        scenes = st["scenes"]
        need = len(clips) - 1
        bridges = list(st.get("bridges") or []) if bridge else []
        if bridge:
            if not (len(bridges) == need and all(os.path.isfile(os.path.join(OUTPUT_DIR, b)) for b in bridges)):
                frames_dir = os.path.join(INPUT_DIR, "frames", "bridge_" + stamp)
                os.makedirs(frames_dir, exist_ok=True)
                bridges = []
                for i in range(need):
                    status["current"] = "过渡 %d/%d" % (i + 1, need)
                    prev_frame = os.path.join(frames_dir, "b%d_prev.png" % i)
                    next_frame = os.path.join(frames_dir, "b%d_next.png" % i)
                    extract_last_frame(clips[i], prev_frame)
                    extract_first_frame(clips[i + 1], next_frame)
                    prev_prompt = (scenes[i].get("prompt") or "").strip() if i < len(scenes) else ""
                    next_prompt = (scenes[i + 1].get("prompt") or "").strip() if i + 1 < len(scenes) else ""
                    prompt = ("无缝转场补帧：镜头从「%s」平滑过渡到「%s」。"
                              "运镜连续不中断，人物与主体的外貌、服装保持不变，"
                              "动作衔接自然，光线与色调渐变过渡，无跳变、无切镜、无黑场。" %
                              (prev_prompt[:80], next_prompt[:80])) + STYLE_SUFFIX + H3_AVOID_SUFFIX
                    rel_prev = os.path.relpath(prev_frame, INPUT_DIR).replace(os.sep, "/")
                    rel_next = os.path.relpath(next_frame, INPUT_DIR).replace(os.sep, "/")
                    r = _submit_and_wait(build_bridge_graph(
                        rel_prev, rel_next, prompt, 7100 + i,
                        "video/bridge_%d_%s" % (i, stamp), need_audio))
                    if not r.get("ok"):
                        raise RuntimeError("过渡 %d 生成失败: %s" % (i, r.get("error")))
                    bridges.append(r["file"])
                    status["done"] = i + 1
                    status["log"].append("过渡 %d: %s" % (i, r["file"]))
                st["bridges"] = bridges
                save_state(st)
            else:
                status["done"] = need
                status["log"].append("复用已有过渡片")
        status["stage"] = "concat"
        status["current"] = "合成中"
        seq = []
        for i in range(len(clips)):
            seq.append(clips[i])
            if i < len(bridges):
                seq.append(os.path.join(OUTPUT_DIR, bridges[i]))
        with_audio = need_audio and all(_has_audio(p) for p in seq)
        if need_audio and not with_audio:
            status["log"].append("部分镜头无音轨，本次成片不含音频")
        final = smart_transcode(seq, subtitles, with_audio)
        status["result"] = final
        status["stage"] = "done"
        status["log"].append("成片: " + final)
    except Exception as e:
        status["stage"] = "error"
        status["error"] = str(e)
    finally:
        status["running"] = False


# ---- 故事板画布整板出片：把画布上的变化帧片段补全后合成成片 ----
BOARD_FILM_STATUS = {"running": False, "stage": "", "done": 0, "total": 0,
                     "current": "", "result": None, "error": None, "log": []}


def board_film_job(st, need_audio=True):
    """按镜头顺序把画布上的片段补齐（缺的用相邻变化帧现生成），再合成成片。"""
    status = BOARD_FILM_STATUS
    try:
        status.update(running=True, stage="segments", done=0, total=0, current="",
                      result=None, error=None, log=[])
        clips = []
        for si, sc in enumerate(st["scenes"]):
            states = sc.get("states") or []
            if not states:
                if sc.get("output"):
                    clips.append(sc["output"])
                continue
            for i in range(len(states) - 1):
                a, b = states[i], states[i + 1]
                if a.get("clip"):
                    clips.append(a["clip"])
                    continue
                if not a.get("keyframe") or not b.get("keyframe"):
                    raise RuntimeError("镜 %d 第 %d 段还缺关键帧" % (si + 1, i + 1))
                status["current"] = "镜 %d 第 %d 段" % (si + 1, i + 1)
                busy, job = _gen_acquire("整板-%s-%d" % (sc.get("id"), i), 3600)
                if busy:
                    raise RuntimeError("ComfyUI 正忙：%s" % busy)
                try:
                    r = generate_scene(segment_scene(sc, states, i))
                finally:
                    _gen_release(job)
                if not r.get("ok"):
                    raise RuntimeError("镜 %d 第 %d 段生成失败：%s" % (si + 1, i + 1, r.get("error")))
                fresh = load_state()
                for s2 in fresh["scenes"]:
                    if s2.get("id") == sc.get("id"):
                        s2["states"][i]["clip"] = r["file"]
                        break
                save_state(fresh)
                st = fresh
                clips.append(r["file"])
                status["done"] += 1
                status["log"].append("镜 %d 第 %d 段: %s" % (si + 1, i + 1, r["file"]))
        if len(clips) < 2:
            raise RuntimeError("至少要有两段已生成的片段才能合成整片")
        status.update(stage="concat", current="合成中")
        # 变化帧之间本来就是同一镜头的连续推进，硬切即可，不插过渡片
        smart_concat_job(clips, [], st, need_audio, False)
        status["result"] = SMART_CONCAT_STATUS.get("result")
        if SMART_CONCAT_STATUS.get("error"):
            raise RuntimeError(SMART_CONCAT_STATUS["error"])
        status["stage"] = "done"
        status["log"].append("成片: %s" % status["result"])
    except Exception as e:
        status["stage"] = "error"
        status["error"] = str(e)
    finally:
        status["running"] = False


# ---- 故事板长镜头：把分镜合并成 H3 原生多镜头一次采样 ----
SEGMENT_MAX_SECONDS = 10
SEGMENT_STATUS = {"running": False, "stage": "", "done": 0, "total": 0,
                  "current": "", "result": None, "error": None}


def pack_segments(scenes):
    """按累计时长把镜头贪心打包成不超过 SEGMENT_MAX_SECONDS 的段落。"""
    groups, cur, acc = [], [], 0.0
    for sc in scenes:
        dur = float(sc.get("seconds") or 5)
        if cur and acc + dur > SEGMENT_MAX_SECONDS + 1e-6:
            groups.append(cur)
            cur, acc = [], 0.0
        cur.append(sc)
        acc += dur
    if cur:
        groups.append(cur)
    return groups


def _fmt_ts(sec):
    return "%02d:%06.3f" % (int(sec // 60), sec % 60)


def compose_storyboard_prompt(group):
    """MiniMax 官方故事板格式：一次采样内用 [Shot N] At MM:SS.mmm 标注镜头切换。"""
    lines = []
    acc = 0.0
    for idx, sc in enumerate(group):
        body = compose_prompt(sc).rstrip("。")
        if idx == 0:
            lines.append("[Shot 1] %s。" % body)
        else:
            lines.append("[Shot %d] At %s, %s。" % (idx + 1, _fmt_ts(acc), body))
        acc += float(sc.get("seconds") or 5)
    return (STYLE_SUFFIX + "\n\n"
            "integrated_multimodal_description: " + " ".join(lines) + "\n\n"
            "non_diegetic_music: N/A")


def generate_segments_job(st, seed_base=8001):
    status = SEGMENT_STATUS
    try:
        groups = pack_segments(st["scenes"])
        status.update(running=True, stage="running", done=0, total=len(groups),
                      current="", result=None, error=None)
        stamp = time.strftime("%H%M%S")
        segs = []
        for gi, grp in enumerate(groups):
            status["current"] = "长镜头 %d/%d" % (gi + 1, len(groups))
            total = sum(float(s.get("seconds") or 5) for s in grp)
            scene = {"model": "MiniMax H3", "prompt": compose_storyboard_prompt(grp),
                     "width": 848, "height": 480, "length": seconds_to_length(total),
                     "seed": seed_base + gi, "mode": "标准", "steps": 25,
                     "prefix": "video/seg_%d_%s" % (gi, stamp)}
            seg_id = "seg_%d_%s" % (gi, stamp)
            busy, job = _gen_acquire(seg_id, 3600)
            if busy:
                raise RuntimeError("ComfyUI 正忙：%s" % busy)
            _log("开始生成 %s（长镜头 %d/%d）" % (seg_id, gi + 1, len(groups)))
            try:
                r = generate_scene(scene)
            except Exception as e:
                r = {"ok": False, "error": str(e)}
            _gen_release(job)
            _log("结束生成 %s ok=%s %s" % (seg_id, r.get("ok"), r.get("file") or r.get("error") or ""))
            if not r.get("ok"):
                raise RuntimeError("长镜头 %d 生成失败: %s" % (gi, r.get("error")))
            segs.append(r["file"])
            status["done"] = gi + 1
        st["segments"] = segs
        save_state(st)
        status["result"] = segs
        status["stage"] = "done"
    except Exception as e:
        status["stage"] = "error"
        status["error"] = str(e)
    finally:
        status["running"] = False


def _seg_subtitles(st):
    """按段落时间轴计算字幕（每两段之间夹一段 BRIDGE_LENGTH 的过渡）。"""
    subs, acc = [], 0.0
    groups = pack_segments(st["scenes"])
    for gi, grp in enumerate(groups):
        for sc in grp:
            text = (sc.get("subtitle") or "").strip()
            if text:
                subs.append({"text": text, "start": acc,
                             "end": acc + float(sc.get("seconds") or 5)})
            acc += float(sc.get("seconds") or 5)
        if gi < len(groups) - 1:
            acc += BRIDGE_LENGTH / 24
    return subs


# ---- HTTP ----
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        try:
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _send_file(self, p, ct):
        try:
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(os.path.getsize(p)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(p, "rb") as f:
                self.wfile.write(f.read())
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _send_media(self, p, ct):
        """镜头首帧/资产图/素材图：带 ETag 走条件请求。

        界面一改就整块重绘，img 元素被重建、no-store 又强制重下，
        于是一屏几十张 1MB 的图被反复取消重下——表现就是「大量图像加载失败」。
        这里改成 no-cache + ETag：浏览器存得住，重绘时只发一个极小的 304；
        文件被覆盖（重新生成设定图）时 mtime 变了，ETag 随之变化，照样能拿到新图。
        """
        try:
            st = os.stat(p)
        except OSError:
            return
        etag = '"%d-%d"' % (int(st.st_mtime), st.st_size)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(st.st_size))
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            with open(p, "rb") as f:
                self.wfile.write(f.read())
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def do_GET(self):
        if self.path == "/":
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "home.html")
            if os.path.exists(p):
                self._send_file(p, "text/html; charset=utf-8")
                return
        elif self.path == "/index.html":
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html")
            if os.path.exists(p):
                self._send_file(p, "text/html; charset=utf-8")
                return
        elif self.path == "/favicon.ico":
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "favicon.svg")
            if os.path.exists(p):
                self._send_file(p, "image/svg+xml")
                return
        elif self.path.startswith("/static/"):
            base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
            rel = unquote(self.path[len("/static/"):]).replace("/", os.sep)
            p = os.path.join(base, rel)
            if os.path.isfile(p) and os.path.abspath(p).startswith(os.path.abspath(base) + os.sep):
                ct = "application/javascript" if p.endswith(".js") else "text/css" if p.endswith(".css") else "application/octet-stream"
                self._send_file(p, ct)
                return
        elif self.path.startswith("/output/"):
            rel = unquote(self.path[len("/output/"):]).replace("/", os.sep)
            p = os.path.join(OUTPUT_DIR, rel)
            if os.path.isfile(p) and os.path.abspath(p).startswith(os.path.abspath(OUTPUT_DIR) + os.sep):
                self._send_media(p, "video/mp4" if p.endswith(".mp4") else "application/octet-stream")
                return
        elif self.path.startswith("/assets/"):
            rel = unquote(self.path[len("/assets/"):]).replace("/", os.sep)
            p = os.path.join(ASSETS_DIR, rel)
            if os.path.isfile(p) and os.path.abspath(p).startswith(os.path.abspath(ASSETS_DIR) + os.sep):
                ct = "image/png" if p.lower().endswith(".png") else "image/jpeg" if p.lower().endswith((".jpg", ".jpeg")) else "image/webp" if p.lower().endswith(".webp") else "image/gif" if p.lower().endswith(".gif") else "application/octet-stream"
                self._send_media(p, ct)
                return
        elif self.path == "/api/agents":
            # 前端流水线按这张表渲染，所以加减 agent 不用改前端
            self._send(200, {"ok": True, "agents": agent_defs()})
        elif self.path.startswith("/api/auto_direct_status"):
            jid = ""
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            for pair in q.split("&"):
                if pair.startswith("job="):
                    jid = pair[4:]
                    break
            # 没带 job 就返回最新的一个，前端刷新后还能找回在跑的那次
            if not jid:
                with AUTO_LOCK:
                    jid = sorted(AUTO_JOBS.keys())[-1] if AUTO_JOBS else ""
            j = AUTO_JOBS.get(jid)
            if not j:
                self._send(200, {"ok": False, "error": "没有这个任务"})
                return
            self._send(200, {"ok": True, "job": jid, "step": j.get("step"),
                             "agent": j.get("agent"), "result": j.get("result"),
                             "done": j.get("done") or 0, "total": j.get("total") or 0,
                             "current": j.get("current"), "error": j.get("error"),
                             "scenes": j.get("scenes") or []})
        elif self.path == "/api/auto_direct_cancel":
            jid = (self._body().get("job") or "")
            if AUTO_JOBS.get(jid):
                AUTO_JOBS[jid]["cancel"] = True
            cancel_running_gen()
            self._send(200, {"ok": True})
        elif self.path == "/api/status":
            try:
                q = _get(COMFY_URL.rstrip("/") + "/queue")
                self._send(200, {"ok": True, "running": len(q.get("queue_running", [])), "pending": len(q.get("queue_pending", []))})
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e)})
            return
        elif self.path == "/api/progress":
            with _LIVE_LOCK:
                live = dict(_LIVE)
            started = live.pop("started", 0.0)
            live["ok"] = True
            live["elapsed"] = int(time.time() - started) if started else 0
            live["scene"], live["gen_elapsed"] = _gen_active()
            self._send(200, live)
            return
        elif self.path == "/api/concat_status":
            self._send(200, {"ok": True, "status": SMART_CONCAT_STATUS})
            return
        elif self.path == "/api/segment_status":
            self._send(200, {"ok": True, "status": SEGMENT_STATUS})
            return
        elif self.path == "/api/board_film_status":
            self._send(200, {"ok": True, "status": BOARD_FILM_STATUS})
            return
        elif self.path == "/api/asset_views_status":
            self._send(200, {"ok": True, "status": ASSET_VIEW_STATUS})
            return
        elif self.path == "/api/version":
            # 页面头上的「构建」徽标用它。前端和后端分开报：只报 app.js 的话，一轮只改后端的
            # 改动会让徽标纹丝不动，看着像前端没更新。
            here = os.path.dirname(os.path.abspath(__file__))
            def _stamp(rel):
                p = os.path.join(here, rel)
                return time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(p)))
            self._send(200, {"ok": True, "js": _stamp(os.path.join("static", "app.js")),
                             "py": _stamp("app.py")})
        elif self.path == "/api/state":
            st = load_state()
            pid = _current_project_id()
            pname = (st.get("name") or "默认项目") if pid == "default" else (st.get("name") or pid)
            self._send(200, {"ok": True, "scenes": st["scenes"],
                             "character": _asset_slot(st, "character"),
                             "scene": _asset_slot(st, "scene"),
                             "equipment": _asset_slot(st, "equipment"),
                             "assets": st.get("assets") or [],
                             "board": st.get("board") or {},
                             "global_negative_prompt": st.get("global_negative_prompt") or "",
                             "project": {"id": pid, "name": pname}})
            return
        elif self.path == "/api/projects":
            projects = list_projects()
            self._send(200, {"ok": True, "current": _current_project_id(), "projects": projects})
            return
        elif self.path == "/api/assets":
            self._send(200, {"ok": True, "assets": list_assets()})
            return
        elif self.path == "/api/outputs":
            self._send(200, {"ok": True, "outputs": list_outputs()})
            return
        elif self.path == "/api/assets/tags":
            self._send(200, {"ok": True, "tags": _load_json(TAGS_FILE, {})})
            return
        elif self.path == "/api/assets/graph":
            self._send(200, {"ok": True, "graph": _neo4j_read_asset_graph()})
            return
        elif self.path == "/api/knowledge":
            ensure_knowledge_graph()
            self._send(200, {"ok": True, "graph": _neo4j_read_graph()})
            return
        elif self.path.startswith("/api/assets/search"):
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            self._send(200, {"ok": True, "assets": search_assets(q)})
            return
        elif self.path.startswith("/input/"):
            rel = unquote(self.path[len("/input/"):]).replace("/", os.sep)
            p = os.path.join(INPUT_DIR, rel)
            if os.path.isfile(p) and os.path.abspath(p).startswith(os.path.abspath(INPUT_DIR) + os.sep):
                ct = "image/png" if p.lower().endswith(".png") else "image/jpeg" if p.lower().endswith((".jpg", ".jpeg")) else "image/webp" if p.lower().endswith(".webp") else "application/octet-stream"
                self._send_media(p, ct)
                return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            if self.path == "/api/optimize":
                body = self._body()
                text = body.get("text") or ""
                images = []
                if body.get("docx_b64"):
                    text, images = extract_document(body["docx_b64"], body.get("filename") or "")
                engine, engine_name, note = "rule", "格式规则", ""
                scenes, entities = [], []
                if text.strip() and is_storyboard_table(text):
                    scenes = optimize_script(text)  # 分镜表按行列拆，镜头数与表格行一一对应
                    if scenes:
                        engine_name = "分镜表逐行解析"
                if not scenes and text.strip() and body.get("ai", True):
                    # 走「拆分镜」agent 在全局设置里配的模型，和 Agent 面板那条是同一份配置
                    engine, api_key, base_url, model = _agent_llm(body, "split")
                    (scenes, entities), ai_label = ai_split_scenes(
                        text, "ollama" if engine == "ollama" else "deepseek", api_key, base_url, model)
                    if scenes:
                        engine, engine_name = "ai", ai_label
                    else:
                        note = ai_label
                if not scenes:
                    scenes = optimize_script(text)
                st = load_state()
                existing = st["scenes"]
                base = 0
                if body.get("append"):
                    for sc in existing:
                        m = re.match(r"scene(\d+)$", str(sc.get("id") or ""))
                        if m:
                            base = max(base, int(m.group(1)))
                for i, s in enumerate(scenes):
                    s["id"] = "scene%d" % (base + i + 1)
                    # setdefault 而不是 update：这里是「补默认」，不是「覆盖模型给的景别运镜」
                    for _k, _v in new_scene_defaults().items():
                        s.setdefault(_k, _v)
                    w, h = resolve_geometry(s)
                    s["width"], s["height"] = w, h
                    s["output"] = None
                    s["first_frame"] = None
                attached = attach_story_images(scenes, images)
                st["scenes"] = (existing + scenes) if body.get("append") else scenes
                added = upsert_entities(st, entities)   # 先有资产
                link_all_assets(st)                     # 新拆出来的镜头按名字连上去
                save_state(st)
                self._send(200, {"ok": True, "scenes": scenes, "assets": st["assets"], "assets_added": added,
                                 "append": bool(body.get("append")), "engine": engine,
                                 "engine_name": engine_name, "note": note, "images": attached})
            elif self.path == "/api/agent_run":
                # 单独跑一个 agent；scenes 不传就跑当前项目里的那批
                body = self._body()
                aid = body.get("agent") or ""
                if aid not in _AGENT_RUNNERS:
                    self._send(200, {"ok": False, "error": "没有这个 agent：" + aid})
                    return
                with AUTO_LOCK:
                    for jid, j in list(AUTO_JOBS.items()):
                        if j.get("step") in ("失败", "已取消", "完成"):
                            AUTO_JOBS.pop(jid, None)
                    jid = "agent_" + time.strftime("%H%M%S") + "_" + str(random.randrange(100, 999))
                    AUTO_JOBS[jid] = {"id": jid, "agent": aid, "scenes": body.get("scenes"),
                                      "idea": body.get("idea") or "",
                                      "agent_models": body.get("agent_models") or {},
                                      "step": "排队", "done": 0, "total": 0, "cancel": False}
                threading.Thread(target=_agent_job, args=(AUTO_JOBS[jid],), daemon=True).start()
                self._send(200, {"ok": True, "job": jid})
            elif self.path == "/api/auto_direct":
                body = self._body()
                idea = (body.get("idea") or "").strip()
                if not idea:
                    self._send(200, {"ok": False, "error": "先写一句你的想法"})
                    return
                with AUTO_LOCK:
                    for jid, j in list(AUTO_JOBS.items()):
                        if j.get("step") in ("失败", "已取消", "完成"):
                            AUTO_JOBS.pop(jid, None)
                    jid = "auto_" + time.strftime("%H%M%S") + "_" + str(random.randrange(100, 999))
                    AUTO_JOBS[jid] = {"id": jid, "idea": idea,
                                      "agent_models": body.get("agent_models") or {},
                                      # 默认链：拆镜 → 拆资产 → 分镜提示词 → 定帧图。
                                      # 以前这里写的是 ["split", "prompt", "image"]，"image"
                                      # 早就不是 agent id 了，走到第三步就报「没有这个 agent」。
                                      "steps": body.get("steps") or ["split", "entity", "prompt", "t2i"],
                                      "step": "排队", "done": 0, "total": 0, "cancel": False}
                threading.Thread(target=_auto_direct_job, args=(AUTO_JOBS[jid],),
                                 daemon=True).start()
                self._send(200, {"ok": True, "job": jid})
            elif self.path == "/api/compile_shot":

                body = self._body()
                scene = body.get("scene") or {}
                # 「编译镜头」手动按钮和「分镜提示词」agent 是同一件事，共用同一份模型配置
                out = compile_shot_prompt(scene, _agent_llm(body, "prompt"))
                if out:
                    self._send(200, {"ok": True, "prompt": out})
                else:
                    self._send(200, {"ok": False, "error":
                                     "需要本机 Ollama 对话模型或 DEEPSEEK_API_KEY"})
            elif self.path == "/api/cancel":
                self._send(200, cancel_running_gen())
            elif self.path == "/api/generate":
                raw = self._body().get("scene", {})
                matched = None
                if raw.get("auto_match"):
                    m = auto_match_image(raw.get("prompt") or "")
                    if m:
                        try:
                            src = resolve_asset_path(m["path"])
                            ext = os.path.splitext(src)[1].lower() or ".png"
                            frames_dir = os.path.join(INPUT_DIR, "frames")
                            os.makedirs(frames_dir, exist_ok=True)
                            dst_name = "auto_" + time.strftime("%H%M%S") + "_" + str(random.randrange(100, 999)) + ext
                            shutil.copyfile(src, os.path.join(frames_dir, dst_name))
                            raw["auto_guides"] = ["frames/" + dst_name]
                            _record_asset_usage(m["path"], str(raw.get("id") or "scene"), str(raw.get("prompt") or ""))
                            matched = {"name": m["name"], "path": m["path"], "score": m["score"]}
                        except Exception as e:
                            matched = {"error": str(e)}
                stg = load_state()
                if (stg.get("global_negative_prompt") or "").strip() and not raw.get("global_negative_prompt"):
                    raw["global_negative_prompt"] = stg["global_negative_prompt"]
                chain_wanted = bool(raw.get("chain_frames")) and not raw.get("first_frame")
                chained = None
                if chain_wanted:
                    chained = chain_first_frame(stg, raw)
                    if chained:
                        raw["first_frame"] = chained
                scene = prepare_scene(raw)
                scene.setdefault("prefix", "video/" + scene.get("id", "scene") + "_" + time.strftime("%H%M%S"))
                sid = str(scene.get("id") or "scene")
                busy, job = _gen_acquire(sid, int(scene.get("timeout") or 1800))
                if busy:
                    self._send(200, {"ok": False, "busy": True, "error": busy})
                    return
                _log("开始生成 %s（模型=%s）" % (sid, scene.get("model")))
                try:
                    r = generate_scene(scene)
                except Exception as e:
                    r = {"ok": False, "error": str(e)}
                _gen_release(job)
                _log("结束生成 %s ok=%s %s" % (sid, r.get("ok"), r.get("file") or r.get("error") or ""))
                if r.get("ok"):
                    st = load_state()
                    for sc in st["scenes"]:
                        if sc.get("id") == scene.get("id"):
                            sc["output"] = r["file"]
                            for k in ("prompt", "seconds", "subtitle", "shot", "camera", "aspect",
                                      "resolution", "mode", "steps", "seed", "negative_prompt", "model", "second_pass"):
                                if k in raw:
                                    sc[k] = raw[k]
                            if raw.get("voice") is not None:
                                sc["voice"] = raw["voice"]
                            if raw.get("guides") is not None:
                                sc["guides"] = raw["guides"]
                            if raw.get("first_frame") is not None:
                                sc["first_frame"] = raw["first_frame"]
                            sc["use_character_frame"] = bool(raw.get("use_character_frame"))
                            sc["use_scene_ref"] = bool(raw.get("use_scene_ref"))
                            break
                    save_state(st)
                if matched is not None:
                    r = dict(r)
                    r["matched"] = matched
                if chained:
                    r = dict(r)
                    r["chained"] = chained
                elif chain_wanted:
                    r = dict(r)
                    r["chain_skip"] = "上一镜还没有成片"
                self._send(200, r)
            elif self.path == "/api/upload_frame":
                body = self._body()
                scene_id = str(body.get("scene_id") or "scene")
                ext = str(body.get("ext") or "png").lower()
                if ext not in ("png", "jpg", "jpeg", "webp"):
                    self._send(200, {"ok": False, "error": "仅支持 png/jpg/webp 图片"})
                    return
                raw = base64.b64decode(body.get("data_b64") or "")
                if not raw or not _looks_like_image(raw, ext):
                    self._send(200, {"ok": False, "error": "图片内容无效"})
                    return
                frames_dir = os.path.join(INPUT_DIR, "frames")
                os.makedirs(frames_dir, exist_ok=True)
                name = "frame_" + re.sub(r"[^0-9a-zA-Z_-]", "", scene_id) + "_" + time.strftime("%H%M%S") + "_" + str(random.randrange(1000, 9999)) + "." + ext
                with open(os.path.join(frames_dir, name), "wb") as f:
                    f.write(raw)
                rel = "frames/" + name
                st = load_state()
                if scene_id in ASSET_SPECS:
                    _asset_of_kind(st, scene_id)["file"] = rel
                else:
                    for sc in st["scenes"]:
                        if sc.get("id") == scene_id:
                            sc["first_frame"] = rel
                            break
                save_state(st)
                self._send(200, {"ok": True, "file": rel})
            elif self.path == "/api/upload_video":
                body = self._body()
                ext = str(body.get("ext") or "mp4").lower().lstrip(".")
                if ext not in ("mp4", "mov", "mkv", "webm"):
                    self._send(200, {"ok": False, "error": "仅支持 mp4/mov/mkv/webm 视频"})
                    return
                raw = base64.b64decode(body.get("data_b64") or "")
                if not raw:
                    self._send(200, {"ok": False, "error": "视频内容为空"})
                    return
                if len(raw) > MAX_VIDEO_BYTES:
                    self._send(200, {"ok": False, "error": "视频超过 %d MB，请先压缩或截短" % (MAX_VIDEO_BYTES // 1024 // 1024)})
                    return
                videos_dir = os.path.join(INPUT_DIR, "videos")
                os.makedirs(videos_dir, exist_ok=True)
                vname = "src_" + time.strftime("%Y%m%d_%H%M%S") + "_" + str(random.randrange(1000, 9999)) + "." + ext
                with open(os.path.join(videos_dir, vname), "wb") as f:
                    f.write(raw)
                self._send(200, {"ok": True, "file": "videos/" + vname})
            elif self.path == "/api/continue_video":
                body = self._body()
                try:
                    scene = make_continue_scene(load_state(), str(body.get("scene_id") or ""), body.get("video"),
                                                body.get("prompt"), body.get("seconds"), body.get("mode"), body.get("seed"))
                except Exception as e:
                    self._send(200, {"ok": False, "error": str(e)})
                    return
                self._send(200, {"ok": True, "scene": scene, "file": scene["first_frame"]})
            elif self.path == "/api/ding_frame":
                body = self._body()
                raw = body.get("scene", {})
                ch = (load_state().get("character") or {})
                if ch.get("description") and not (raw.get("character") or {}).get("description"):
                    raw["character"] = ch
                sc = (load_state().get("scene") or {})
                if sc.get("description") and not (raw.get("scene") or {}).get("description"):
                    raw["scene"] = sc
                scene = prepare_scene(raw)
                en = _translate_prompt(scene["prompt"])
                if not en:
                    self._send(200, {"ok": False, "error": "提示词翻译失败：需要本机 Ollama 对话模型或 DEEPSEEK_API_KEY"})
                    return
                batch = max(2, min(8, int(body.get("batch") or 4)))
                seed = scene.get("seed") or random.randrange(0, 2 ** 31)
                stamp = time.strftime("%H%M%S") + "_" + str(random.randrange(100, 999))
                sid = re.sub(r"[^0-9a-zA-Z_-]", "", str(scene.get("id") or "scene"))
                prefix = "frames/ding_" + sid + "_" + stamp
                graph = build_graph_t2i(scene, en, batch, seed, prefix)
                save_workflow_files(graph, "定帧_" + str(scene.get("id") or "scene"))
                lock_id = "定帧-" + str(scene.get("id") or "scene")
                busy, job = _gen_acquire(lock_id, 900)
                if busy:
                    self._send(200, {"ok": False, "busy": True, "error": busy})
                    return
                try:
                    r = _submit_prompt(graph, timeout=900, images_all=True)
                except Exception as e:
                    r = {"ok": False, "error": str(e)}
                _gen_release(job)
                if not r.get("ok"):
                    self._send(200, r)
                    return
                files = []
                frames_dir = os.path.join(INPUT_DIR, "frames")
                os.makedirs(frames_dir, exist_ok=True)
                for rel in r.get("files") or []:
                    src = os.path.join(_COMFY_ROOT, "output", rel.replace("/", os.sep))
                    if not os.path.isfile(src):
                        continue
                    dst = os.path.join(frames_dir, os.path.basename(rel))
                    shutil.move(src, dst)
                    files.append("frames/" + os.path.basename(rel))
                if not files:
                    self._send(200, {"ok": False, "error": "定帧生成失败：无图片输出"})
                    return
                st = load_state()
                for s in st["scenes"]:
                    if s.get("id") == scene.get("id"):
                        s["ding_candidates"] = files
                        break
                save_state(st)
                self._send(200, {"ok": True, "files": files, "prompt_en": en, "seed": seed})
            elif self.path == "/api/pick_ding":
                body = self._body()
                scene_id = str(body.get("scene_id") or "scene")
                file = str(body.get("file") or "")
                name = os.path.basename(file.replace("\\", "/"))
                src = os.path.join(INPUT_DIR, "frames", name)
                # kf_ 也要收：画布上变化帧出的图会进卡片的定帧候选，在卡片上「选用」它
                # 走的就是这个接口
                if not name.startswith(("ding_", "kf_")) or not os.path.isfile(src):
                    self._send(200, {"ok": False, "error": "定帧文件不存在"})
                    return
                # 记所选那张自己的路径，不再复制成固定的 ding_<镜>.png：名字固定的话 URL 就不变，
                # 换一张浏览器还在显示旧图，「已选用」也永远对不上是哪一张
                rel = "frames/" + name
                st = load_state()
                for s in st["scenes"]:
                    if s.get("id") == scene_id:
                        s["first_frame"] = rel
                        s["ding_frame"] = rel
                        break
                save_state(st)
                self._send(200, {"ok": True, "first_frame": rel})
            elif self.path == "/api/asset_image":
                body = self._body()
                st = load_state()
                a = _asset_for_write(st, body)
                if a is None:
                    self._send(200, {"ok": False, "error": "资产不存在"})
                    return
                aid = str(a.get("id"))
                kind = a.get("kind") if a.get("kind") in ASSET_SPECS else "character"
                spec = ASSET_SPECS[kind]
                desc = (a.get("description") or "").strip()
                if not desc:
                    self._send(200, {"ok": False, "error": "请先填写" + spec["label"].replace("设定图", "") + "的描述"})
                    return
                en = _translate_prompt(desc, spec["sys"])
                if not en:
                    self._send(200, {"ok": False, "error": "提示词翻译失败：需要本机 Ollama 对话模型或 DEEPSEEK_API_KEY"})
                    return
                en = en.strip().rstrip(".") + ", " + spec["suffix"]
                batch = max(2, min(8, int(body.get("batch") or 4)))
                seed = int(body.get("seed") or 0) or random.randrange(0, 2 ** 31)
                stamp = time.strftime("%H%M%S") + "_" + str(random.randrange(100, 999))
                slug = re.sub(r"[^0-9a-zA-Z_-]", "", aid) or kind
                graph = build_graph_t2i({"aspect": spec["aspect"], "width": spec["width"], "height": spec["height"]},
                                        en, batch, seed, "frames/asset_" + slug + "_" + stamp)
                save_workflow_files(graph, "资产图_" + spec["label"])
                busy, job = _gen_acquire("资产图-" + aid, 900)
                if busy:
                    self._send(200, {"ok": False, "busy": True, "error": busy})
                    return
                try:
                    r = _submit_prompt(graph, timeout=900, images_all=True)
                except Exception as e:
                    r = {"ok": False, "error": str(e)}
                _gen_release(job)
                if not r.get("ok"):
                    self._send(200, r)
                    return
                files = []
                frames_dir = os.path.join(INPUT_DIR, "frames")
                os.makedirs(frames_dir, exist_ok=True)
                for rel in r.get("files") or []:
                    src = os.path.join(_COMFY_ROOT, "output", rel.replace("/", os.sep))
                    if not os.path.isfile(src):
                        continue
                    shutil.move(src, os.path.join(frames_dir, os.path.basename(rel)))
                    files.append("frames/" + os.path.basename(rel))
                if not files:
                    self._send(200, {"ok": False, "error": spec["label"] + "生成失败：无图片输出"})
                    return
                st = load_state()
                fresh = _find_asset(st, aid) or a
                fresh["candidates"] = files
                fresh["prompt_en"] = en
                save_state(st)
                self._send(200, {"ok": True, "asset": fresh, "files": files, "prompt_en": en, "seed": seed})
            elif self.path == "/api/pick_asset":
                body = self._body()
                st = load_state()
                a = _asset_for_write(st, body)
                if a is None:
                    self._send(200, {"ok": False, "error": "资产不存在"})
                    return
                name = os.path.basename(str(body.get("file") or "").replace("\\", "/"))
                src = os.path.join(INPUT_DIR, "frames", name)
                if not name.startswith("asset_") or not os.path.isfile(src):
                    self._send(200, {"ok": False, "error": "设定图文件不存在"})
                    return
                # 直接把选中的那张候选图的路径记下来。以前是复制成固定的 asset_<id>.png：
                # URL 永远不变，换一张候选图浏览器还在显示旧图；「已选用」也比不上是哪一张。
                rel = "frames/" + name
                a["file"] = rel
                link_all_assets(st)      # 底图换了，连线的参考图跟着换
                save_state(st)
                self._send(200, {"ok": True, "asset": a, "image": rel, "scenes": st["scenes"]})
            elif self.path == "/api/keyframe":
                body = self._body()
                raw = body.get("scene", {})
                st = load_state()
                idx = body.get("state_index")
                state = None
                if idx is not None:
                    try:
                        state = (raw.get("states") or [])[int(idx)]
                    except (IndexError, TypeError, ValueError):
                        state = None
                    if not isinstance(state, dict):
                        self._send(200, {"ok": False, "error": "变化帧不存在"})
                        return
                    raw = dict(raw, prompt=state.get("prompt") or "",
                               shot=state.get("shot") or "中景",
                               angle=state.get("angle") or "平视",
                               camera=state.get("camera") or "固定镜头",
                               seconds=state.get("seconds") or 4,
                               subtitle=state.get("subtitle") or "")
                prev = ""
                if idx is not None and int(idx) > 0:
                    prev_states = raw.get("states") or []
                    if int(idx) - 1 < len(prev_states):
                        prev = str((prev_states[int(idx) - 1] or {}).get("keyframe") or "")
                scene = prepare_scene(raw)
                refs = keyframe_references(scene, st, prev)
                # 从上一帧改的那条指令和「拿资产图建场景」不是一回事，别混用
                edit_sys = KONTEXT_SYS_CHAIN if prev else KONTEXT_SYS_REF
                en = _translate_prompt(scene["prompt"], edit_sys if refs else KONTEXT_SYS_T2I)
                if not en:
                    self._send(200, {"ok": False, "error": "提示词翻译失败：需要本机 Ollama 对话模型或 DEEPSEEK_API_KEY"})
                    return
                batch = max(1, min(4, int(body.get("batch") or 2)))
                steps = max(8, min(40, int(body.get("steps") or 20)))
                seed = int(body.get("seed") or 0) or random.randrange(0, 2 ** 31)
                stamp = time.strftime("%H%M%S") + "_" + str(random.randrange(100, 999))
                sid = re.sub(r"[^0-9a-zA-Z_-]", "", str(scene.get("id") or "scene"))
                graph = build_graph_keyframe(scene, en, refs, seed, steps, batch, "frames/kf_" + sid + "_" + stamp)
                save_workflow_files(graph, "故事版关键帧_Kontext")
                lock_id = "关键帧-" + str(scene.get("id") or "scene")
                busy, job = _gen_acquire(lock_id, 1800)
                if busy:
                    self._send(200, {"ok": False, "busy": True, "error": busy})
                    return
                try:
                    r = _submit_prompt(graph, timeout=1800, images_all=True)
                except Exception as e:
                    r = {"ok": False, "error": str(e)}
                _gen_release(job)
                if not r.get("ok"):
                    self._send(200, r)
                    return
                files = []
                frames_dir = os.path.join(INPUT_DIR, "frames")
                os.makedirs(frames_dir, exist_ok=True)
                for rel in r.get("files") or []:
                    src = os.path.join(_COMFY_ROOT, "output", rel.replace("/", os.sep))
                    if not os.path.isfile(src):
                        continue
                    shutil.move(src, os.path.join(frames_dir, os.path.basename(rel)))
                    files.append("frames/" + os.path.basename(rel))
                if not files:
                    self._send(200, {"ok": False, "error": "关键帧生成失败：无图片输出"})
                    return
                st = load_state()
                for s in st["scenes"]:
                    if s.get("id") != scene.get("id"):
                        continue
                    if idx is not None:
                        states = s.get("states") or []
                        if 0 <= int(idx) < len(states):
                            slot = states[int(idx)]
                            add_version(slot, "keyframe", files, seed=seed)
                            slot["candidates"] = files
                            # 第一帧出的图就是这一镜的定帧候选：分镜卡片里直接能看到、能选，
                            # 不用在画布上出完再回卡片里重出一遍
                            if int(idx) == 0:
                                s["ding_candidates"] = files
                        s["keyframe_ref"] = refs[0] if refs else None
                    else:
                        add_version(s, "keyframe", files, seed=seed)
                        s["ding_candidates"] = files
                        s["keyframe_ref"] = refs[0] if refs else None
                    break
                save_state(st)
                self._send(200, {"ok": True, "files": files, "prompt_en": en, "seed": seed,
                                 "refs": refs, "state_index": idx})
            elif self.path == "/api/split_states":
                body = self._body()
                st = load_state()
                scene = next((s for s in st["scenes"] if s.get("id") == str(body.get("scene_id") or "")), None)
                if not scene:
                    self._send(200, {"ok": False, "error": "镜头不存在"})
                    return
                text = str(body.get("text") or "").strip() or (scene.get("prompt") or "").strip()
                if not text:
                    self._send(200, {"ok": False, "error": "这个镜头还没有画面描述，先写描述或先拆镜"})
                    return
                try:
                    total = int(float(body.get("seconds") or scene.get("seconds") or 0))
                except (TypeError, ValueError):
                    total = 0
                engine, api_key, base_url, model = _agent_llm(body, "frame")
                # 时长要走 total_sec：帧数上限（H3 每段最少 4 秒）就是这么算出来的。
                # 只把时长写进正文，模型会当成参考值，照拆出短于 4 秒的段。
                states, label = ai_split_states(text, "ollama" if engine == "ollama" else "deepseek",
                                                api_key, base_url, model, total_sec=total)
                if not states:
                    self._send(200, {"ok": False, "error": "拆变化帧失败：" + label})
                    return
                fit_states_seconds(states, total)
                stamp = time.strftime("%H%M%S")
                for i, s in enumerate(states):
                    s["id"] = "st" + stamp + str(i)
                    s["length"] = seconds_to_length(s["seconds"])
                scene["states"] = states
                save_state(st)
                self._send(200, {"ok": True, "states": states, "engine": label})
            elif self.path == "/api/save_states":
                body = self._body()
                st = load_state()
                scene = next((s for s in st["scenes"] if s.get("id") == str(body.get("scene_id") or "")), None)
                if not scene:
                    self._send(200, {"ok": False, "error": "镜头不存在"})
                    return
                incoming = body.get("states")
                if not isinstance(incoming, list):
                    self._send(200, {"ok": False, "error": "states 必须是列表"})
                    return
                old = {s.get("id"): s for s in (scene.get("states") or []) if isinstance(s, dict)}
                cleaned = []
                for s in incoming:
                    if not isinstance(s, dict):
                        continue
                    prev = old.get(s.get("id")) or {}
                    try:
                        sec = int(float(s.get("seconds", prev.get("seconds", 4))))
                    except (TypeError, ValueError):
                        sec = int(prev.get("seconds") or 4)
                    item = {
                        "id": s.get("id") or prev.get("id") or ("st" + str(random.randrange(10 ** 9))),
                        "prompt": str(s.get("prompt", prev.get("prompt") or "")).strip(),
                        "shot": s.get("shot") if s.get("shot") in SHOT_SIZES else (prev.get("shot") or "中景"),
                        "camera": s.get("camera") if s.get("camera") in CAMERA_MOVES else (prev.get("camera") or "固定镜头"),
                        "seconds": max(3, min(15, sec)),
                        "subtitle": str(s.get("subtitle", prev.get("subtitle") or "")).strip(),
                    }
                    item["length"] = seconds_to_length(item["seconds"])
                    for k in ("keyframe", "keyframe_versions", "keyframe_batches", "candidates",
                              "clip", "clip_versions", "clip_batches", "clip_prev", "seed",
                              "prompt_en", "edit", "inpaint"):
                        if prev.get(k) is not None:
                            item[k] = prev[k]
                    cleaned.append(item)
                scene["states"] = cleaned
                save_state(st)
                self._send(200, {"ok": True, "states": cleaned})
            elif self.path == "/api/pick_state_keyframe":
                body = self._body()
                scene_id = str(body.get("scene_id") or "")
                try:
                    idx = int(body.get("state_index"))
                except (TypeError, ValueError):
                    self._send(200, {"ok": False, "error": "state_index 必须是整数"})
                    return
                name = os.path.basename(str(body.get("file") or "").replace("\\", "/"))
                src = os.path.join(INPUT_DIR, "frames", name)
                if not name.startswith("kf_") or not os.path.isfile(src):
                    self._send(200, {"ok": False, "error": "关键帧文件不存在"})
                    return
                # 同上：直接记所选那张的路径。以前复制成 kf_<镜>_s<N>.png，同一个变化帧换一张
                # 候选图 URL 不变，画布上那张节点图不会跟着换
                rel = "frames/" + name
                st = load_state()
                for s in st["scenes"]:
                    if s.get("id") == scene_id:
                        states = s.get("states") or []
                        if 0 <= idx < len(states):
                            slot = states[idx]
                            slot["keyframe"] = rel
                            vers = slot.setdefault("keyframe_versions", [])
                            if rel not in vers:
                                vers.append(rel)
                            # 第一个变化帧的关键帧就是这一镜的定帧：分镜卡片、首帧、卡片里
                            # 那条定帧候选都跟着它走，画布上选定一次就够了
                            if idx == 0:
                                s["ding_frame"] = rel
                                s["first_frame"] = rel
                        break
                save_state(st)
                self._send(200, {"ok": True, "keyframe": rel,
                                 "first_frame": rel if idx == 0 else None})
            elif self.path == "/api/segment":
                body = self._body()
                st = load_state()
                scene = next((s for s in st["scenes"] if s.get("id") == str(body.get("scene_id") or "")), None)
                if not scene:
                    self._send(200, {"ok": False, "error": "镜头不存在"})
                    return
                states = scene.get("states") or []
                try:
                    idx = int(body.get("index"))
                except (TypeError, ValueError):
                    self._send(200, {"ok": False, "error": "index 必须是整数"})
                    return
                if not 0 <= idx < len(states) - 1:
                    self._send(200, {"ok": False, "error": "这一段不存在（要相邻两个变化帧）"})
                    return
                a, b = states[idx], states[idx + 1]
                if not a.get("keyframe") or not b.get("keyframe"):
                    self._send(200, {"ok": False, "error": "这一段的首尾两个变化帧都要先选定关键帧"})
                    return
                seg_seed = int(states[idx].get("seed") or random.randrange(0, 2 ** 31))
                prepared = segment_scene(scene, states, idx, seed=seg_seed)
                busy, job = _gen_acquire("片段-%s-%d" % (scene["id"], idx), 3600)
                if busy:
                    self._send(200, {"ok": False, "busy": True, "error": busy})
                    return
                try:
                    r = generate_scene(prepared)
                except Exception as e:
                    r = {"ok": False, "error": str(e)}
                _gen_release(job)
                if not r.get("ok"):
                    self._send(200, r)
                    return
                st = load_state()
                for s in st["scenes"]:
                    if s.get("id") == scene["id"]:
                        s["states"][idx]["clip_prev"] = s["states"][idx].get("clip")
                        s["states"][idx]["clip"] = r["file"]
                        s["states"][idx]["seed"] = seg_seed
                        add_version(s["states"][idx], "clip", [r["file"]], "segment", seg_seed)
                        break
                save_state(st)
                self._send(200, {"ok": True, "clip": r["file"], "index": idx, "seed": seg_seed})
            elif self.path == "/api/asset_views":
                body = self._body()
                if ASSET_VIEW_STATUS.get("running"):
                    self._send(200, {"ok": False, "busy": True, "error": "六视图正在生成中，请等这一轮跑完"})
                    return
                st = load_state()
                a = _asset_for_write(st, body)
                if a is None:
                    self._send(200, {"ok": False, "error": "资产不存在"})
                    return
                src = str(body.get("source") or "").strip() or (a.get("file") or "")
                name = os.path.basename(src.replace("\\", "/"))
                if not name or not os.path.isfile(os.path.join(INPUT_DIR, "frames", name)):
                    self._send(200, {"ok": False, "error": "六视图是图生图，请先上传或生成一张底图"})
                    return
                want = body.get("views")
                if not isinstance(want, list) or not want:
                    want = [k for k, _l in ASSET_VIEWS]
                want = [k for k, _l in ASSET_VIEWS if k in want]
                if not want:
                    self._send(200, {"ok": False, "error": "views 里没有认识的机位"})
                    return
                try:
                    steps = max(8, min(40, int(body.get("steps") or 20)))
                except (TypeError, ValueError):
                    steps = 20
                threading.Thread(target=asset_views_job,
                                 args=(a.get("id"), a.get("kind"), "frames/" + name, want, steps), daemon=True).start()
                self._send(200, {"ok": True, "started": True, "views": want, "source": "frames/" + name})
            elif self.path == "/api/board_film":
                if BOARD_FILM_STATUS.get("running"):
                    self._send(200, {"ok": True, "started": False, "busy": True,
                                     "error": "整板出片正在进行中"})
                    return
                body = self._body()
                st = load_state()
                need_audio = bool(body.get("need_audio", True))
                threading.Thread(target=board_film_job, args=(st, need_audio), daemon=True).start()
                self._send(200, {"ok": True, "started": True})
            elif self.path == "/api/edit_frames":
                body = self._body()
                st = load_state()
                scene = next((s for s in st["scenes"] if s.get("id") == str(body.get("scene_id") or "")), None)
                if not scene:
                    self._send(200, {"ok": False, "error": "镜头不存在"})
                    return
                try:
                    idx = None if body.get("index") in (None, "") else int(body.get("index"))
                except (TypeError, ValueError):
                    self._send(200, {"ok": False, "error": "index 必须是整数"})
                    return
                states = scene.get("states") or []
                if idx is not None:
                    if not 0 <= idx < len(states) - 1:
                        self._send(200, {"ok": False, "error": "这一段不存在"})
                        return
                    slot = states[idx]
                    video = slot.get("clip")
                else:
                    slot = scene
                    video = scene.get("output")
                if not video:
                    self._send(200, {"ok": False, "error": "这一镜还没有成片，先出视频再来改"})
                    return
                instruction = str(body.get("instruction") or "").strip()
                if not instruction:
                    self._send(200, {"ok": False, "error": "写一句要换成什么，例如「把红色杯子换成蓝色玻璃瓶」"})
                    return
                src = os.path.join(OUTPUT_DIR, video.lstrip("/"))
                if not os.path.isfile(src):
                    self._send(200, {"ok": False, "error": "视频文件不在：" + video})
                    return
                en = _translate_prompt(instruction, EDIT_FRAME_SYS)
                if not en:
                    self._send(200, {"ok": False, "error": "提示词翻译失败：需要本机 Ollama 对话模型或 DEEPSEEK_API_KEY"})
                    return
                try:
                    steps = max(8, min(40, int(body.get("steps") or 20)))
                except (TypeError, ValueError):
                    steps = 20
                frames_dir = os.path.join(INPUT_DIR, "frames")
                os.makedirs(frames_dir, exist_ok=True)
                stamp = time.strftime("%H%M%S") + "_" + str(random.randrange(100, 999))
                tag = re.sub(r"[^0-9a-zA-Z_-]", "", scene["id"]) + ("_s%d" % idx if idx is not None else "")
                # 视频里的物体一直在动，片段的首尾帧得一起改，否则重出时物体又会弹回原样
                grabs = [("first", extract_first_frame)]
                if idx is not None:
                    grabs.append(("last", extract_last_frame))
                made = {}
                for name, grab in grabs:
                    base = "edit_%s_%s_%s.png" % (tag, name, stamp)
                    try:
                        grab(src, os.path.join(frames_dir, base))
                    except Exception as e:
                        self._send(200, {"ok": False, "error": "抽帧失败：" + str(e)})
                        return
                    graph = build_graph_kontext_edit("frames/" + base, en, random.randrange(0, 2 ** 31), steps,
                                                     "frames/editout_%s_%s_%s" % (tag, name, stamp))
                    save_workflow_files(graph, "换物体_" + name)
                    busy, job = _gen_acquire("换物体-" + tag + "-" + name, 1800)
                    if busy:
                        self._send(200, {"ok": False, "busy": True, "error": busy})
                        return
                    try:
                        r = _submit_prompt(graph, timeout=1800, images_all=True)
                    finally:
                        _gen_release(job)
                    if not r.get("ok"):
                        self._send(200, r)
                        return
                    files = []
                    for rel in r.get("files") or []:
                        p = os.path.join(_COMFY_ROOT, "output", rel.replace("/", os.sep))
                        if not os.path.isfile(p):
                            continue
                        shutil.move(p, os.path.join(frames_dir, os.path.basename(rel)))
                        files.append("frames/" + os.path.basename(rel))
                    if not files:
                        self._send(200, {"ok": False, "error": "编辑帧生成失败：无图片输出"})
                        return
                    made[name] = files
                st = load_state()
                for s in st["scenes"]:
                    if s.get("id") != scene["id"]:
                        continue
                    target = s["states"][idx] if idx is not None else s
                    target["edit"] = {"instruction": instruction, "prompt_en": en, "video": video,
                                      "frames": made, "pick": {}, "stamp": stamp}
                    break
                save_state(st)
                self._send(200, {"ok": True, "frames": made, "prompt_en": en, "video": video})
            elif self.path == "/api/apply_edit":
                body = self._body()
                st = load_state()
                scene = next((s for s in st["scenes"] if s.get("id") == str(body.get("scene_id") or "")), None)
                if not scene:
                    self._send(200, {"ok": False, "error": "镜头不存在"})
                    return
                try:
                    idx = None if body.get("index") in (None, "") else int(body.get("index"))
                except (TypeError, ValueError):
                    self._send(200, {"ok": False, "error": "index 必须是整数"})
                    return
                states = scene.get("states") or []
                if idx is not None and not 0 <= idx < len(states) - 1:
                    self._send(200, {"ok": False, "error": "这一段不存在"})
                    return
                edit = (states[idx] if idx is not None else scene).get("edit") or {}
                first = str(body.get("first") or (edit.get("pick") or {}).get("first") or "").strip()
                last = str(body.get("last") or (edit.get("pick") or {}).get("last") or "").strip()
                if not first:
                    self._send(200, {"ok": False, "error": "先选一张改好的首帧"})
                    return
                # 只认 input/frames 下真实存在的文件，别让请求里的路径随便指
                frames_dir = os.path.join(INPUT_DIR, "frames")
                for rel in (first, last):
                    if not rel:
                        continue
                    if not os.path.isfile(os.path.join(frames_dir, os.path.basename(rel.replace("\\", "/")))):
                        self._send(200, {"ok": False, "error": "编辑帧不在：" + rel})
                        return
                if idx is not None:
                    prepared = segment_scene(scene, states, idx, first=first,
                                             last=last or states[idx + 1].get("keyframe"),
                                             seed=states[idx].get("seed"))
                else:
                    seg = dict(scene)
                    seg.update(first_frame=first, seed=scene.get("seed"),
                               prefix="video/edit_%s_%s" % (re.sub(r"[^0-9a-zA-Z_-]", "", scene["id"]),
                                                            time.strftime("%H%M%S")))
                    if last:
                        seg["last_frame"] = last
                    seg.pop("output", None)
                    seg.pop("edit", None)
                    prepared = prepare_scene(seg)
                busy, job = _gen_acquire("换物体重出-%s-%s" % (scene["id"], idx), 3600)
                if busy:
                    self._send(200, {"ok": False, "busy": True, "error": busy})
                    return
                try:
                    r = generate_scene(prepared)
                except Exception as e:
                    r = {"ok": False, "error": str(e)}
                _gen_release(job)
                if not r.get("ok"):
                    self._send(200, r)
                    return
                fresh = load_state()
                for s in fresh["scenes"]:
                    if s.get("id") != scene["id"]:
                        continue
                    if idx is not None:
                        target = s["states"][idx]
                        target["clip_prev"] = target.get("clip")
                        target["clip"] = r["file"]
                        add_version(target, "clip", [r["file"]], "edit")
                    else:
                        s["output_prev"] = s.get("output")
                        s["output"] = r["file"]
                        add_version(s, "clip", [r["file"]], "edit")
                    target = s["states"][idx] if idx is not None else s
                    target["edit"] = {}
                    break
                save_state(fresh)
                self._send(200, {"ok": True, "video": r["file"], "index": idx})
            elif self.path == "/api/video_inpaint":
                body = self._body()
                st = load_state()
                scene = next((s for s in st["scenes"] if s.get("id") == str(body.get("scene_id") or "")), None)
                if not scene:
                    self._send(200, {"ok": False, "error": "镜头不存在"})
                    return
                try:
                    idx = None if body.get("index") in (None, "") else int(body.get("index"))
                except (TypeError, ValueError):
                    self._send(200, {"ok": False, "error": "index 必须是整数"})
                    return
                states = scene.get("states") or []
                if idx is not None:
                    if not 0 <= idx < len(states) - 1:
                        self._send(200, {"ok": False, "error": "这一段不存在"})
                        return
                    slot = states[idx]
                    video = slot.get("clip")
                else:
                    slot = scene
                    video = scene.get("output")
                if not video:
                    self._send(200, {"ok": False, "error": "这一镜还没有成片，先出视频再来修补"})
                    return
                target = str(body.get("target") or "").strip()
                if not target:
                    self._send(200, {"ok": False, "error": "写一句要修掉或换掉什么，例如「画面右边穿蓝衣服的人」"})
                    return
                src = os.path.join(OUTPUT_DIR, video.lstrip("/"))
                if not os.path.isfile(src):
                    self._send(200, {"ok": False, "error": "视频文件不在：" + video})
                    return
                target_en = _translate_prompt(target, SAM3_SYS)
                if not target_en:
                    self._send(200, {"ok": False, "error": "目标描述翻译失败：需要本机 Ollama 对话模型或 DEEPSEEK_API_KEY"})
                    return
                target_en = target_en.strip().strip('"').strip(".")[:120]
                w, h = resolve_geometry({"aspect": scene.get("aspect") or "16:9",
                                         "resolution": scene.get("resolution") or "480p"}, snap=16)
                try:
                    steps = max(8, min(40, int(body.get("steps") or 20)))
                    expand = max(0, min(200, int(body.get("expand") or 20)))
                    threshold = max(0.05, min(0.95, float(body.get("threshold") or 0.5)))
                except (TypeError, ValueError):
                    steps, expand, threshold = 20, 20, 0.5
                prompt = str(body.get("prompt") or "").strip() or "背景自然延续，画面干净，光影一致"
                stamp = time.strftime("%H%M%S") + "_" + str(random.randrange(100, 999))
                tag = re.sub(r"[^0-9a-zA-Z_-]", "", scene["id"]) + ("_s%d" % idx if idx is not None else "") + "_" + stamp
                try:
                    frames_dir, n = prep_vace_frames(src, tag, w, h)
                except Exception as e:
                    self._send(200, {"ok": False, "error": str(e)})
                    return
                if n > VACE_MAX_FRAMES:
                    shutil.rmtree(frames_dir, ignore_errors=True)
                    self._send(200, {"ok": False, "error": "这段按 %d fps 抽出来 %d 帧（约 %.1f 秒），超过 VACE 单次上限 %d 帧；"
                                     "请把它切短再修，或在画布上只对某一段片段做修补"
                                     % (VACE_FPS, n, n / float(VACE_FPS), VACE_MAX_FRAMES)})
                    return
                seed = int(body.get("seed") or 0) or random.randrange(0, 2 ** 31)
                graph = build_graph_vace_inpaint(frames_dir, target_en, prompt, w, h, n, seed, steps,
                                                 expand, threshold, "video/inpaint_" + tag)
                save_workflow_files(graph, "视频修补_VACE")
                busy, job = _gen_acquire("视频修补-" + tag, 3600)
                if busy:
                    shutil.rmtree(frames_dir, ignore_errors=True)
                    self._send(200, {"ok": False, "busy": True, "error": busy})
                    return
                try:
                    r = _submit_prompt(graph, timeout=3600)
                except Exception as e:
                    r = {"ok": False, "error": str(e)}
                finally:
                    _gen_release(job)
                    shutil.rmtree(frames_dir, ignore_errors=True)
                if not r.get("ok"):
                    self._send(200, r)
                    return
                # VACE 出来的没有声音，原片有音轨就接回去
                out_rel = r["file"]
                out_abs = os.path.join(OUTPUT_DIR, out_rel.lstrip("/"))
                if os.path.isfile(out_abs) and _has_audio(src):
                    muxed = os.path.join(OUTPUT_DIR, "_mux_" + tag + ".mp4")
                    rr = _ffmpeg_run([_ffmpeg(), "-y", "-i", out_abs, "-i", src, "-map", "0:v:0", "-map", "1:a:0",
                                      "-c:v", "copy", "-c:a", "aac", "-shortest", muxed])
                    if rr.returncode == 0 and os.path.isfile(muxed):
                        os.replace(muxed, out_abs)
                fresh = load_state()
                for s in fresh["scenes"]:
                    if s.get("id") != scene["id"]:
                        continue
                    if idx is not None:
                        t2 = s["states"][idx]
                        t2["clip_prev"] = t2.get("clip")
                        t2["clip"] = out_rel
                        add_version(t2, "clip", [out_rel], "inpaint", seed)
                    else:
                        s["output_prev"] = s.get("output")
                        s["output"] = out_rel
                        add_version(s, "clip", [out_rel], "inpaint", seed)
                    break
                save_state(fresh)
                self._send(200, {"ok": True, "video": out_rel, "frames": n, "seed": seed,
                                 "target": target, "target_en": target_en, "size": [w, h]})
            elif self.path == "/api/set_version":
                body = self._body()
                st = load_state()
                scene = next((s for s in st["scenes"] if s.get("id") == str(body.get("scene_id") or "")), None)
                if not scene:
                    self._send(200, {"ok": False, "error": "镜头不存在"})
                    return
                try:
                    idx = None if body.get("index") in (None, "") else int(body.get("index"))
                except (TypeError, ValueError):
                    self._send(200, {"ok": False, "error": "index 必须是整数"})
                    return
                field = str(body.get("field") or "keyframe")
                if field not in ("keyframe", "clip"):
                    self._send(200, {"ok": False, "error": "field 只能是 keyframe 或 clip"})
                    return
                states = scene.get("states") or []
                if idx is not None:
                    if not 0 <= idx < len(states):
                        self._send(200, {"ok": False, "error": "变化帧不存在"})
                        return
                    slot = states[idx]
                else:
                    slot = scene
                rel = str(body.get("rel") or "").strip()
                if rel not in (slot.get(field + "_versions") or []):
                    self._send(200, {"ok": False, "error": "这个版本不在记录里"})
                    return
                base = INPUT_DIR if field == "keyframe" else OUTPUT_DIR
                p = os.path.join(base, rel.replace("/", os.sep))
                if not os.path.isfile(p) or not os.path.abspath(p).startswith(os.path.abspath(base) + os.sep):
                    self._send(200, {"ok": False, "error": "文件不在：" + rel})
                    return
                if field == "clip":
                    slot["clip_prev"] = slot.get("clip")
                slot[field] = rel
                save_state(st)
                self._send(200, {"ok": True, "field": field, "rel": rel, "index": idx})
            elif self.path == "/api/board":
                body = self._body()
                positions = body.get("positions")
                if not isinstance(positions, dict):
                    self._send(200, {"ok": False, "error": "positions 必须是对象"})
                    return
                clean = {}
                for k, v in positions.items():
                    if not isinstance(v, dict):
                        continue
                    try:
                        clean[str(k)] = {"x": float(v.get("x") or 0), "y": float(v.get("y") or 0)}
                    except (TypeError, ValueError):
                        continue
                st = load_state()
                st["board"] = clean
                save_state(st)
                self._send(200, {"ok": True, "positions": clean})
            elif self.path == "/api/save_assets":
                body = self._body()
                items = body.get("assets")
                if not isinstance(items, list):
                    self._send(200, {"ok": False, "error": "assets 必须是列表"})
                    return
                st = load_state()
                st["assets"] = items
                linked = link_all_assets(st)      # 池子变了，连线跟着重算
                save_state(st)
                self._send(200, {"ok": True, "linked": linked, "scenes": st["scenes"]})
            elif self.path == "/api/link_assets":
                # 手工排过引用的镜头（refs_manual），这一下就是把它交回自动连线
                body = self._body()
                st = load_state()
                sid = str(body.get("scene_id") or "")
                n = 0
                for s in st["scenes"]:
                    if sid and s.get("id") != sid:
                        continue
                    s.pop("refs_manual", None)
                    if link_scene_assets(s, st, force=True):
                        n += 1
                save_state(st)
                self._send(200, {"ok": True, "linked": n, "scenes": st["scenes"]})
            elif self.path == "/api/delete_asset":
                body = self._body()
                st = load_state()
                aid = str(body.get("asset_id") or "")
                keep = [a for a in (st.get("assets") or []) if str(a.get("id")) != aid]
                if len(keep) == len(st.get("assets") or []):
                    self._send(200, {"ok": False, "error": "资产不存在"})
                    return
                st["assets"] = keep
                for s in st["scenes"]:
                    # 连线里还留着这条 id 的一并摘掉，别留一个指不到东西的引用
                    refs = [r for r in (s.get("refs") or []) if str(r) != aid]
                    if len(refs) != len(s.get("refs") or []):
                        s["refs"] = refs
                        s["ref_images"] = [e for e in (s.get("ref_images") or []) if str(e.get("id")) != aid]
                link_all_assets(st)
                save_state(st)
                self._send(200, {"ok": True, "assets": st["assets"], "scenes": st["scenes"]})
            elif self.path == "/api/save_scenes":
                body = self._body()
                scenes = body.get("scenes")
                if not isinstance(scenes, list):
                    self._send(200, {"ok": False, "error": "scenes 必须是列表"})
                    return
                st = load_state()
                st["scenes"] = scenes
                link_all_assets(st)
                save_state(st)
                # 连线是后端算的，把结果发回去，画布上那些边才会跟着动
                self._send(200, {"ok": True, "scenes": st["scenes"]})
            elif self.path == "/api/upload_voice":
                body = self._body()
                scene_id = str(body.get("scene_id") or "scene")
                ext = str(body.get("ext") or "mp3").lower()
                if ext not in ("wav", "mp3", "flac", "ogg", "m4a", "aac"):
                    self._send(200, {"ok": False, "error": "仅支持 wav/mp3/flac/ogg/m4a/aac 音频"})
                    return
                raw = base64.b64decode(body.get("data_b64") or "")
                if not raw or len(raw) > 100 * 1024 * 1024:
                    self._send(200, {"ok": False, "error": "音频无效或超过 100MB"})
                    return
                vdir = os.path.join(INPUT_DIR, "voice")
                os.makedirs(vdir, exist_ok=True)
                name = "voice_" + re.sub(r"[^0-9a-zA-Z_-]", "", scene_id) + "_" + time.strftime("%H%M%S") + "_" + str(random.randrange(1000, 9999)) + "." + ext
                with open(os.path.join(vdir, name), "wb") as f:
                    f.write(raw)
                rel = "voice/" + name
                st = load_state()
                for sc in st["scenes"]:
                    if sc.get("id") == scene_id:
                        sc["voice"] = rel
                        break
                save_state(st)
                self._send(200, {"ok": True, "file": rel})
            elif self.path == "/api/upload_guide":
                body = self._body()
                scene_id = str(body.get("scene_id") or "scene")
                ext = str(body.get("ext") or "png").lower()
                if ext not in ("png", "jpg", "jpeg", "webp"):
                    self._send(200, {"ok": False, "error": "仅支持 png/jpg/webp 图片"})
                    return
                raw = base64.b64decode(body.get("data_b64") or "")
                if not raw or not _looks_like_image(raw, ext):
                    self._send(200, {"ok": False, "error": "图片内容无效"})
                    return
                frames_dir = os.path.join(INPUT_DIR, "frames")
                os.makedirs(frames_dir, exist_ok=True)
                name = "guide_" + re.sub(r"[^0-9a-zA-Z_-]", "", scene_id) + "_" + time.strftime("%H%M%S") + "_" + str(random.randrange(1000, 9999)) + "." + ext
                with open(os.path.join(frames_dir, name), "wb") as f:
                    f.write(raw)
                rel = "frames/" + name
                st = load_state()
                for sc in st["scenes"]:
                    if sc.get("id") == scene_id:
                        sc.setdefault("guides", [])
                        sc["guides"].append(rel)
                        break
                save_state(st)
                self._send(200, {"ok": True, "file": rel})
            elif self.path == "/api/remove_guide":
                body = self._body()
                scene_id = str(body.get("scene_id") or "scene")
                fname = str(body.get("file") or "")
                st = load_state()
                for sc in st["scenes"]:
                    if sc.get("id") == scene_id:
                        gs = sc.get("guides") or []
                        if fname in gs:
                            gs.remove(fname)
                            sc["guides"] = gs
                        try:
                            fp = os.path.join(INPUT_DIR, fname.replace("/", os.sep))
                            if os.path.isfile(fp) and os.path.abspath(fp).startswith(os.path.abspath(INPUT_DIR) + os.sep):
                                os.remove(fp)
                        except Exception:
                            pass
                        break
                save_state(st)
                self._send(200, {"ok": True})
            elif self.path in ("/api/character", "/api/scene", "/api/asset_info"):
                # 三种入口都是「写池子里的某条资产」：画布上的资产卡按 asset_id 找，
                # 全局设置面板按 kind 找该类第一条（迁移后就是老的角色/场景/设备槽位）。
                body = self._body()
                st = load_state()
                aid = str(body.get("asset_id") or "").strip()
                kind = str(body.get("kind") or "").strip() or {"/api/character": "character",
                                                               "/api/scene": "scene"}.get(self.path, "")
                if aid:
                    a = _find_asset(st, aid)
                    if a is None:
                        self._send(200, {"ok": False, "error": "资产不存在"})
                        return
                elif kind in ASSET_SPECS:
                    a = _asset_of_kind(st, kind)
                else:
                    self._send(200, {"ok": False, "error": "需要 asset_id，或 kind 取 " + " / ".join(ASSET_SPECS)})
                    return
                if body.get("kind") in ASSET_SPECS:
                    a["kind"] = body["kind"]
                if body.get("name") is not None:
                    a["name"] = str(body.get("name") or "").strip()
                if body.get("description") is not None:
                    a["description"] = str(body.get("description") or "").strip()
                if body.get("image") is not None:
                    a["file"] = body.get("image") or None
                link_all_assets(st)      # 名字变了，谁连这条资产也跟着变
                save_state(st)
                self._send(200, {"ok": True, "asset": a, "assets": st["assets"],
                                 "scenes": st["scenes"], "kind": a.get("kind")})
            elif self.path == "/api/global":
                body = self._body()
                st = load_state()
                st["global_negative_prompt"] = str(body.get("negative_prompt") or "").strip()
                save_state(st)
                self._send(200, {"ok": True, "global_negative_prompt": st["global_negative_prompt"]})
            elif self.path == "/api/project/new":
                body = self._body()
                name = str(body.get("name") or "").strip()[:40] or ("新项目 " + time.strftime("%m-%d %H:%M"))
                pid = "p" + time.strftime("%Y%m%d%H%M%S") + "_" + str(random.randrange(100, 999))
                os.makedirs(PROJECTS_DIR, exist_ok=True)
                p = os.path.join(PROJECTS_DIR, pid + ".json")
                if not os.path.abspath(p).startswith(os.path.abspath(PROJECTS_DIR) + os.sep):
                    self._send(200, {"ok": False, "error": "非法项目 ID"})
                    return
                json.dump({"name": name, "scenes": [], "character": {}}, open(p, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=2)
                json.dump({"id": pid}, open(CURRENT_PROJECT_FILE, "w", encoding="utf-8"))
                self._send(200, {"ok": True, "project": {"id": pid, "name": name}})
            elif self.path == "/api/project/select":
                body = self._body()
                pid = str(body.get("id") or "default")
                if pid != "default" and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", pid):
                    self._send(200, {"ok": False, "error": "非法项目 ID"})
                    return
                p = STATE_FILE if pid == "default" else os.path.join(PROJECTS_DIR, pid + ".json")
                if pid != "default" and not (os.path.abspath(p).startswith(os.path.abspath(PROJECTS_DIR) + os.sep) and os.path.isfile(p)):
                    self._send(200, {"ok": False, "error": "项目不存在"})
                    return
                json.dump({"id": pid}, open(CURRENT_PROJECT_FILE, "w", encoding="utf-8"))
                self._send(200, {"ok": True, "project": {"id": pid}})
            elif self.path == "/api/project/delete":
                pid = str(self._body().get("id") or "")
                if pid == "default":
                    self._send(200, {"ok": False, "error": "默认项目不能删除"})
                    return
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", pid):
                    self._send(200, {"ok": False, "error": "非法项目 ID"})
                    return
                # 项目 ID 不含点号，pid + "." 只命中本项目自己的存档与备份
                removed = 0
                if os.path.isdir(PROJECTS_DIR):
                    for f in sorted(os.listdir(PROJECTS_DIR)):
                        fp = os.path.join(PROJECTS_DIR, f)
                        if f.startswith(pid + ".") and os.path.isfile(fp):
                            os.remove(fp)
                            removed += 1
                if not removed:
                    self._send(200, {"ok": False, "error": "项目不存在"})
                    return
                if _current_project_id() == pid:
                    json.dump({"id": "default"}, open(CURRENT_PROJECT_FILE, "w", encoding="utf-8"))
                self._send(200, {"ok": True, "id": pid})
            elif self.path == "/api/concat":
                body = self._body()
                clips = body.get("clips", [])
                subs = body.get("subtitles", [])
                if not body.get("subtitle", True):
                    subs = []
                need_audio = bool(body.get("need_audio", True))
                # bridge=True 走智能合成（两镜之间用 H3 首尾关键帧补过渡帧），False 直接串接
                r = start_smart_concat(clips, subs, need_audio, subtitle=bool(body.get("subtitle", True)), bridge=bool(body.get("bridge", True)))
                self._send(200, r)
            elif self.path == "/api/generate_segments":
                if SEGMENT_STATUS.get("running"):
                    self._send(200, {"ok": True, "started": False, "busy": True})
                else:
                    st = load_state()
                    threading.Thread(target=generate_segments_job, args=(st,), daemon=True).start()
                    self._send(200, {"ok": True, "started": True})
            elif self.path == "/api/assets/upload":
                body = self._body()
                raw = base64.b64decode(body.get("data_b64") or "")
                ext = str(body.get("ext") or "png").lower()
                if ext not in ("png", "jpg", "jpeg", "webp"):
                    self._send(200, {"ok": False, "error": "素材库仅支持 png/jpg/webp 图片"})
                    return
                if not raw or len(raw) > 50 * 1024 * 1024 or not _looks_like_image(raw, ext):
                    self._send(200, {"ok": False, "error": "图片内容无效或超过 50MB"})
                    return
                os.makedirs(ASSETS_DIR, exist_ok=True)
                stem = re.sub(r"[^0-9a-zA-Z_-]", "", str(body.get("filename") or "").split(".")[0])[:40] or "asset"
                fname = stem + "_" + time.strftime("%H%M%S") + "_" + str(random.randrange(1000, 9999)) + "." + ext
                with open(os.path.join(ASSETS_DIR, fname), "wb") as f:
                    f.write(raw)
                st = os.stat(os.path.join(ASSETS_DIR, fname))
                aid = _mysql_set_asset(fname, fname, st.st_size, int(st.st_mtime), "", "upload")
                _neo4j_index_assets([{"path": fname, "name": fname, "tags": ""}], prune=False)
                self._send(200, {"ok": True, "file": fname, "id": aid})
            elif self.path == "/api/assets/delete":
                rel = (self._body().get("path") or "").replace("\\", "/").lstrip("/")
                try:
                    p = resolve_asset_path(rel)
                    if os.path.isfile(p):
                        os.remove(p)
                        _mysql_delete_asset(rel)
                        _neo4j_unindex_asset(rel)
                        self._send(200, {"ok": True})
                    else:
                        self._send(200, {"ok": False, "error": "文件不存在"})
                except ValueError as ve:
                    self._send(200, {"ok": False, "error": str(ve)})
            elif self.path == "/api/outputs/delete":
                rel = (self._body().get("path") or "").replace("\\", "/").lstrip("/")
                try:
                    p = resolve_output_path(rel)
                    if os.path.isfile(p):
                        os.remove(p)
                        self._send(200, {"ok": True})
                    else:
                        self._send(200, {"ok": False, "error": "文件不存在"})
                except ValueError as ve:
                    self._send(200, {"ok": False, "error": str(ve)})
            elif self.path == "/api/script_ai":
                body = self._body()
                topic = (body.get("topic") or "").strip()
                if not topic:
                    self._send(200, {"ok": False, "error": "主题不能为空"})
                else:
                    self._send(200, gen_script_with_ai(topic, body.get("api_key"), body.get("base_url"), body.get("model"), body.get("sys_prompt")))
            elif self.path == "/api/assets/tag":
                body = self._body()
                p = (body.get("path") or "").replace("\\", "/").lstrip("/")
                self._send(200, tag_asset_with_ai(p, body.get("api_key"), body.get("base_url"), body.get("model"), body.get("prompt")))
            elif self.path == "/api/assets/tag_manual":
                body = self._body()
                pth = (body.get("path") or "").replace(chr(92), "/").lstrip("/")
                t = str(body.get("tags") or "").strip()
                data = _load_json(TAGS_FILE, {})
                data[pth] = t
                try:
                    json.dump(data, open(TAGS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
                except Exception:
                    pass
                _mysql_set_asset(pth, os.path.basename(pth), tags=t, source="manual")
                _neo4j_index_assets([{"path": pth, "name": os.path.basename(pth), "tags": t}], prune=False)
                self._send(200, {"ok": True, "tags": t})
            elif self.path == "/api/knowledge":
                g = self._body().get("graph") or {}
                ok, err = _neo4j_write_graph(g.get("nodes") or [], g.get("edges") or [])
                if ok:
                    self._send(200, {"ok": True})
                else:
                    self._send(200, {"ok": False, "error": err})
            else:
                self._send(404, {"error": "not found"})
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "error": "无效的 JSON 请求体"})
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send(500, {"ok": False, "error": str(e)})

    def log_message(self, *a):
        pass


def main():
    port = int(os.environ.get("PORT", "9081"))
    threading.Thread(target=_live_loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("script-to-video 导演台 启动: http://127.0.0.1:%d" % port)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
