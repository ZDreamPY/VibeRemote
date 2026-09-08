@echo off
chcp 65001 >nul
title VibeRemote 停止
echo 正在停止 VibeRemote 服务...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
echo.
echo 如需重新启动，双击"启动VibeRemote.bat"
pause >nul