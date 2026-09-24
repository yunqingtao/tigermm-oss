@echo off
rem Tiger.M.M desktop launcher -- ASCII only (cmd reads .bat as OEM codepage)
rem Paths are relative to THIS file, so the folder can live anywhere.
setlocal
cd /d "%~dp0"
call "%~dp0_find_python.bat"
if errorlevel 1 (
    pause
    exit /b 1
)
set "APP=%~dp0tmm_app.py"
if not exist "%APP%" (
    echo [ERROR] tmm_app.py not found next to this launcher
    pause
    exit /b 1
)
if not defined PYW set "PYW=%PY%"
rem Launch without a console window when pythonw is available. Logs: tmm_app.log
start "" "%PYW%" -B "%APP%"
exit /b 0
