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
rem    real python.org install first (registry, per-user, then machine-wide,
rem    3.10-3.13) and only fall back to PATH as a last resort.
rem
rem  Search order:
rem    1. already-set PY (caller override)
rem    2. TMM_PY environment variable
rem    3. per-user install:  %LOCALAPPDATA%\Programs\Python\Python3xx
rem    4. machine-wide:      %ProgramFiles%\Python3xx  /  C:\Python3xx
rem    5. PATH python.exe    (last resort; may be a venv/conda interpreter)
rem
rem  DELIBERATELY NOT HERE: the registry HKCU\Software\Python\PythonCore\*\
rem  InstallPath.  Tried it on 2026-10-09 and it was a net loss: this box has
rem  other tools that stage a python into ProgramData (Accio pre-install) and
rem  register it as the 3.12 install, so "first registry hit wins" picked THAT
rem  interpreter instead of the user's own -- i.e. exactly the wrong-python
rem  hazard described above.  The registry also carries stale keys (a 3.13 key
rem  pointing at a directory with no python.exe).  Standard dirs are ordered
rem  and boring; keep it that way.  (The auto-install below lands in the
rem  per-user dir, which rule 3 already finds.)
rem
rem  NOT FOUND  ->  try to install Python automatically (2026-10-09)
rem    Runs _get_python.ps1 (Windows ships PowerShell, so this works even on
rem    a machine with no Python at all): downloads from mirrors with a
rem    SHA256 + Authenticode double check, then installs per-user /quiet
rem    (no admin, no UAC).  Only once -- a TMM_NO_AUTOPY guard stops loops.
rem    Force it for testing/reinstall:   set TMM_PYTHON_FETCH=1
rem    Skip it entirely:                 set TMM_NO_AUTOPY=1
rem
rem  Exit code: 0 = found, 1 = not found (callers must check).
rem  ASCII only on purpose: cmd.exe reads .bat in the OEM codepage and
rem  non-ASCII bytes get mangled into garbage commands.
rem ===================================================================
if defined PY if exist "%PY%" goto :have
if defined TMM_PY if exist "%TMM_PY%" set "PY=%TMM_PY%"
if defined TMM_PYTHON_FETCH goto :fetch
if not defined PY call :scan
if defined PY goto :have

:fetch
rem Reaching this line means: this machine has no Python we can use.
rem Try to put one there instead of dumping the whole job on the user.
if defined TMM_NO_AUTOPY goto :manual
set "TMM_NO_AUTOPY=1"
echo.
echo   Python not found.
echo   Fetching and installing Python 3.11 automatically -- per-user, no admin
echo   prompt, no system changes.  Only this once; it can take a few minutes.
echo   (Official file: verified by SHA256 + Python Software Foundation signature.)
echo.
if exist "%~dp0_get_python.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_get_python.ps1"
    echo.
    echo   Re-checking...
) else (
    echo   [X] _get_python.ps1 is missing from this folder.
)
call :scan
if defined PY goto :have

:manual
echo [ERROR] Python not found. Opening PYTHON_FIRST.txt ...
if exist "%~dp0PYTHON_FIRST.txt" start "" "%~dp0PYTHON_FIRST.txt"
echo.
echo   Install Python 3.10+ from https://www.python.org/downloads/
echo   ^(tick "Add python.exe to PATH" on the first screen^)
echo.
echo   Chinese guide: PYTHON_FIRST.txt  -- opened in Notepad.
echo   Or point us at it:  set TMM_PY=C:\Python311\python.exe
exit /b 1

:have
set "PYW="
if exist "%PY:python.exe=pythonw.exe%" set "PYW=%PY:python.exe=pythonw.exe%"
exit /b 0

:scan
rem -- 1) per-user install (this is where the auto-install below puts it) --
if not defined PY call :try "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY call :try "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY call :try "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY call :try "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
rem -- 2) machine-wide --
if not defined PY call :try "%ProgramFiles%\Python313\python.exe"
if not defined PY call :try "%ProgramFiles%\Python312\python.exe"
if not defined PY call :try "%ProgramFiles%\Python311\python.exe"
if not defined PY call :try "%ProgramFiles%\Python310\python.exe"
if not defined PY call :try "C:\Python313\python.exe"
if not defined PY call :try "C:\Python312\python.exe"
if not defined PY call :try "C:\Python311\python.exe"
if not defined PY call :try "C:\Python310\python.exe"
rem -- 3) last resort: whatever "python" is on PATH (may be a venv/conda python) --
if not defined PY for %%P in (python.exe) do if not defined PY set "PY=%%~$PATH:P"
exit /b 0

:try
if not defined PY if exist %1 set "PY=%~1"
exit /b 0
