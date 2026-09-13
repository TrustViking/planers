@echo off
chcp 65001 >nul
rem Planer launcher for the installed program: lies next to planer.exe (TZ 7.6, 9).
rem Without pause the console window would close together with the program.
setlocal EnableExtensions

set "ROOT=%~dp0."
set "PLANER_EXE=%ROOT%\planer.exe"

if not exist "%PLANER_EXE%" (
  echo [ERROR] planer.exe not found: "%PLANER_EXE%"
  pause
  exit /b 2
)

cd /d "%ROOT%"
"%PLANER_EXE%" %*
set "CODE=%ERRORLEVEL%"

echo.
echo Exit code: %CODE%
pause
exit /b %CODE%
