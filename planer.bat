@echo off
chcp 65001 >nul
rem Planer entry point. Dev stage: venv python -m app.main. Stage 5 (TZ 7.6): planer.exe.
setlocal EnableExtensions

set "ROOT=%~dp0."
set "PYTHON=%ROOT%\.venv_planers\Scripts\python.exe"

if not exist "%PYTHON%" (
  echo [ERROR] Python not found: "%PYTHON%"
  pause
  exit /b 2
)

cd /d "%ROOT%"
echo [INFO] planer args: %*
"%PYTHON%" -m app.main %*
set "CODE=%ERRORLEVEL%"

echo.
echo Exit code: %CODE%
pause
exit /b %CODE%
