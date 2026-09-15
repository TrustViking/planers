@echo off
chcp 65001 >nul
rem Planer: release build - exe + installer WITHOUT secrets (TZ 9, stage 5).
rem build_local.bat calls this script with PLANER_INCLUDE_SECRETS=1; that is the only difference.
rem The only environment is .venv_planers: the app and pyinstaller live there.
rem Exit codes: 0 - exe and installer built; 1 - build failed; 3 - exe built, Inno Setup not found.
setlocal EnableExtensions

set "ROOT=%~dp0."
cd /d "%ROOT%"

if not defined PLANER_INCLUDE_SECRETS set "PLANER_INCLUDE_SECRETS=0"
set "PYINSTALLER_PIN=pyinstaller==6.19.0"
set "BUILD_PYTHON=%ROOT%\.venv_planers\Scripts\python.exe"
set "SPEC=%ROOT%\planer.spec"
set "ISS=%ROOT%\planer.iss"
set "DIST_APP=%ROOT%\dist\planer"
set "APP_ICON=%ROOT%\planers.ico"
set "DISCOVERY_DOC=%DIST_APP%\_internal\googleapiclient\discovery_cache\documents\youtube.v3.json"

if "%PLANER_INCLUDE_SECRETS%"=="1" (
  echo [WARN] LOCAL build: installer bundles secrets\client_secret.json - do NOT publish it.
) else (
  echo [INFO] RELEASE build: installer contains no secrets.
)

rem --- 1. environment: .venv_planers only ----------------------------------------
if not exist "%BUILD_PYTHON%" (
  echo [ERROR] Python not found: "%BUILD_PYTHON%"
  call :finish 1
  exit /b 1
)
"%BUILD_PYTHON%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo [INFO] PyInstaller not found in .venv_planers - installing %PYINSTALLER_PIN% ...
  "%BUILD_PYTHON%" -m pip install --disable-pip-version-check --quiet %PYINSTALLER_PIN%
  if errorlevel 1 (
    echo [ERROR] pip install %PYINSTALLER_PIN% into .venv_planers failed.
    call :finish 1
    exit /b 1
  )
)

rem --- 2. version: app\version.py is the only source ----------------------------
set "APP_VERSION="
set "VERSION_FILE=%TEMP%\planer_build_version.tmp"
"%BUILD_PYTHON%" -c "from app.version import APP_VERSION; print(APP_VERSION)" > "%VERSION_FILE%"
if not errorlevel 1 set /p APP_VERSION=<"%VERSION_FILE%"
if exist "%VERSION_FILE%" del /Q "%VERSION_FILE%"
if not defined APP_VERSION (
  echo [ERROR] Cannot read APP_VERSION from app\version.py.
  call :finish 1
  exit /b 1
)
echo [INFO] Version: %APP_VERSION%

if not exist "%APP_ICON%" (
  echo [ERROR] Icon not found: "%APP_ICON%"
  call :finish 1
  exit /b 1
)

if "%PLANER_INCLUDE_SECRETS%"=="1" if not exist "%ROOT%\secrets\client_secret.json" (
  echo [ERROR] secrets\client_secret.json not found - local installer needs it.
  call :finish 1
  exit /b 1
)

rem --- 3. exe ------------------------------------------------------------------
if exist "%ROOT%\build" rmdir /S /Q "%ROOT%\build"
if exist "%ROOT%\dist" rmdir /S /Q "%ROOT%\dist"
"%BUILD_PYTHON%" -m PyInstaller --noconfirm --clean --distpath "%ROOT%\dist" --workpath "%ROOT%\build" "%SPEC%"
if errorlevel 1 (
  echo [ERROR] PyInstaller failed.
  call :finish 1
  exit /b 1
)
if not exist "%DISCOVERY_DOC%" (
  echo [ERROR] youtube.v3.json is missing in the build: "%DISCOVERY_DOC%"
  call :finish 1
  exit /b 1
)

rem Next to the exe: launcher, icon for shortcuts and config\planer.json (technical settings, shipped filled in).
rem config\channels.json is NOT shipped: the owner creates it, the program prints its template.
mkdir "%DIST_APP%\config" >nul 2>&1
copy /Y "%ROOT%\planer.bat" "%DIST_APP%\planer.bat" >nul
copy /Y "%APP_ICON%" "%DIST_APP%\planers.ico" >nul
copy /Y "%ROOT%\config\planer.json" "%DIST_APP%\config\planer.json" >nul
if not exist "%DIST_APP%\config\planer.json" (
  echo [ERROR] config\planer.json was not copied to "%DIST_APP%\config".
  call :finish 1
  exit /b 1
)
echo [OK] Portable build: %DIST_APP%

rem --- 4. installer ------------------------------------------------------------
set "ISCC="
for /f "delims=" %%I in ('where ISCC.exe 2^>nul') do if not defined ISCC set "ISCC=%%I"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC (
  echo [ERROR] Inno Setup 6 ^(ISCC.exe^) not found - the exe is built, but the installer needs Inno Setup.
  echo         Install Inno Setup 6 from https://jrsoftware.org/isdl.php and run this script again.
  call :finish 3
  exit /b 3
)

"%ISCC%" /Q /DAppVersion=%APP_VERSION% /DIncludeSecrets=%PLANER_INCLUDE_SECRETS% "%ISS%"
if errorlevel 1 (
  echo [ERROR] Inno Setup failed.
  call :finish 1
  exit /b 1
)
if "%PLANER_INCLUDE_SECRETS%"=="1" (
  echo [OK] Installer: dist\installer\planer-setup-local-%APP_VERSION%.exe  ^(contains secrets - do NOT publish^)
) else (
  echo [OK] Installer: dist\installer\planer-setup-%APP_VERSION%.exe
)
call :finish 0
exit /b 0

:finish
if not defined NO_PAUSE pause
exit /b %1
