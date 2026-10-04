@echo off
setlocal
cd /d "%~dp0"
if exist "分发软件版本\软件\UltrasoundMFLReplay.exe" (
  start "" "分发软件版本\软件\UltrasoundMFLReplay.exe"
) else (
  python desktop_app.py --config "%~dp0config.json"
)
endlocal
