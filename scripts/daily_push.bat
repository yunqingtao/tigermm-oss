@echo off
REM ===================================================================
REM  Tiger.M.M - daily auto-push (called by the Windows scheduled task)
REM  Output is appended to logs\daily_push.log so you can see afterwards
REM  which day it really pushed and which day it failed.
REM  Interpreter comes from _find_python.bat (no hardcoded paths).
REM ===================================================================
setlocal
call "%~dp0..\_find_python.bat"
if errorlevel 1 (
    echo [daily_push] no Python found >> "%~dp0..\logs\daily_push.log"
    exit /b 1
)
set PROJ=%~dp0..
cd /d "%PROJ%"
if not exist logs mkdir logs
echo. >> "logs\daily_push.log"
echo ===== %DATE% %TIME% ===== >> "logs\daily_push.log"
"%PY%" -B "scripts\daily_push.py" >> "logs\daily_push.log" 2>&1
set RC=%ERRORLEVEL%
echo [exit=%RC%] >> "logs\daily_push.log"
exit /b %RC%
