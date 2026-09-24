@echo off
REM ===================================================================
REM  Tiger.M.M - run ALL verification gates ("verify on every change")
REM
REM  Usage:
REM    verify.bat             full   (all gates + canonical)
REM    verify.bat --quick     fast   (skip gates needing network/services)
REM    verify.bat --list      show which gates will run
REM    verify.bat --only skill_format
REM    verify.bat --json      machine-readable result
REM
REM  Exit code: 0 = all green, 1 = any red (or user-state polluted).
REM
REM  NOTE: keep this file ASCII-only. Chinese in .bat comments gets
REM  mangled by cmd.exe (GBK/UTF-8 mismatch) and breaks parsing.
REM ===================================================================
setlocal
set PYTHONDONTWRITEBYTECODE=1
pushd "%~dp0"
call "%~dp0_find_python.bat"
if errorlevel 1 (
    popd
    endlocal & exit /b 1
)
"%PY%" -B scripts\hermes_verify.py %*
set RC=%ERRORLEVEL%
popd
endlocal & exit /b %RC%
