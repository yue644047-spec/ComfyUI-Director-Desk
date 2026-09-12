@echo off
setlocal
REM ============================================================
REM  Pack Ollama (program + model store) for an offline machine.
REM  Usage: make_ollama_pack.bat <destination> [mini]
REM    make_ollama_pack.bat E:\ollama_pack         all models (~14GB)
REM    make_ollama_pack.bat E:\ollama_pack mini    qwen3:8b + bge-m3 (~6.5GB)
REM  Offline install steps: see the migration doc next to this script.
REM ============================================================
if "%~1"=="" (
  echo Usage: make_ollama_pack.bat ^<destination^> [mini]
  echo   example: make_ollama_pack.bat E:\ollama_pack mini
  echo   details: see the migration doc shipped next to this script.
  pause ^& exit /b 1
)
if /I "%~2"=="mini" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_ollama_pack.ps1" -Dest "%~1" -Mini
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_ollama_pack.ps1" -Dest "%~1"
)
pause
