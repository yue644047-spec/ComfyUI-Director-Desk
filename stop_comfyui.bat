@echo off
powershell -NoProfile -Command "foreach ($p in 8188,9081) { Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force } }; echo ComfyUI + 导播台 stopped"
pause
