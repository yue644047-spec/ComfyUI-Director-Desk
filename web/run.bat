@echo off
chcp 65001 >nul
cd /d %~dp0..
set PY=%~dp0..\python_embeded\python.exe
echo ==========================================
echo   script-to-video Web 端（导播台）
echo   请确保 ComfyUI 已运行: http://127.0.0.1:8188
echo   浏览器打开 http://127.0.0.1:9081
echo ==========================================
"%PY%" -s web\app.py
pause
