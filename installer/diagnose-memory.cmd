@echo off
setlocal
set "OLIVIA_PROBE_PY="
for /d %%D in ("%~dp0runtime\python-*-embed-amd64") do if exist "%%~fD\python.exe" set "OLIVIA_PROBE_PY=%%~fD\python.exe"
if not defined OLIVIA_PROBE_PY (
  echo Please extract both probe files next to Olivia.exe and run again.
  pause
  exit /b 1
)
"%OLIVIA_PROBE_PY%" "%~dp0diagnose_memory_import.py" "%~dp0memory-import-report.json"
if errorlevel 1 (
  echo Probe failed. Please capture this window.
) else (
  echo Please send memory-import-report.json for diagnosis.
)
pause
