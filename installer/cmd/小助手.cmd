@echo off
setlocal
set ROOT=%~dp0
if "%ROOT:~-1%"=="\" set ROOT=%ROOT:~0,-1%
set /p VER=<"%ROOT%\current.txt"
set VDIR=%ROOT%\versions\%VER%
set HERMES_HOME=%ROOT%\data
set PLAYWRIGHT_BROWSERS_PATH=%VDIR%\ms-playwright
cd /d "%VDIR%"
start "" "%VDIR%\hermes-agent\venv\Scripts\pythonw.exe" -m tools.launcher "%ROOT%"
endlocal
