@echo off
rem Start Autoflash, the conversion tool, on Windows: double-click this file.
rem (From a terminal, .\start.ps1 does the same; options pass through either way.)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set code=%errorlevel%
echo.
rem Opened by a double-click, the window would close before the last words are read.
rem pause's own words would start at the edge: these are set in, as the tool's are.
echo  Press any key to continue . . .
pause >nul
exit /b %code%
