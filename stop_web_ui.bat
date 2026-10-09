@echo off
REM Graceful stop for TMM engine (avoids killing SQLite WAL hard).
REM English only on purpose: CJK in .bat breaks under some consoles.
setlocal
call "%~dp0_find_python.bat"
if errorlevel 1 (
    pause
    exit /b 1
)
pushd "%~dp0"
"%PY%" -B stop_web_ui.py %*
set RC=%ERRORLEVEL%
popd
if not "%RC%"=="0" pause
exit /b %RC%
