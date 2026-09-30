@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0dist\UltrasoundMFLReplay\UltrasoundMFLReplay.exe" (
  start "" "%~dp0dist\UltrasoundMFLReplay\UltrasoundMFLReplay.exe"
) else (
  python "%~dp0desktop_app.py"
)
if errorlevel 1 (
  echo.
  echo 回放服务启动失败，请确认 Python 和 NumPy 已安装。
  pause
)
endlocal
