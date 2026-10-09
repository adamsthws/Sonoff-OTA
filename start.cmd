@echo off
rem Start the S60 conversion tool on Windows: double-click this file.
rem (From a terminal, .\start.ps1 does the same; options pass through either way.)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set code=%errorlevel%
echo.
rem Opened by a double-click, the window would close before the last words are read.
pause
exit /b %code%
