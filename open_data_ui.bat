@echo off
rem Tiger.M.M data-dashboard launcher -- starts the engine (if needed) and opens
rem the READ-ONLY data page (/dash: artifacts / experts / audit).
rem Paths are relative to THIS file, so the folder can live anywhere.
setlocal
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0_find_python.bat"
if errorlevel 1 (
    pause
    exit /b 1
)
"%PY%" -B "%~dp0start_web_ui.py" --open --dash
