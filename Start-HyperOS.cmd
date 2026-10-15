@echo off
setlocal
set "PYTHONUTF8=1"
chcp 65001 >nul
cd /d "%~dp0"
if not defined HYPEROS_AVD_WORKSPACE set "HYPEROS_AVD_WORKSPACE=%~dp0"
python scripts\launch.py %*
endlocal
