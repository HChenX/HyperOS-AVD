@echo off
setlocal
set "PYTHONUTF8=1"
chcp 65001 >nul
cd /d "%~dp0"
if not defined HYPEROS_AVD_WORKSPACE set "HYPEROS_AVD_WORKSPACE=%~dp0work\os4-pad"
if not exist "%HYPEROS_AVD_WORKSPACE%\local\runtime.json" (
    echo Install the official OS4 Pad Release first: .\Setup.cmd --bundle path\to\manifest.json 1>&2
    exit /b 1
)
python scripts\launch.py %*
endlocal
