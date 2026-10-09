@echo off
rem ===================================================================
rem  Tiger.M.M - verify the API keys you put into keys.json
rem
rem  KEEP THIS FILE PURE ASCII.  DO NOT add `chcp 65001` here.
rem
rem  Measured 2026-10-09 (A/B test on zh-CN Windows):
rem    .bat with `chcp 65001` + UTF-8 Chinese
rem        -> cmd loses track of its byte offset in codepage 65001 and
rem           starts executing halves of later lines, e.g. it echoed
rem           "'<chinese-fragment>' is not recognized as an internal or
rem            external command, operable program or batch file."
rem           (a known cmd.exe bug with batch files + codepage 65001)
rem    .bat without chcp, Chinese stored as UTF-8
rem        -> runs fine, but the Chinese renders as mojibake on a GBK
rem           console, so the user cannot read it anyway
rem  => All Chinese output lives in check_keys.py.  Python prints through
rem     the Windows console API, which is codepage-independent, so the
rem     text comes out right no matter what the console is set to.
rem
rem  Historical note: this file used to carry its own Chinese echo lines
rem  plus `chcp 65001` -- exactly the broken combination above.  A
rem  recipient double-clicking it got a flood of "is not recognized as an
rem  internal or external command" instead of a readable result.
rem ===================================================================
setlocal
cd /d "%~dp0"

set PY=
where python >nul 2>&1 && set PY=python
if "%PY%"=="" (where py >nul 2>&1 && set PY=py -3)
if "%PY%"=="" (
  echo [X] Python not found. Install Python 3.10+ from python.org
  echo     ^(tick "Add python.exe to PATH" on the first screen^)
  echo     Step-by-step guide: PYTHON_FIRST.txt
  echo.
  pause
  exit /b 1
)

%PY% -B check_keys.py
set RC=%ERRORLEVEL%
echo.
pause
exit /b %RC%
