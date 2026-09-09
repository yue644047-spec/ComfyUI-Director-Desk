@echo off
chcp 65001 >nul
setlocal
cd /d D:\app\comfyui
set PY=D:\ProgramData\anaconda3\envs\clora\python.exe
if not exist "%PY%" (
  echo [ERROR] clora env missing. Run migration\install_env.bat first.
  pause & exit /b 1
)
echo Starting Director at http://127.0.0.1:9081  (log: director.log)
"%PY%" web\app.py > director.log 2>&1
