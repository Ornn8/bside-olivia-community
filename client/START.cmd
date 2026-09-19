@echo off
setlocal
if not exist "%LOCALAPPDATA%\BSideOliviaLocal\install\START.cmd" (echo INSTALL_FIRST & exit /b 2)
call "%LOCALAPPDATA%\BSideOliviaLocal\install\START.cmd"
exit /b %ERRORLEVEL%
