@echo off
setlocal
cd /d "%~dp0"

rem Install PyInstaller if missing
python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 pip install pyinstaller >nul 2>&1

rem Build one-file no-console exe (index.html bundled; pin.txt persists next to exe)
python -m PyInstaller --noconfirm --onefile --windowed --name VibeRemote ^
  --collect-all qrcode ^
  --add-data "index.html;." ^
  server.py

echo.
echo Build done: dist\VibeRemote.exe
pause
