@echo off
setlocal
cd /d "%~dp0"
if exist "dist\UltrasoundMFLReplay\UltrasoundMFLReplay.exe" (
  start "" "dist\UltrasoundMFLReplay\UltrasoundMFLReplay.exe"
) else (
  python desktop_app.py
)
endlocal
