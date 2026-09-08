@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

rem 依赖检查
python -c "import websockets, pynput, qrcode" >nul 2>&1
if errorlevel 1 pip install websockets pynput qrcode >nul 2>&1

rem 后台静默启动
start "" pythonw "%~dp0server.py"

rem 等 2 秒让服务起来
timeout /t 2 /nobreak >nul

rem 打开二维码
start "" http://127.0.0.1:8081/qrcode.svg
exit /b 0