@echo off
rem Tiger.M.M data-dashboard launcher -- starts the engine (if needed) and opens
rem the READ-ONLY data page (/dash: artifacts / experts / audit).
rem Paths are relative to THIS file, so the folder can live anywhere.
rem 2026-10-09: dropped the `chcp 65001` that used to sit here.  This file is
rem   pure ASCII so it never needed it, and a batch file that switches to
rem   codepage 65001 makes cmd.exe mis-track its read offset (see the long note
rem   in check_keys.bat, measured).  Not worth the risk for zero benefit.
setlocal
cd /d "%~dp0"
call "%~dp0_find_python.bat"
if errorlevel 1 (
    pause
    exit /b 1
)
"%PY%" -B "%~dp0start_web_ui.py" --open --dash
