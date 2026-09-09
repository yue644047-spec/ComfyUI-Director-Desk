@echo off
chcp 65001 >nul
setlocal
cd /d D:\app\comfyui
set PY=D:\ProgramData\anaconda3\envs\clora\python.exe
if not exist "%PY%" (
  echo [ERROR] clora env missing. Run migration\install_env.bat first.
  pause & exit /b 1
)
echo Starting ComfyUI at http://127.0.0.1:8188  (log: comfyui.log)
"%PY%" main.py --disable-pinned-memory --use-sage-attention > comfyui.log 2>&1
