@echo off
chcp 65001 >nul
setlocal
cd /d D:\app\comfyui
REM ============================================================
REM  incremental packaging: code + frontend + workflows + docs
REM  (models / output / python env are NOT included)
REM  Run: make_incremental.bat          -> without state.json
REM       make_incremental.bat withstate -> include state.json
REM ============================================================
set STAGE=migration\_stage
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set TS=%%i

if exist "%STAGE%" rmdir /s /q "%STAGE%"
mkdir "%STAGE%"

for %%d in (comfy comfy_extras comfy_api comfy_api_nodes comfy_execution comfy_config utils app blueprints middleware api_server html web custom_nodes alembic) do (
  if exist "%%d" robocopy "%%d" "%STAGE%\%%d" /E /NFL /NDL /NJH /NJS /NP >nul
)
copy *.py "%STAGE%" >nul
for %%f in (requirements.txt manager_requirements.txt pyproject.toml pytest.ini extra_model_paths.yaml.example) do (
  if exist "%%f" copy "%%f" "%STAGE%" >nul
)
copy *.md "%STAGE%" >nul
copy *.bat "%STAGE%" >nul

if exist "user\default\workflows" robocopy "user\default\workflows" "%STAGE%\workflows" /E /NFL /NDL /NJH /NJS /NP >nul
for %%f in (migration\*.bat migration\*.md) do copy "%%f" "%STAGE%" >nul

if /I "%~1"=="withstate" (
  if exist "web\state.json" (
    mkdir "%STAGE%\web" 2>nul
    copy "web\state.json" "%STAGE%\web\state.json" >nul
  )
)

for /d /r "%STAGE%" %%p in (__pycache__) do rmdir /s /q "%%p" 2>nul

tar -a -c -f "migration\incremental_%TS%.zip" -C "%STAGE%" .
if errorlevel 1 (
  echo [ERROR] packaging failed
  pause & exit /b 1
)
rmdir /s /q "%STAGE%"
echo.
echo OK: migration\incremental_%TS%.zip
echo Copy this zip to the intranet machine and extract it over D:\app\comfyui
pause
