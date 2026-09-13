@echo off
chcp 65001 >nul
rem Planer launcher (TZ 7.6). One file for both cases:
rem   installed program - planer.exe lies next to this file, it is started;
rem   repository        - no planer.exe here, app.main is started from .venv_planers.
rem Without pause the console window would close together with the program.
setlocal EnableExtensions

set "ROOT=%~dp0."
set "PLANER_EXE=%ROOT%\planer.exe"
set "PYTHON=%ROOT%\.venv_planers\Scripts\python.exe"

cd /d "%ROOT%"
if exist "%PLANER_EXE%" (
  "%PLANER_EXE%" %*
) else if exist "%PYTHON%" (
  "%PYTHON%" -m app.main %*
) else (
  echo [ERROR] Neither planer.exe nor Python found next to planer.bat: "%ROOT%"
  pause
  exit /b 2
)
set "CODE=%ERRORLEVEL%"

echo.
echo Exit code: %CODE%
pause
exit /b %CODE%
