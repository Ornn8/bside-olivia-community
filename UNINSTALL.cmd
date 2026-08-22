@echo off
setlocal
if not exist "%LOCALAPPDATA%\BSideOliviaLocal\install\UNINSTALL.cmd" (echo NOT_INSTALLED & exit /b 2)
call "%LOCALAPPDATA%\BSideOliviaLocal\install\UNINSTALL.cmd"
exit /b %ERRORLEVEL%
