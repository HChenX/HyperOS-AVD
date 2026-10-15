@echo off
setlocal
set "PYTHONUTF8=1"
set "HYPEROS_AVD_WORKSPACE=D:\RuanJian\HyperOS-AVD\HyperOS-Phone"
set "ANDROID_HOME=D:\RuanJian\SDK"
set "ANDROID_SDK_ROOT=D:\RuanJian\SDK"
chcp 65001 >nul
cd /d D:\Android\HyperOS-AVD
python scripts\launch.py %*
endlocal
