#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""拼接补帧：相邻镜头之间用 H3 首尾关键帧生成过渡片段，再无缝接成片。

过渡片 = MiniMaxH3ImageToVideo(first_frame=上一镜最后一帧, last_frame=下一镜第一帧)。
模型以上一镜结尾和下一镜开头为上下文锚点，在两者之间自动补出中间帧（约 1.6 秒），
因此每一处镜头切换都是模型生成的连续过渡，而不是硬切。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

BRIDGE_LENGTH = 39        # 17k+5 网格，约 1.6 秒 @24fps
BRIDGE_STEPS = 25
WIDTH, HEIGHT = 864, 480   # 必须是 32 的倍数，否则首尾关键帧路径 patchify 会崩


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def extract_first(src_abs, out_abs):
    r = sh([app._ffmpeg(), "-y", "-i", src_abs, "-frames:v", "1", "-q:v", "2", out_abs])
    if r.returncode != 0 or not os.path.isfile(out_abs):
        raise RuntimeError("抽首帧失败: " + r.stderr[-300:])


def extract_last(src_abs, out_abs):
    r = sh([app._ffmpeg(), "-y", "-sseof", "-0.05", "-i", src_abs, "-frames:v", "1", "-q:v", "2", out_abs])
    if r.returncode != 0 or not os.path.isfile(out_abs):
        raise RuntimeError("抽尾帧失败: " + r.stderr[-300:])


def build_bridge_graph(first_frame, last_frame, prompt, seed, prefix):
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": app.MODELS["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": app.MODELS["clip"], "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": app.MODELS["vae_video"]}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": app.MODELS["vae_audio"]}},
        "5": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {
            "clip": ["2", 0], "vae": ["3", 0], "prompt": prompt,
            "width": WIDTH, "height": HEIGHT, "length": BRIDGE_LENGTH,
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


def submit_and_wait(graph, timeout=1500):
    base = app.COMFY_URL.rstrip("/")
    req = urllib.request.Request(
        base + "/prompt",
        data=json.dumps({"prompt": graph, "client_id": "bridge"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    r = json.load(urllib.request.urlopen(req, timeout=120))
    pid = r["prompt_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(12)
        try:
            h = json.load(urllib.request.urlopen(base + "/history/" + pid, timeout=30))
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
            rel_dir = os.path.basename(os.path.normpath(app.OUTPUT_DIR))
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


def main():
    st = app.load_state()
    scenes = st["scenes"]
    clips = []
    for sc in scenes:
        out = sc.get("output")
        if not out:
            print("FAIL 缺少镜头输出:", sc.get("id"), flush=True)
            return 1
        abs_p = os.path.join(app.OUTPUT_DIR, out)
        if not os.path.isfile(abs_p):
            print("FAIL 镜头文件不存在:", abs_p, flush=True)
            return 1
        clips.append(abs_p)

    stamp = time.strftime("%H%M%S")
    frames_dir = os.path.join(app.INPUT_DIR, "frames", "bridge_" + stamp)
    os.makedirs(frames_dir, exist_ok=True)

    bridges = []
    for i in range(len(clips) - 1):
        prev, nxt = clips[i], clips[i + 1]
        prev_frame = os.path.join(frames_dir, "b%d_prev.png" % i)
        next_frame = os.path.join(frames_dir, "b%d_next.png" % i)
        print("BRIDGE %d/%d 抽取关键帧 ..." % (i + 1, len(clips) - 1), flush=True)
        extract_last(prev, prev_frame)
        extract_first(nxt, next_frame)

        prev_prompt = (scenes[i].get("prompt") or "").strip()
        next_prompt = (scenes[i + 1].get("prompt") or "").strip()
        prompt = ("无缝转场补帧：画面从「%s」平滑过渡到「%s」，"
                  "运镜与动作自然连续，光线渐变，无跳变无切镜。"
                  "电影质感，真实实拍风格，无字幕无水印。" %
                  (prev_prompt[:80], next_prompt[:80]))
        prefix = "video/bridge_%d_%s" % (i, stamp)
        rel_prev = os.path.relpath(prev_frame, app.INPUT_DIR).replace(os.sep, "/")
        rel_next = os.path.relpath(next_frame, app.INPUT_DIR).replace(os.sep, "/")
        graph = build_bridge_graph(rel_prev, rel_next, prompt, 7100 + i, prefix)
        print("BRIDGE %d/%d 提交 H3 过渡生成 (seed=%d) ..." % (i + 1, len(clips) - 1, 7100 + i), flush=True)
        r = submit_and_wait(graph)
        if not r.get("ok"):
            print("FAIL BRIDGE %d: %s" % (i, r.get("error")), flush=True)
            return 2
        bridges.append(r["file"])
        print("ok bridge %d -> %s" % (i, r["file"]), flush=True)

    seq = []
    for i in range(len(clips)):
        seq.append(clips[i])
        if i < len(bridges):
            seq.append(os.path.join(app.OUTPUT_DIR, bridges[i]))
    print("CONCAT 合成 %d 个片段 (6 镜头 + %d 过渡) ..." % (len(seq), len(bridges)), flush=True)

    # 统一转码拼接：所有片段归一到 848x480@24fps、44.1kHz，避免 -c copy 的码流不齐
    ff = app._ffmpeg()
    final = os.path.join(app.OUTPUT_DIR,
                         "script2video_final_" + time.strftime("%Y%m%d_%H%M%S") + ".mp4")
    cmd = [ff, "-y"]
    for s in seq:
        cmd += ["-i", s]
    n = len(seq)
    vparts = ["[%d:v]scale=848:480,fps=24,setsar=1[v%d]" % (i, i) for i in range(n)]
    aparts = ["[%d:a]aresample=44100[a%d]" % (i, i) for i in range(n)]
    fc = ";".join(vparts + aparts)
    fc += ";" + "".join("[v%d][a%d]" % (i, i) for i in range(n)) + "concat=n=%d:v=1:a=1[vv][aa]" % n
    cmd += ["-filter_complex", fc, "-map", "[vv]", "-map", "[aa]",
            "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", final]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("FAIL 合成:", r.stderr[-800:], flush=True)
        return 3
    print("RESULT:", {"ok": True, "file": os.path.basename(final)}, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
