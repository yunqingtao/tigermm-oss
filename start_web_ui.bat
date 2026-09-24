@echo off
rem Tiger.M.M Web UI launcher -- ASCII only (cmd reads .bat as OEM codepage)
rem Uses _find_python.bat to locate a real Python (never a bare "python" that
rem might resolve to some unrelated venv on PATH).
setlocal
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0_find_python.bat"
if errorlevel 1 (
    pause
    exit /b 1
)
"%PY%" -B "%~dp0start_web_ui.py" --open
