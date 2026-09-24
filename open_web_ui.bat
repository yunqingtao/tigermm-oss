@echo off
rem Tiger.M.M web dashboard launcher -- starts engine (if needed) then opens browser
rem Paths relative to THIS file.
setlocal
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0_find_python.bat"
if errorlevel 1 (
    pause
    exit /b 1
)
"%PY%" -B "%~dp0start_web_ui.py" --open
