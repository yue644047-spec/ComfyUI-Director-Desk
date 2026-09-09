@echo off
chcp 65001 >nul
cd /d %~dp0..
set PY=D:\ProgramData\anaconda3\envs\clora\python.exe
echo ==========================================
echo   script-to-video Web 端
echo   ComfyUI 请确保已在 http://127.0.0.1:8188 运行
echo   浏览器打开 http://127.0.0.1:9081
echo ==========================================
"%PY%" web\app.py
pause
