@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0.\installer\Install.ps1" -PayloadRoot "%~dp0." -Destination "%LOCALAPPDATA%\BSideOliviaLocal\install"
exit /b %ERRORLEVEL%
