#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""script-to-video 导演台后端（纯标准库，无第三方依赖）。

导演台模式：分镜表携带导演参数（景别/运镜/比例/步数/种子），
后端负责把它们折叠进最终提示词，并提交 ComfyUI 生成。

提供：
  GET  /                   前端页面
  GET  /api/state          已保存的分镜状态（刷新页面后恢复）
  GET  /api/status         ComfyUI 队列状态
  POST /api/optimize       脚本 -> 分镜（含导演参数）
  POST /api/generate       单镜生成（提交 ComfyUI 并轮询）
  POST /api/concat         合成 + 字幕
"""
import base64, io, json, os, random, re, shutil, subprocess, sys, threading, time, urllib.request, urllib.error, zipfile, uuid
from collections import deque
from urllib.parse import unquote, urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", os.path.join(_COMFY_ROOT, "output", "video"))
INPUT_DIR = os.environ.get("INPUT_DIR", os.path.join(_COMFY_ROOT, "input"))
FONT = os.environ.get("FONT", "C:/Windows/Fonts/simhei.ttf")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

MODELS = {
    "unet": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "vae_video": "minimax_h3_video_vae_fp16.safetensors",
    "vae_audio": "minimax_h3_audio_vae_fp32.safetensors",
}

# ---- 导演参数 ----
SHOTS = ["远景", "全景", "中景", "近景", "特写", "大特写"]
CAMERAS = ["固定镜头", "缓慢推近", "缓慢拉远", "水平摇镜", "垂直摇镜", "跟随镜头", "手持晃动", "环绕运镜"]
ASPECT_RATIOS = {"16:9": (16, 9), "9:16": (9, 16), "1:1": (1, 1), "21:9": (21, 9)}
RESOLUTIONS = {"480p": 480, "720p": 720, "1080p": 1080, "2K": 1440, "4K": 2160}
STYLE_SUFFIX = "电影质感，真实实拍风格，无字幕无水印。"
MODES = {"标准": {"steps": 25, "turbo": False}, "均衡": {"steps": 12, "turbo": False}, "高清": {"steps": 32, "turbo": False}, "极速": {"steps": 4, "turbo": True}}
TURBO_LORA = "minimax_h3_turbo_4step_ema_ckpt850.safetensors"
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


def compose_prompt(scene):
    """把画面描述 + 导演参数折叠成最终提示词。"""
    prompt = (scene.get("prompt") or "").strip()
    while prompt.endswith(("。", "！", "？", "，", "、", " ")):
        prompt = prompt[:-1]
    shot = scene.get("shot") or "中景"
    camera = scene.get("camera") or "固定镜头"
    neg = (scene.get("negative_prompt") or "").strip()
    char = scene.get("character") or {}
    char_desc = (char.get("description") or "").strip()
    char_name = (char.get("name") or "").strip()
    segs = []
    if char_desc:
        segs.append("角色（全程固定同一人）：" + (char_name + "，" if char_name else "") + char_desc)
    if prompt:
        segs.append(prompt)
    segs.append("景别：" + shot)
    segs.append("运镜：" + camera)
    s = "。".join(segs) + "。"
    if neg:
        s += "避免：" + neg + "。"
    return s + (scene.get("style_suffix") or STYLE_SUFFIX)


def prepare_scene(scene):
    """补全默认值、解析几何、计算帧数、折叠提示词。"""
    s = dict(scene)
    s.setdefault("shot", "中景")
    s.setdefault("camera", "固定镜头")
    s.setdefault("aspect", "16:9")
    s.setdefault("resolution", "480p")
    s.setdefault("mode", "标准")
    s.setdefault("steps", 25)
    s.setdefault("negative_prompt", "")
    s.setdefault("model", "MiniMax H3")
    s.setdefault("guides", [])
    s["seconds"] = max(3, min(15, int(s.get("seconds") or 5)))
    if s["model"] == "Wan2.2 14B":
        s["length"] = s["seconds"] * 16 + 1
        s["width"], s["height"] = resolve_geometry(s, snap=16)
    elif s["model"] == "Wan2.2 5B":
        s["length"] = s["seconds"] * 16 + 1
        s["width"], s["height"] = resolve_geometry(s, snap=32)
    else:
        s["length"] = seconds_to_length(s["seconds"])
        s["width"], s["height"] = resolve_geometry(s)
    s["steps"] = int(s.get("steps") or 25)
    s["seed"] = int(s["seed"]) if s.get("seed") else None
    s["prompt"] = compose_prompt(s)
    if not s.get("first_frame") and s.get("use_character_frame"):
        ch = s.get("character") or {}
        if ch.get("image"):
            s["first_frame"] = ch["image"]
    return s


def build_graph(scene):
    if scene.get("model") == "Wan2.2 5B":
        return build_graph_wan22(scene)
    if scene.get("model") == "Wan2.2 14B":
        return build_graph_wan22_14b(scene)
    return build_graph_h3(scene)


def build_graph_wan22(scene):
    prompt = scene["prompt"]
    width = scene.get("width", 832)
    height = scene.get("height", 480)
    length = scene.get("length") or (max(3, min(15, int(scene.get("seconds") or 5))) * 16 + 1)
    seed = scene.get("seed") or random.randrange(0, 2 ** 31)
    mode = scene.get("mode") or "标准"
    steps = scene.get("steps") or WAN_MODE_STEPS.get(mode, 30)
    neg = (scene.get("negative_prompt") or "").strip() or WAN_DEFAULT_NEGATIVE
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
    if voice:
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
    neg = (scene.get("negative_prompt") or "").strip() or WAN_DEFAULT_NEGATIVE
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
    if voice:
        graph["16"] = {"class_type": "LoadAudio", "inputs": {"audio": voice}}
        graph["14"]["inputs"]["audio"] = ["16", 0]
    return graph


def build_graph_h3(scene):
    prompt = scene["prompt"]
    length = scene.get("length") or seconds_to_length(scene.get("seconds", 5))
    width = scene.get("width", 864)
    height = scene.get("height", 480)
    seed = scene.get("seed") or random.randrange(0, 2 ** 31)
    mode_cfg = MODES.get(scene.get("mode") or "标准", MODES["标准"])
    steps = scene.get("steps") or mode_cfg["steps"]
    turbo = bool(mode_cfg.get("turbo"))
    prefix = scene.get("prefix", "video/script2video")
    graph = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": MODELS["clip"], "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["vae_video"]}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["vae_audio"]}},
        "5": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {"clip": ["2", 0], "vae": ["3", 0], "prompt": prompt, "width": width, "height": height, "length": length}},
        "15": {"class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch", "inputs": {"model": ["16" if turbo else "1", 0]}},
        "6": {"class_type": "BasicGuider", "inputs": {"model": ["15", 0], "conditioning": ["5", 0]}},
        "7": {"class_type": "BasicScheduler", "inputs": {"model": ["15", 0], "scheduler": "simple", "steps": steps, "denoise": 1.0}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "10": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["9", 0], "guider": ["6", 0], "sampler": ["8", 0], "sigmas": ["7", 0], "latent_image": ["5", 1]}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["3", 0]}},
        "12": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["10", 0], "vae": ["4", 0]}},
        "13": {"class_type": "CreateVideo", "inputs": {"images": ["11", 0], "fps": 24.0, "audio": ["12", 0]}},
        "14": {"class_type": "SaveVideo", "inputs": {"video": ["13", 0], "filename_prefix": prefix, "format": "auto"}},
    }
    if turbo:
        graph["16"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": TURBO_LORA, "strength_model": 1.0}}
    first_frame = scene.get("first_frame")
    if first_frame:
        graph["17"] = {"class_type": "LoadImage", "inputs": {"image": first_frame}}
        graph["5"]["inputs"]["first_frame"] = ["17", 0]
    guides = scene.get("guides") or []
    if guides:
        cond_src, latent_src = ["5", 0], ["5", 1]
        n = len(guides)
        for i, g in enumerate(guides):
            lnid, nid = str(19 + i * 2), str(20 + i * 2)
            frame_idx = max(1, min(int(length) - 1, round(int(length) * (i + 1) / (n + 1))))
            graph[lnid] = {"class_type": "LoadImage", "inputs": {"image": g}}
            graph[nid] = {"class_type": "MiniMaxH3AddGuide", "inputs": {"positive": cond_src, "latent": latent_src, "vae": ["3", 0], "image": [lnid, 0], "frame_idx": frame_idx}}
            cond_src = [nid, 0]
        graph["6"]["inputs"]["conditioning"] = cond_src
    voice = scene.get("voice")
    if voice:
        graph["18"] = {"class_type": "LoadAudio", "inputs": {"audio": voice}}
        graph["13"]["inputs"]["audio"] = ["18", 0]
    return graph


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


def _input_specs(info):
    inp = info.get("input") or {}
    for section in ("required", "optional"):
        for name, spec in (inp.get(section) or {}).items():
            if isinstance(spec, (list, tuple)) and len(spec) >= 1:
                tspec = spec[0]
                opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            else:
                tspec, opts = spec, {}
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
        for name, tspec, opts, _opt in _input_specs(info):
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
        for name, _tspec, _opts, _opt in _input_specs(info):
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
        safe = name
        ui_path = os.path.join(UI_WORKFLOWS_DIR, safe + ".json")
        json.dump(graph_to_ui(graph), open(ui_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        json.dump(graph, open(os.path.join(UI_WORKFLOWS_DIR, safe + "_api.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("workflow saved:", ui_path)
    except Exception as e:
        print("workflow save failed:", e)


def _post(url, data):
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def _get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def generate_scene(scene):
    """scene 需已 prepare。提交生成并轮询，返回输出文件名（相对 output 目录）。"""
    graph = build_graph(scene)
    base = COMFY_URL.rstrip("/")
    save_workflow_files(graph, "Director_最新镜头_" + ("Wan22" if scene.get("model") == "Wan2.2 5B" else "H3"))
    r = _post(base + "/prompt", {"prompt": graph, "client_id": "web"})
    pid = r["prompt_id"]
    deadline = time.time() + scene.get("timeout", 1800)
    while time.time() < deadline:
        time.sleep(15)
        try:
            h = _get(base + "/history/" + pid)
        except Exception:
            continue
        e = h.get(pid)
        if e:
            st = e.get("status", {})
            if st.get("status_str") == "error":
                msgs = [m for m in st.get("messages", []) if isinstance(m, list) and m and m[0] == "execution_error"]
                err = msgs[-1][1].get("exception_message") if msgs else "unknown error"
                return {"ok": False, "error": err}
            if st.get("status_str") == "success":
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
                        return {"ok": True, "file": file}
    return {"ok": False, "error": "timeout"}


def _ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def concat_scenes(clips, subtitles):
    """clips: 绝对路径列表; subtitles: [{text,start,end,fontsize,position}]"""
    ff = _ffmpeg()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    final = os.path.join(OUTPUT_DIR, "script2video_final_" + time.strftime("%Y%m%d_%H%M%S") + ".mp4")
    listfile = os.path.join(OUTPUT_DIR, "_concat_list.txt")
    NL = chr(10)
    with open(listfile, "w", encoding="utf-8") as f:
        for c in clips:
            f.write("file '" + os.path.abspath(c).replace(os.sep, "/") + "'" + NL)
    tmp = final + ".concat.mp4"
    subprocess.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", tmp], check=True, capture_output=True)
    vf_parts = []
    BS = chr(92)
    for s in subtitles:
        text = s["text"].replace(":", BS + ":").replace("'", BS + "'")
        pos = s.get("position", "bottom")
        y = "(h-text_h)/2" if pos == "center" else "h-150"
        vf_parts.append(
            "drawtext=fontfile='" + FONT.replace(":", BS + ":") + "':text='" + text + "':"
            "fontcolor=white:fontsize=" + str(s.get("fontsize", 40)) + ":borderw=2:bordercolor=black:"
            "x=(w-text_w)/2:y=" + y + ":enable='between(t," + str(s["start"]) + "," + str(s["end"]) + ")'"
        )
    cmd = [ff, "-y", "-i", tmp, "-c:v", "libx264", "-crf", "18", "-c:a", "copy"]
    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]
    cmd.append(final)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr[-800:]}
    os.remove(listfile)
    os.remove(tmp)
    return {"ok": True, "file": os.path.basename(final)}


def list_assets():
    """列出 output 目录下所有视频/图片素材。"""
    base = os.path.abspath(OUTPUT_DIR)
    exts = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".png", ".jpg", ".jpeg", ".gif", ".webp")
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
    return out


def resolve_asset_path(rel):
    """把素材相对路径解析为绝对路径，并做包含性校验。"""
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
KNOWLEDGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "script_graph.json")
TAGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets_tags.json")

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


def _neo4j_driver():
    try:
        from neo4j import GraphDatabase
    except Exception:
        return None
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7688")
    user = os.environ.get("NEO4J_USER", "neo4j")
    pwd = os.environ.get("NEO4J_PASSWORD", "comfyui123")
    try:
        return GraphDatabase.driver(uri, auth=(user, pwd), connection_timeout=5)
    except Exception:
        return None


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
    finally:
        d.close()


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
    finally:
        d.close()


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
    finally:
        d.close()


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _llm_chat(base_url, api_key, model, messages, max_tokens=2000, timeout=180):
    url = base_url.rstrip("/") + "/chat/completions"
    data = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": False}
    req = urllib.request.Request(
        url, data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
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
    finally:
        d.close()


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


def auto_match_image(prompt):
    """按提示词检索素材库：文件名/标签里的关键词在提示词中出现越多分越高。只匹配图片。"""
    prompt = (prompt or "").lower()
    tags = _load_json(TAGS_FILE, {})
    best = None
    for a in list_assets():
        if is_video(a.get("path") or ""):
            continue
        score = 0
        stem = (a.get("name") or "").split(".")[0].lower()
        for kw in re.split(r"[^0-9a-z一-鿿]+", stem):
            if len(kw) >= 2 and kw in prompt:
                score += 2
        tag_text = (tags.get(a.get("path"), "") or "").lower()
        for kw in re.split(r"[,，|。:：]+", tag_text):
            kw = kw.strip()
            if len(kw) >= 2 and kw in prompt:
                score += 3
        if score > 0 and (best is None or score > best[0]):
            best = (score, a)
    if not best:
        return None
    score, a = best
    return {"path": a["path"], "name": a["name"], "score": score, "tags": tags.get(a["path"], "")}


def is_video(p):
    return bool(re.search(r"\.(mp4|webm|mov|mkv|avi)$", p, re.I))


# ---- 脚本解析/优化 ----
def _extract_docx_raw(raw):
    z = zipfile.ZipFile(io.BytesIO(raw))
    xml = z.read("word/document.xml").decode("utf-8", "ignore")
    paras = re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)
    out = []
    for p in paras:
        texts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, re.S)
        line = "".join(texts).strip()
        if line:
            out.append(line)
    return chr(10).join(out)


def _extract_xlsx_raw(raw):
    z = zipfile.ZipFile(io.BytesIO(raw))
    shared = []
    try:
        xml = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
        shared = ["".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S))
                  for si in re.findall(r"<si>.*?</si>", xml, re.S)]
    except KeyError:
        pass
    out = []
    for name in sorted(z.namelist()):
        if not re.match(r"xl/worksheets/sheet\d+\.xml$", name):
            continue
        xml = z.read(name).decode("utf-8", "ignore")
        for cell in re.findall(r"<c\b.*?</c>", xml, re.S):
            inline = "".join(re.findall(r"<t[^>]*>(.*?)</t>", cell, re.S))
            if inline:
                out.append(inline)
                continue
            m = re.search(r"<v[^>]*>(.*?)</v>", cell, re.S)
            if m and re.search(r't="s"', cell) and m.group(1).isdigit():
                out.append(shared[int(m.group(1))])
    return chr(10).join(out)


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
    """md/doc/docx/xls 提取纯文本（标准库尽力而为）。"""
    raw = base64.b64decode(b64)
    ext = os.path.splitext(filename or "")[1].lower().lstrip(".")
    if ext == "docx" or (ext == "doc" and raw[:2] == b"PK"):
        return _extract_docx_raw(raw)
    if ext == "xls" and raw[:2] == b"PK":
        return _extract_xlsx_raw(raw)
    if ext in ("md", "markdown", "txt"):
        for enc in ("utf-8-sig", "gbk"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "ignore")
    if ext == "doc" and raw.lstrip()[:5].lower() == b"{\\rtf":
        return _rtf_to_text(raw)
    if ext in ("doc", "xls"):
        return _extract_binary_text(raw)
    return _extract_docx_raw(raw) if raw[:2] == b"PK" else _extract_binary_text(raw)


def optimize_script(text):
    """把脚本解析成分镜列表（画面描述，不含风格后缀）。"""
    text = text.strip()
    lines = [l for l in text.splitlines() if l.strip()]
    # 1) 分镜表：含 | 或制表符的行
    tab_rows = []
    for l in lines:
        if "|" in l:
            cells = [c.strip() for c in l.strip().strip("|").split("|")]
            tab_rows.append(cells)
        elif "	" in l:
            tab_rows.append([c.strip() for c in l.split("	")])
    if len(tab_rows) >= 2:
        header = [h.lower() for h in tab_rows[0]]
        idx = {"t": -1, "pic": -1, "voice": -1, "audio": -1}
        for i, h in enumerate(header):
            if "时间" in h or "秒" in h or "s" == h: idx["t"] = i
            if "画面" in h or "分镜" in h or "镜头" in h: idx["pic"] = i
            if "台词" in h or "文案" in h or "对白" in h: idx["voice"] = i
            if "音效" in h or "bgm" in h or "音乐" in h: idx["audio"] = i
        scenes = []
        for row in tab_rows[1:]:
            if len(row) < 2 or not any(row): continue
            def cell(i): return row[i] if 0 <= i < len(row) else ""
            t = cell(idx["t"]) if idx["t"] >= 0 else ""
            pic = cell(idx["pic"]) if idx["pic"] >= 0 else ""
            voice = cell(idx["voice"]) if idx["voice"] >= 0 else ""
            audio = cell(idx["audio"]) if idx["audio"] >= 0 else ""
            sec = 5
            m = re.search(r"([0-9]+) *[-~] *([0-9]+) *s", t)
            if m: sec = int(m.group(2)) - int(m.group(1))
            else:
                m2 = re.search(r"([0-9]+) *s", t)
                if m2: sec = int(m2.group(1))
            sec = max(3, min(15, sec))
            subtitle = ""
            voice_part = ""
            if voice:
                if "字幕" in voice:
                    subtitle = re.sub(r"^[^：:]*[：:] *", "", voice).strip()
                elif voice.startswith(("画外音", "旁白", "对白", "配音")):
                    voice_part = voice
                else:
                    voice_part = "画外音：" + voice
            parts = []
            if pic: parts.append(pic)
            if voice_part: parts.append(voice_part)
            if audio: parts.append("音效：" + audio)
            prompt = "。".join(parts)
            scenes.append({"seconds": sec, "length": seconds_to_length(sec), "prompt": prompt, "subtitle": subtitle})
        if scenes:
            return scenes
    # 2) 纯文字：按空行拆，或整段
    blocks = [b.strip() for b in text.replace(chr(13), "").split(chr(10) + chr(10)) if b.strip()]
    if not blocks:
        blocks = [text]
    scenes = []
    for b in blocks:
        sec = max(5, min(15, 5 + (len(b) // 40) * 5))
        scenes.append({"seconds": sec, "length": seconds_to_length(sec), "prompt": b, "subtitle": ""})
    return scenes


def new_scene_defaults():
    return {"shot": "中景", "camera": "固定镜头", "aspect": "16:9", "resolution": "480p", "mode": "标准", "steps": 25, "seed": None, "negative_prompt": "", "model": "MiniMax H3"}


# ---- 状态持久化 ----
def load_state():
    try:
        st = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        st = {}
    st.setdefault("scenes", [])
    st.setdefault("character", {})
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
        if p.endswith(STYLE_SUFFIX):
            sc["prompt"] = p[: -len(STYLE_SUFFIX)].rstrip("。")
        # 补齐导演参数与几何信息（兼容旧数据）
        for k, v in new_scene_defaults().items():
            sc.setdefault(k, v)
        sc["width"], sc["height"] = resolve_geometry(sc)
    st["scenes"] = scenes
    return st


def save_state(st):
    json.dump(st, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


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


def build_bridge_graph(first_frame, last_frame, prompt, seed, prefix):
    w, h = BRIDGE_CANVAS
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": MODELS["clip"], "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["vae_video"]}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["vae_audio"]}},
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
        "12": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["10", 0], "vae": ["4", 0]}},
        "13": {"class_type": "CreateVideo", "inputs": {"images": ["11", 0], "fps": 24.0, "audio": ["12", 0]}},
        "14": {"class_type": "SaveVideo", "inputs": {"video": ["13", 0], "filename_prefix": prefix, "format": "auto"}},
    }


def _submit_and_wait(graph, timeout=1500):
    base = COMFY_URL.rstrip("/")
    r = _post(base + "/prompt", {"prompt": graph, "client_id": "bridge"})
    pid = r["prompt_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(12)
        try:
            h = _get(base + "/history/" + pid)
        except Exception:
            continue
        e = h.get(pid)
        if not e:
            continue
        st = e.get("status", {})
        if st.get("status_str") == "error":
            msgs = [m for m in st.get("messages", [])
                    if isinstance(m, list) and m and m[0] == "execution_error"]
            err = msgs[-1][1].get("exception_message") if msgs else "unknown error"
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
    return {"ok": False, "error": "timeout"}


def smart_transcode(seq, subtitles):
    """镜头 + 过渡片统一转码拼接（归一到 848x480@24fps、44.1kHz 音频）。"""
    ff = _ffmpeg()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    final = os.path.join(OUTPUT_DIR, "script2video_final_" + time.strftime("%Y%m%d_%H%M%S") + ".mp4")
    cmd = [ff, "-y"]
    for s in seq:
        cmd += ["-i", s]
    n = len(seq)
    vparts = ["[%d:v]scale=848:480,fps=24,setsar=1[v%d]" % (i, i) for i in range(n)]
    aparts = ["[%d:a]aresample=44100[a%d]" % (i, i) for i in range(n)]
    fc = ";".join(vparts + aparts)
    fc += ";" + "".join("[v%d][a%d]" % (i, i) for i in range(n)) + "concat=n=%d:v=1:a=1[vv][aa]" % n
    BS = chr(92)
    vf = []
    for s in subtitles or []:
        text = (s.get("text") or "").replace(":", BS + ":").replace("'", BS + "'")
        pos = s.get("position", "bottom")
        y = "(h-text_h)/2" if pos == "center" else "h-150"
        vf.append(
            "drawtext=fontfile='" + FONT.replace(":", BS + ":") + "':text='" + text + "':"
            "fontcolor=white:fontsize=" + str(s.get("fontsize", 40)) + ":borderw=2:bordercolor=black:"
            "x=(w-text_w)/2:y=" + y + ":enable='between(t," + str(s.get("start", 0)) + "," + str(s.get("end", 9999)) + ")'"
        )
    out_v = "vv"
    if vf:
        fc += ";[vv]" + ",".join(vf) + "[vvsub]"
        out_v = "vvsub"
    cmd += ["-filter_complex", fc, "-map", "[" + out_v + "]", "-map", "[aa]",
            "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", final]
    r = _ffmpeg_run(cmd)
    if r.returncode != 0:
        raise RuntimeError("合成失败: " + r.stderr[-800:])
    return os.path.basename(final)


def start_smart_concat(clips, subtitles):
    """启动后台补帧合成。优先使用故事板段落；否则用镜头输出（clips 为空时取 state）。"""
    st = load_state()
    segs = [s for s in (st.get("segments") or [])
            if os.path.isfile(os.path.join(OUTPUT_DIR, s))]
    if len(segs) >= 2:
        abs_clips = [os.path.join(OUTPUT_DIR, s) for s in segs]
        subtitles = _seg_subtitles(st)
    else:
        abs_clips = []
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
        return {"ok": False, "error": "至少需要 2 个已生成的镜头才能补帧合成"}
    status = SMART_CONCAT_STATUS
    if status.get("running"):
        return {"ok": True, "started": False, "busy": True}
    status.update(running=True, stage="bridging", done=0, total=len(abs_clips) - 1,
                  current="", result=None, error=None, log=[])
    threading.Thread(target=smart_concat_job, args=(abs_clips, subtitles, st), daemon=True).start()
    return {"ok": True, "started": True}


def smart_concat_job(clips, subtitles, st):
    status = SMART_CONCAT_STATUS
    try:
        stamp = time.strftime("%H%M%S")
        frames_dir = os.path.join(INPUT_DIR, "frames", "bridge_" + stamp)
        os.makedirs(frames_dir, exist_ok=True)
        scenes = st["scenes"]
        bridges = list(st.get("bridges") or [])
        need = len(clips) - 1
        if not (len(bridges) == need and all(os.path.isfile(os.path.join(OUTPUT_DIR, b)) for b in bridges)):
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
                          "动作衔接自然，光线与色调渐变过渡，无跳变、无切镜、无黑场。"
                          "电影质感，真实实拍风格，无字幕无水印。" %
                          (prev_prompt[:80], next_prompt[:80]))
                rel_prev = os.path.relpath(prev_frame, INPUT_DIR).replace(os.sep, "/")
                rel_next = os.path.relpath(next_frame, INPUT_DIR).replace(os.sep, "/")
                r = _submit_and_wait(build_bridge_graph(
                    rel_prev, rel_next, prompt, 7100 + i,
                    "video/bridge_%d_%s" % (i, stamp)))
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
        final = smart_transcode(seq, subtitles)
        status["result"] = final
        status["stage"] = "done"
        status["log"].append("成片: " + final)
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


def compose_storyboard_prompt(group, character=None):
    """MiniMax 官方故事板格式：一次采样内用 [Shot N] At MM:SS.mmm 标注镜头切换。"""
    lines = []
    acc = 0.0
    for idx, sc in enumerate(group):
        if character:
            sc = dict(sc)
            sc["character"] = character
        body = compose_prompt(sc).rstrip("。")
        if idx == 0:
            lines.append("[Shot 1] %s。" % body)
        else:
            lines.append("[Shot %d] At %s, %s。" % (idx + 1, _fmt_ts(acc), body))
        acc += float(sc.get("seconds") or 5)
    return ("电影质感，真实实拍风格，无字幕无水印。\n\n"
            "integrated_multimodal_description: " + " ".join(lines) + "\n\n"
            "overall_soundscape: 夜晚战场氛围：火焰噼啪声、风声、远处巨龙的吼声。\n\n"
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
            scene = {"model": "MiniMax H3", "prompt": compose_storyboard_prompt(grp, st.get("character")),
                     "width": 848, "height": 480, "length": seconds_to_length(total),
                     "seed": seed_base + gi, "mode": "标准", "steps": 25,
                     "prefix": "video/seg_%d_%s" % (gi, stamp)}
            r = generate_scene(scene)
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
                             "end": acc + float(sc.get("seconds") or 5),
                             "fontsize": 44, "position": "center"})
            acc += float(sc.get("seconds") or 5)
        if gi < len(groups) - 1:
            acc += BRIDGE_LENGTH / 24
    return subs


def concat_segments_sync(st):
    """段落模式同步合成：生成段落间过渡并接成片（供命令行 runner 复用）。"""
    segs = st.get("segments") or []
    clips = []
    for s in segs:
        p = os.path.join(OUTPUT_DIR, s)
        if not os.path.isfile(p):
            raise RuntimeError("段落文件缺失: " + s)
        clips.append(p)
    if len(clips) < 2:
        raise RuntimeError("段落不足，无法合成")
    smart_concat_job(clips, _seg_subtitles(st), st)
    status = SMART_CONCAT_STATUS
    if status.get("stage") != "done":
        raise RuntimeError(status.get("error") or "合成失败")
    return status["result"]


# ---- HTTP ----
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, p, ct):
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(os.path.getsize(p)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(p, "rb") as f:
            self.wfile.write(f.read())

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html")
            if os.path.exists(p):
                self._send_file(p, "text/html; charset=utf-8")
                return
        elif self.path.startswith("/static/"):
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), self.path.lstrip("/").replace("/", os.sep))
            if os.path.exists(p) and os.path.isfile(p):
                ct = "application/javascript" if p.endswith(".js") else "text/css" if p.endswith(".css") else "application/octet-stream"
                self._send_file(p, ct)
                return
        elif self.path.startswith("/output/"):
            rel = unquote(self.path[len("/output/"):]).replace("/", os.sep)
            p = os.path.join(OUTPUT_DIR, rel)
            if os.path.exists(p) and os.path.isfile(p):
                self._send_file(p, "video/mp4" if p.endswith(".mp4") else "application/octet-stream")
                return
        elif self.path == "/api/status":
            try:
                q = _get(COMFY_URL.rstrip("/") + "/queue")
                self._send(200, {"ok": True, "running": len(q.get("queue_running", [])), "pending": len(q.get("queue_pending", []))})
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e)})
            return
        elif self.path == "/api/concat_status":
            self._send(200, {"ok": True, "status": SMART_CONCAT_STATUS})
            return
        elif self.path == "/api/segment_status":
            self._send(200, {"ok": True, "status": SEGMENT_STATUS})
            return
        elif self.path == "/api/state":
            st = load_state()
            self._send(200, {"ok": True, "scenes": st["scenes"], "character": st.get("character") or {}})
            return
        elif self.path == "/api/assets":
            self._send(200, {"ok": True, "assets": list_assets()})
            return
        elif self.path == "/api/assets/tags":
            self._send(200, {"ok": True, "tags": _load_json(TAGS_FILE, {})})
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
                self._send_file(p, ct)
                return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            if self.path == "/api/optimize":
                body = self._body()
                text = body.get("text") or ""
                if body.get("docx_b64"):
                    text = extract_document(body["docx_b64"], body.get("filename") or "")
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
                    s.update(new_scene_defaults())
                    w, h = resolve_geometry(s)
                    s["width"], s["height"] = w, h
                    s["output"] = None
                st["scenes"] = (existing + scenes) if body.get("append") else scenes
                save_state(st)
                self._send(200, {"ok": True, "scenes": scenes, "append": bool(body.get("append"))})
            elif self.path == "/api/generate":
                raw = self._body().get("scene", {})
                matched = None
                if raw.get("auto_match") and not raw.get("first_frame"):
                    m = auto_match_image(raw.get("prompt") or "")
                    if m:
                        try:
                            src = resolve_asset_path(m["path"])
                            ext = os.path.splitext(src)[1].lower() or ".png"
                            frames_dir = os.path.join(INPUT_DIR, "frames")
                            os.makedirs(frames_dir, exist_ok=True)
                            dst_name = "auto_" + time.strftime("%H%M%S") + "_" + str(random.randrange(100, 999)) + ext
                            shutil.copyfile(src, os.path.join(frames_dir, dst_name))
                            raw["first_frame"] = "frames/" + dst_name
                            matched = {"name": m["name"], "path": m["path"], "score": m["score"]}
                        except Exception as e:
                            matched = {"error": str(e)}
                ch = (load_state().get("character") or {})
                if ch.get("description") and not raw.get("character"):
                    raw["character"] = ch
                scene = prepare_scene(raw)
                scene.setdefault("prefix", "video/" + scene.get("id", "scene") + "_" + time.strftime("%H%M%S"))
                r = generate_scene(scene)
                if r.get("ok"):
                    st = load_state()
                    for sc in st["scenes"]:
                        if sc.get("id") == scene.get("id"):
                            sc["output"] = r["file"]
                            for k in ("prompt", "seconds", "subtitle", "shot", "camera", "aspect",
                                      "resolution", "mode", "steps", "seed", "negative_prompt", "model"):
                                if k in raw:
                                    sc[k] = raw[k]
                            if raw.get("voice") is not None:
                                sc["voice"] = raw["voice"]
                            if raw.get("guides") is not None:
                                sc["guides"] = raw["guides"]
                            if raw.get("first_frame") is not None:
                                sc["first_frame"] = raw["first_frame"]
                            sc["use_character_frame"] = bool(raw.get("use_character_frame"))
                            break
                    save_state(st)
                if matched is not None:
                    r = dict(r)
                    r["matched"] = matched
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
                if scene_id == "character":
                    st["character"]["image"] = rel
                else:
                    for sc in st["scenes"]:
                        if sc.get("id") == scene_id:
                            sc["first_frame"] = rel
                            break
                save_state(st)
                self._send(200, {"ok": True, "file": rel})
            elif self.path == "/api/save_scenes":
                body = self._body()
                scenes = body.get("scenes")
                if not isinstance(scenes, list):
                    self._send(200, {"ok": False, "error": "scenes 必须是列表"})
                    return
                st = load_state()
                st["scenes"] = scenes
                save_state(st)
                self._send(200, {"ok": True})
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
            elif self.path == "/api/character":
                body = self._body()
                st = load_state()
                ch = st["character"]
                ch["name"] = str(body.get("name") or "").strip()
                ch["description"] = str(body.get("description") or "").strip()
                if body.get("image") is not None:
                    ch["image"] = body.get("image") or None
                save_state(st)
                self._send(200, {"ok": True, "character": ch})
            elif self.path == "/api/concat":
                body = self._body()
                clips = body.get("clips", [])
                subs = body.get("subtitles", [])
                if body.get("bridge", True):
                    # 默认走智能合成：镜头之间用 H3 首尾关键帧补过渡帧
                    r = start_smart_concat(clips, subs)
                else:
                    abs_clips = [os.path.join(OUTPUT_DIR, c.lstrip("/")) if not os.path.isabs(c) else c for c in clips]
                    r = concat_scenes(abs_clips, subs)
                self._send(200, r)
            elif self.path == "/api/generate_segments":
                if SEGMENT_STATUS.get("running"):
                    self._send(200, {"ok": True, "started": False, "busy": True})
                else:
                    st = load_state()
                    threading.Thread(target=generate_segments_job, args=(st,), daemon=True).start()
                    self._send(200, {"ok": True, "started": True})
            elif self.path == "/api/assets/delete":
                rel = (self._body().get("path") or "").replace("\\", "/").lstrip("/")
                try:
                    p = resolve_asset_path(rel)
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
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send(500, {"ok": False, "error": str(e)})

    def log_message(self, *a):
        pass


def main():
    port = int(os.environ.get("PORT", "9081"))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("script-to-video 导演台 启动: http://127.0.0.1:%d" % port)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
