@echo off
chcp 65001 >nul
rem Planer: LOCAL build - the same as build_release.bat, but the build carries the developer data:
rem the whole secrets\ folder (client_secret.json, channels.json and the channel tokens).
rem The built program runs at once - no OAuth, no hand-copying.
rem Never publish the resulting installer.
setlocal EnableExtensions

set "PLANER_INCLUDE_SECRETS=1"
call "%~dp0build_release.bat"
exit /b %ERRORLEVEL%
