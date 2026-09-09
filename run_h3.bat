@echo off
cd /d D:\app\comfyui
D:\ProgramData\anaconda3\envs\clora\python.exe main.py --disable-pinned-memory --use-sage-attention > comfyui.log 2>&1
pause
