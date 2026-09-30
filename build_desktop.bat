@echo off
setlocal
cd /d "%~dp0"
python -m PyInstaller --noconfirm --clean --onedir --windowed --name UltrasoundMFLReplay --add-data config.json;. --add-data assets;assets desktop_app.py
if errorlevel 1 (
  echo Build failed. Make sure PyInstaller is installed in the current Python environment.
  exit /b 1
)
copy /Y config.json dist\UltrasoundMFLReplay\config.json >NUL
if errorlevel 1 (
  echo Config copy failed.
  exit /b 1
)
echo Build complete: dist\UltrasoundMFLReplay\UltrasoundMFLReplay.exe
endlocal
