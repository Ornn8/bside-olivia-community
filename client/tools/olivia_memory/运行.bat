@echo off
setlocal enabledelayedexpansion

rem ============================================================
rem  Launcher for olivia_memory.py
rem
rem  KEEP THIS FILE PURE ASCII.
rem
rem  Why: the console code page differs between machines (some are
rem  65001/UTF-8, some are 936/GBK). A .bat containing non-ASCII text
rem  gets mis-read on the other kind, and cmd then splits lines apart
rem  and tries to run the fragments as commands.
rem
rem  An earlier version was UTF-8 with "chcp 65001". On a machine where
rem  chcp did not take effect, the rem/echo lines were torn apart and
rem  executed as garbage commands. ASCII-only text cannot break that way.
rem
rem  NEVER add "chcp" to this file. Also: inside an "if (" block an
rem  unescaped ")" ends the block early and silently breaks the script,
rem  so keep brackets out of the echo text below.
rem
rem  All Chinese output comes from olivia_memory.py, which detects the
rem  console encoding and matches it.
rem ============================================================

rem ---- 0) olivia_memory.py must sit next to this .bat ----
rem   The usual way to hit this: double-clicking the .bat straight from
rem   inside a zip viewer. 7-Zip and friends unpack only the .bat to a
rem   temp folder, the .py is not there, and Python then reports a very
rem   confusing "can't open file ...\Temp\7zXXXX\olivia_memory.py".
rem   Say it plainly instead.
if not exist "%~dp0olivia_memory.py" (
  echo.
  echo   olivia_memory.py is not next to this .bat.
  echo.
  echo   Most likely you opened the zip and double-clicked the .bat
  echo   inside the archive viewer. Only the .bat gets unpacked to a
  echo   temp folder, so the other two files are missing.
  echo.
  echo   Extract the whole folder first, then open that folder and
  echo   run the .bat from there.
  echo.
  pause
  exit /b 1
)

set "PY="
set "CANDS="

rem ---- 1) python / python3 on PATH ----
rem   The WindowsApps entries are skipped here: on a machine with no real
rem   Python they are only a stub that opens the Microsoft Store. A machine
rem   that DOES have the Store Python gets a second chance in step 5.
for /f "delims=" %%P in ('where python 2^>nul ^| find /i /v "WindowsApps"') do set CANDS=!CANDS! "%%P"
for /f "delims=" %%P in ('where python3 2^>nul ^| find /i /v "WindowsApps"') do set CANDS=!CANDS! "%%P"

rem ---- 2) the Python launcher py.exe on PATH ----
rem   Plenty of people install Python and end up with only "py" on the
rem   path, no "python". py.exe is the official launcher and finds the
rem   newest installed Python.
for /f "delims=" %%P in ('where py 2^>nul') do set CANDS=!CANDS! "%%P"

rem ---- 3) the python shipped with Olivia ----
rem   the runtime folder name contains a version number,
rem   so search recursively and filter by "python-"
for %%D in (C D E F G H I J K) do for /d %%R in ("%%D:\*Olivia*") do for /f "delims=" %%P in ('dir /s /b "%%~fR\python.exe" 2^>nul ^| find /i "python-"') do set CANDS=!CANDS! "%%~fP"
for /d %%R in ("%LOCALAPPDATA%\*Olivia*") do for /f "delims=" %%P in ('dir /s /b "%%~fR\python.exe" 2^>nul ^| find /i "python-"') do set CANDS=!CANDS! "%%~fP"

rem ---- 4) take the first candidate that really runs ----
for %%P in (%CANDS%) do if not defined PY (
  "%%~P" -c "import sqlite3, json" >nul 2>&1
  if not errorlevel 1 set "PY=%%~P"
)

rem ---- 5) last resort: the Microsoft Store Python ----
rem   It lives under WindowsApps, which step 1 skipped. Only tried when
rem   nothing else worked. Running a candidate is what tells a real Python
rem   from the plain stub: the real one imports fine, the stub does not.
if defined PY goto :have_python
for /f "delims=" %%P in ('where python 2^>nul ^| find /i "WindowsApps"') do call :try_python "%%~P"
:have_python

if not defined PY (
  echo.
  echo   Python not found.  /  No usable Python.
  echo.
  echo   Try one of these:
  echo     1. Put this folder next to your Olivia install folder, then run again.
  echo     2. Double-click olivia_memory.py directly - if Windows opens it with
  echo        a Python launcher, that works just as well.
  echo     3. Install Python 3 and tick "Add Python to PATH". A Microsoft Store
  echo        Python works too.
  echo     4. Run it by hand:   python olivia_memory.py   or   py olivia_memory.py
  echo.
  pause
  exit /b 1
)

echo   Using Python: %PY%
"%PY%" "%~dp0olivia_memory.py" %*

echo.
pause
endlocal
exit /b 0

:try_python
if defined PY exit /b 0
"%~1" -c "import sqlite3, json" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
exit /b 0
