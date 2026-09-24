@echo off
setlocal
cd /d "%~dp0"

rem Auto-install deps only when running from source (exe bundles them)
if not exist "dist\VibeRemote.exe" (
  python -c "import websockets, pynput, qrcode" >nul 2>&1
  if errorlevel 1 pip install websockets pynput qrcode >nul 2>&1
)

rem Start server in background (prefer packaged exe if built)
if exist "dist\VibeRemote.exe" (
  start "" "%~dp0dist\VibeRemote.exe"
) else (
  start "" pythonw "%~dp0server.py"
)

rem Wait for service to come up
timeout /t 2 /nobreak >nul

rem Open pairing page in default browser
start "" http://127.0.0.1:8081/qrcode.svg
exit /b 0
