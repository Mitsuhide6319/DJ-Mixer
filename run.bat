@echo off
rem DJ Mixer launcher.
rem   run.bat            : コンソールの窓を出さずに起動する（DJ Mixer.vbs と同じ）
rem   run.bat --browser  : 既定のブラウザで開く（開発用。コンソールに記録が出る）
cd /d "%~dp0"
if "%~1"=="" (
  start "" wscript.exe "%~dp0DJ Mixer.vbs"
  exit /b
)
uv run python -m djmix %*
