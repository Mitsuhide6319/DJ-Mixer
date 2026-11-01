@echo off
rem DJ Mixer setup (first time). Installs uv and ffmpeg with winget if missing,
rem installs Python 3.11 and the required packages (uv sync), and makes a desktop shortcut.
rem Details: tools\setup.ps1
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\setup.ps1"
echo.
pause
