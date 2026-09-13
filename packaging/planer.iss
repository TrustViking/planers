; Инсталлятор планера (ТЗ §9, этап 5). Собирается из build_local.bat / build_release.bat:
;   ISCC /DAppVersion=<app\version.py> /DIncludeSecrets=0|1 packaging\planer.iss
; Источник файлов — dist\planer (то, что прошло смоук): planer.exe, _internal\, planer.bat, config\*.example.yaml.
;
; Папка и права — как в installer.iss броадкастера: мастер спрашивает «для всех пользователей /
; только для меня» (PrivilegesRequiredOverridesAllowed) и всегда показывает страницу выбора папки.
; По умолчанию {autopf}\Planer: «только для меня» — %LOCALAPPDATA%\Programs\Planer,
; «для всех» — C:\Program Files\Planer. Во frozen-режиме корень планера — папка exe
; (app\paths.py::resolve_root), туда пишутся config\, secrets\, promo\, state\, keystreams\, logs\;
; в Program Files без прав администратора писать нельзя — ставить в папку с правом записи (например D:\_exe\Planer).
;
; Удаление снимает только то, что положил инсталлятор. Рабочие planer.yaml / channels.yaml,
; токены в secrets\, promo\, state\, keystreams\, logs\ создаёт программа — они остаются.

#ifndef AppVersion
  #error AppVersion не передан: номер версии берётся только из app\version.py через build-скрипт
#endif

#ifndef IncludeSecrets
  #define IncludeSecrets 0
#endif

#define RepoRoot AddBackslash(SourcePath) + ".."
#define SourceDir RepoRoot + "\dist\planer"

#if IncludeSecrets
  #define SetupBaseName "planer-setup-local-" + AppVersion
#else
  #define SetupBaseName "planer-setup-" + AppVersion
#endif

[Setup]
AppId={{DC9B9159-696D-4AD9-92BB-92539B3781E5}
AppName=Планер
AppVersion={#AppVersion}
AppVerName=Планер {#AppVersion}
AppPublisher=TrustViking
DefaultDirName={autopf}\Planer
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
DisableDirPage=no
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
OutputDir={#RepoRoot}\dist\installer
OutputBaseFilename={#SetupBaseName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile={#SourceDir}\planers.ico
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=Планер {#AppVersion}
UninstallDisplayIcon={app}\planer.exe
CloseApplications=yes
RestartIfNeededByRun=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Dirs]
; Пустая папка для пакетов: владелец кладёт туда пакет ещё до первого запуска. При удалении не трогается.
Name: "{app}\promo"; Flags: uninsneveruninstall

[Files]
Source: "{#SourceDir}\planer.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\planer.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\planers.ico"; DestDir: "{app}"; Flags: ignoreversion
; Только примеры: рабочие planer.yaml и channels.yaml создаёт программа (app\config\loader.py::ensure_configs_exist).
Source: "{#SourceDir}\config\planer.example.yaml"; DestDir: "{app}\config"; Flags: ignoreversion
Source: "{#SourceDir}\config\channels.example.yaml"; DestDir: "{app}\config"; Flags: ignoreversion
#if IncludeSecrets
; build_local.bat: паспорт программы Google из secrets\ репо. В build_release.bat его нет —
; владелец получает файл отдельно (messages_ru.CLIENT_SECRET_MISSING).
Source: "{#RepoRoot}\secrets\client_secret.json"; DestDir: "{app}\secrets"; Flags: ignoreversion
#endif

[Icons]
; Ярлыки — на planer.bat, а не на exe: без pause окно консоли закрылось бы вместе с программой.
Name: "{autodesktop}\Планер"; Filename: "{app}\planer.bat"; WorkingDir: "{app}"; IconFilename: "{app}\planers.ico"
Name: "{autodesktop}\Планер — пакеты"; Filename: "{app}\promo"
Name: "{autoprograms}\Планер"; Filename: "{app}\planer.bat"; WorkingDir: "{app}"; IconFilename: "{app}\planers.ico"
