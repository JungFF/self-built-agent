@echo off
setlocal
set ROOT=%~dp0
if "%ROOT:~-1%"=="\" set ROOT=%ROOT:~0,-1%
set /p VER=<"%ROOT%\current.txt"
set VDIR=%ROOT%\versions\%VER%
set HERMES_HOME=%ROOT%\data
cd /d "%VDIR%"
"%VDIR%\hermes-agent\venv\Scripts\python.exe" -m tools.recover "%ROOT%\data" "%VDIR%\factory"
echo.
pause
endlocal
