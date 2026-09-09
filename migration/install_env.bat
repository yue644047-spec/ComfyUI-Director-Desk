@echo off
chcp 65001 >nul
setlocal
REM ============================================
REM  clora Python env installer (intranet target)
REM  Run on the NEW machine after copying the
REM  migration folder + D:\app\comfyui.
REM ============================================
set TARFILE=%~dp0clora_env.tar.gz
set ENVDIR=D:\ProgramData\anaconda3\envs\clora

if not exist "%TARFILE%" (
  echo [ERROR] %TARFILE% not found.
  echo Copy the whole migration folder to this machine first.
  pause & exit /b 1
)

if not exist "%ENVDIR%\python.exe" (
  echo Unpacking to %ENVDIR% ...
  if not exist "D:\ProgramData\anaconda3\envs" mkdir "D:\ProgramData\anaconda3\envs"
  tar -xzf "%TARFILE%" -C "D:\ProgramData\anaconda3\envs"
  if errorlevel 1 (
    echo [ERROR] unpack failed. Check free disk space.
    pause & exit /b 1
  )
) else (
  echo %ENVDIR% already exists, skip unpack.
)

echo Fixing internal paths (conda-unpack) ...
set "PATH=%ENVDIR%;%ENVDIR%\Scripts;%ENVDIR%\Library\bin;%PATH%"
"%ENVDIR%\Scripts\conda-unpack.exe"
if errorlevel 1 (
  echo [WARN] conda-unpack error. If ENVDIR is the same path as the source machine, ignore this.
)

echo Verifying torch ...
"%ENVDIR%\python.exe" -c "import torch; print('torch', torch.__version__, 'cuda', torch.version.cuda)"
if errorlevel 1 (
  echo [ERROR] python check failed.
  pause & exit /b 1
)

echo.
echo [OK] clora env ready: %ENVDIR%
echo Next: run start_comfyui.bat then start_director.bat
pause
