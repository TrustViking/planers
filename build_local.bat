@echo off
chcp 65001 >nul
rem Planer: LOCAL build - the same as build_release.bat, but the installer bundles secrets\client_secret.json.
rem Never publish the resulting installer.
setlocal EnableExtensions

set "PLANER_INCLUDE_SECRETS=1"
call "%~dp0build_release.bat"
exit /b %ERRORLEVEL%
