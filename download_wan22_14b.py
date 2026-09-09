import os, sys, time, urllib.request, shutil

BASE = os.path.dirname(os.path.abspath(__file__))
import ctypes
LOCK = os.path.join(BASE, "download_wan22_14b.lock")

def _pid_alive(pid):
    try:
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return False

def _acquire_lock():
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            old = int(open(LOCK, encoding="utf-8").read().strip() or "0")
        except Exception:
            old = 0
        if old and not _pid_alive(old):
            try:
                os.remove(LOCK)
            except Exception:
                pass
            return _acquire_lock()
        print("another downloader is already running, exit", flush=True)
        return False

if not _acquire_lock():
    sys.exit(2)

FILES = [
    ("https://modelscope.cn/models/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/master/split_files/vae/wan_2.1_vae.safetensors",
     os.path.join(BASE, "models", "vae", "wan_2.1_vae.safetensors"), 253815318),
    ("https://modelscope.cn/models/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/master/split_files/diffusion_models/wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors",
     os.path.join(BASE, "models", "diffusion_models", "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors"), 14294742832),
    ("https://modelscope.cn/models/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/master/split_files/diffusion_models/wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors",
     os.path.join(BASE, "models", "diffusion_models", "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors"), 14294742832),
    ("https://modelscope.cn/models/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/master/split_files/diffusion_models/wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors",
     os.path.join(BASE, "models", "diffusion_models", "wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors"), 14293923632),
    ("https://modelscope.cn/models/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/master/split_files/diffusion_models/wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors",
     os.path.join(BASE, "models", "diffusion_models", "wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors"), 14293923632),
]

def download(url, dest, expected, max_attempts=2000):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    name = os.path.basename(dest)
    if os.path.exists(dest) and os.path.getsize(dest) >= expected:
        print("SKIP (complete) %s" % dest, flush=True)
        return
    part = dest + ".part"
    done = os.path.getsize(part) if os.path.exists(part) else 0
    alt = url.replace("https://modelscope.cn/", "https://huggingface.co/").replace("/resolve/master/", "/resolve/main/")
    attempt = 0
    last_print = 0.0
    while True:
        u = alt if attempt % 2 else url
        try:
            headers = {"Range": "bytes=%d-" % done} if done else {}
            req = urllib.request.Request(u, headers=headers)
            with urllib.request.urlopen(req, timeout=45) as r:
                cl = r.headers.get("Content-Length")
                total = None
                if r.status == 206 and cl:
                    total = done + int(cl)
                elif r.status == 200:
                    total = int(cl) if cl else None
                    if done:
                        done = 0
                mode = "wb" if (r.status == 200 and done == 0) else "ab"
                with open(part, mode) as f:
                    while True:
                        chunk = r.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        now = time.time()
                        if total and now - last_print >= 10:
                            last_print = now
                            print("%s %.1f%% (%d / %d)" % (name, done * 100.0 / total, done, total), flush=True)
            if total is None or done >= total:
                os.replace(part, dest)
                print("DONE %s %d bytes" % (dest, done), flush=True)
                return
            print("short read %s, resume at %d" % (name, done), flush=True)
        except Exception as e:
            attempt += 1
            if attempt > max_attempts:
                print("FAILED %s: %s" % (dest, repr(e)[:200]), flush=True)
                sys.exit(1)
            print("retry #%d %s: %s" % (attempt, name, repr(e)[:120]), flush=True)
            time.sleep(min(10, 1 + attempt))

for u, d, sz in FILES:
    download(u, d, sz)

leftover = os.path.join(BASE, "models", "split_files")
if os.path.isdir(leftover):
    shutil.rmtree(leftover, ignore_errors=True)
print("ALL DONE", flush=True)
try:
    os.remove(LOCK)
except Exception:
    pass
