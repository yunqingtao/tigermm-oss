@echo off
rem ===================================================================
rem  Tiger.M.M - locate a Python interpreter (shared by all launchers)
rem
rem  Sets:
rem    PY  = python.exe   (required)
rem    PYW = pythonw.exe  (no console window; may be empty)
rem
rem  Why look here first, and NOT at PATH:
rem    2026-09-21 lesson (cost a red suite): PATH usually points at some
rem    OTHER python (a venv / conda / another tool's runtime). Picking that
rem    one gives a TMM without pywin32/fastapi etc -> canonical tests go red
rem    with bogus async errors, and state files get touched. So: look for a
rem    real python.org install first (per-user, then machine-wide, 3.10-3.13)
rem    and only fall back to PATH as a last resort.
rem
rem  Search order:
rem    1. already-set PY (caller override)
rem    2. TMM_PY environment variable
rem    3. per-user install:  %LOCALAPPDATA%\Programs\Python\Python3xx
rem    4. machine-wide:      %ProgramFiles%\Python3xx  /  C:\Python3xx
rem    5. PATH python.exe    (last resort; may be a venv/conda interpreter)
rem
rem  Exit code: 0 = found, 1 = not found (callers must check).
rem  ASCII only on purpose: cmd.exe reads .bat in the OEM codepage and
rem  non-ASCII bytes get mangled into garbage commands.
rem ===================================================================
if defined PY if exist "%PY%" goto :have
if defined TMM_PY if exist "%TMM_PY%" set "PY=%TMM_PY%"
if not defined PY call :try "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY call :try "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY call :try "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY call :try "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PY call :try "%ProgramFiles%\Python313\python.exe"
if not defined PY call :try "%ProgramFiles%\Python312\python.exe"
if not defined PY call :try "%ProgramFiles%\Python311\python.exe"
if not defined PY call :try "%ProgramFiles%\Python310\python.exe"
if not defined PY call :try "C:\Python313\python.exe"
if not defined PY call :try "C:\Python312\python.exe"
if not defined PY call :try "C:\Python311\python.exe"
if not defined PY call :try "C:\Python310\python.exe"
rem last resort: whatever "python" is on PATH (may be a venv/conda python)
if not defined PY for %%P in (python.exe) do if not defined PY set "PY=%%~$PATH:P"

:have
if not defined PY (
    echo [ERROR] Python not found.
    echo         Install Python 3.10+ from https://www.python.org/downloads/
    echo         or set TMM_PY to the full path of python.exe, e.g.
    echo             set TMM_PY=C:\Python311\python.exe
    exit /b 1
)
set "PYW="
if exist "%PY:python.exe=pythonw.exe%" set "PYW=%PY:python.exe=pythonw.exe%"
exit /b 0

:try
if not defined PY if exist %1 set "PY=%~1"
exit /b 0
