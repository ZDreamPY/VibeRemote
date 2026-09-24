@echo off
title Stop VibeRemote
echo Stopping VibeRemote service...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
echo.
echo Done. Restart by double-clicking the start bat.
pause >nul
