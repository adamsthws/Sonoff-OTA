@echo off
rem Start Autoflash, the conversion tool, on Windows: double-click this file, or
rem run it from a terminal (.\start.cmd run --verbose: the tool's own commands
rem and options pass through). It runs tools\windows-start.ps1 with PowerShell's
rem script policy set aside for this one run, so nothing has to be changed first.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\windows-start.ps1" %*
exit /b %errorlevel%
