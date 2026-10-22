@echo off
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
if not exist "%~dp0data\tmp" mkdir "%~dp0data\tmp"
set "TEMP=%~dp0data\tmp"
set "TMP=%TEMP%"
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" bot.py %*
) else (
  py -3 bot.py %*
)
pause
