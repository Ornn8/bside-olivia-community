@echo off
setlocal enabledelayedexpansion

rem ============================================================
rem  Launcher for olivia_memory.py
rem
rem  KEEP THIS FILE PURE ASCII.  A .bat with non-ASCII text is
rem  read with the wrong code page on some machines; cmd then
rem  splits the lines apart and runs the fragments as commands.
rem  NEVER add "chcp" to this file.  And keep ")" out of the echo
rem  text inside an "if (" block - an unescaped ")" ends the block.
rem
rem  All Chinese output comes from olivia_memory.py, which detects
rem  the console encoding and matches it.
rem
rem  Python search order:
rem    0) the Python remembered from last time (python_path.txt)
rem    1) python / python3 on PATH           (WindowsApps skipped)
rem    2) the py launcher on PATH
rem    3) the Python shipped with Olivia
rem    4) WindowsApps python                 (last resort)
rem    5) otherwise: ask the user to drag the Olivia folder in
rem
rem  Step 3 checks two folder levels, because the install is often
rem  nested:  X:\SomeFolder\OliviaSomething\runtime\python-*\python.exe
rem  A one-level scan of X:\*Olivia* cannot see that layout.
rem ============================================================

rem ---- 0) olivia_memory.py must sit next to this .bat ----
if not exist "%~dp0olivia_memory.py" (
  echo.
  echo   olivia_memory.py is not next to this .bat.
  echo.
  echo   Most likely you opened the zip and double-clicked the .bat
  echo   inside the archive viewer. Only the .bat gets unpacked to a
  echo   temp folder, so the other files are missing.
  echo.
  echo   Extract the whole folder first, then open that folder and
  echo   run the .bat from there.
  echo.
  pause
  exit /b 1
)

set "PY="
set "REMEMBER=%~dp0python_path.txt"

rem ---- reuse the Python that worked last time ----
if exist "%REMEMBER%" (
  set "OLD="
  set /p OLD=<"%REMEMBER%"
  set "OLD=!OLD:"=!"
  if defined OLD call :try "!OLD!"
)

rem ---- 1) python / python3 on PATH (WindowsApps skipped) ----
for /f "delims=" %%P in ('where python 2^>nul ^| find /i /v "WindowsApps"') do call :try "%%~P"
if not defined PY for /f "delims=" %%P in ('where python3 2^>nul ^| find /i /v "WindowsApps"') do call :try "%%~P"

rem ---- 2) the py launcher ----
if not defined PY for /f "delims=" %%P in ('where py 2^>nul') do call :try "%%~P"

rem ---- 3) the Python shipped with Olivia ----
rem   level 1:  X:\*Olivia*
for %%D in (C D E F G H I J K L M N) do if exist "%%D:\" for /d %%R in ("%%D:\*Olivia*") do call :check "%%~fR"
rem   level 2:  X:\*\*Olivia*
for %%D in (C D E F G H I J K L M N) do if exist "%%D:\" for /d %%A in ("%%D:\*") do for /d %%R in ("%%~fA\*Olivia*") do call :check "%%~fR"
rem   and under the user profile
if not defined PY if defined LOCALAPPDATA for /d %%R in ("%LOCALAPPDATA%\*Olivia*") do call :check "%%~fR"

rem ---- 4) the Microsoft Store python, last resort ----
if not defined PY for /f "delims=" %%P in ('where python 2^>nul ^| find /i "WindowsApps"') do call :try "%%~P"

if defined PY goto :run

rem ---- 5) nothing found: ask for the Olivia folder ----
echo.
echo   No usable Python was found on this computer.
echo.
echo   Please drag your Olivia install folder into this window
echo   and press Enter.  It is the folder that contains
echo   "install", "runtime" and "launcher" side by side.
echo.
set "OLIV="
set /p "OLIV=  Folder: "
set "OLIV=%OLIV:"=%"
if not defined OLIV goto :giveup
call :check "%OLIV%"
if defined PY goto :run
echo.
echo   Still nothing under that folder. These were tried:
echo     %OLIV%\python.exe
echo     %OLIV%\runtime\python-*\python.exe
echo     %OLIV%\python-*\python.exe
echo.
goto :giveup

:run
echo.
echo   Found Python: "%PY%"
(echo "%PY%")>"%REMEMBER%" 2>nul
echo.
"%PY%" "%~dp0olivia_memory.py" %*
echo.
pause
endlocal
exit /b 0

:giveup
echo   The tool cannot run until a Python is available.
echo.
echo   Option 1: Install Python 3 from python.org and tick
echo             "Add python.exe to PATH", then run this again.
echo   Option 2: Run the tool by hand with a Python you already
echo             have, for example:
echo               python olivia_memory.py
echo.
pause
endlocal
exit /b 1

rem ------------------------------------------------------------
rem  :check <folder>
rem    The folder may be the Olivia root, its runtime folder, or
rem    the folder holding python.exe itself. All three are tried,
rem    so it does not matter which one the user drags in.
rem ------------------------------------------------------------
:check
if defined PY exit /b 0
if exist "%~1\python.exe" call :try "%~1\python.exe"
if defined PY exit /b 0
for /d %%F in ("%~1\runtime\python-*") do if not defined PY call :try "%%~fF\python.exe"
if defined PY exit /b 0
for /d %%F in ("%~1\python-*") do if not defined PY call :try "%%~fF\python.exe"
if defined PY exit /b 0
for /f "delims=" %%P in ('dir /s /b "%~1\runtime\python.exe" 2^>nul') do if not defined PY call :try "%%~P"
exit /b 0

rem ------------------------------------------------------------
rem  :try <path to a python executable>
rem    Accepts it only if it really imports what the tool needs.
rem    That is what tells a real Python apart from the Microsoft
rem    Store stub, which exists on many machines and does not.
rem ------------------------------------------------------------
:try
if defined PY exit /b 0
"%~1" -c "import sqlite3, json" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
exit /b 0
