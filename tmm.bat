@echo off
rem Tiger.M.M CLI launcher
rem Paths relative to THIS file, so the folder can live anywhere.
rem NOTE: the old ssh-tunnel cleanup was removed on purpose -- it killed every
rem       ssh.exe on the machine, which is not acceptable outside the author's box.
setlocal
cd /d "%~dp0"
call "%~dp0_find_python.bat"
if errorlevel 1 (
    pause
    exit /b 1
)
rem clear pycache
for /d /r . %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d" 2>nul
set PYTHONDONTWRITEBYTECODE=1
"%PY%" -B "%~dp0main.py" --cli %*
exit /b %ERRORLEVEL%
