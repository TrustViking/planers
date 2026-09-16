

#ifndef AppVersion
  #error AppVersion не передан: номер версии берётся только из app\version.py через build-скрипт
#endif

#ifndef IncludeSecrets
  #define IncludeSecrets 0
#endif

#define RepoRoot RemoveBackslash(SourcePath)
#define SourceDir RepoRoot + "\dist\planer"

#if IncludeSecrets
  #define SetupBaseName "planer-setup-local-" + AppVersion
#else
  #define SetupBaseName "planer-setup-" + AppVersion
#endif

#ifndef OutputDir
#define OutputDir "dist\installer"
#endif

[Setup]
AppId={{DC9B9159-696D-4AD9-92BB-92539B3781E5}
AppName=Planer
AppVersion={#AppVersion}
AppVerName=Planer {#AppVersion}
AppPublisher=Vi0rel
DefaultDirName={src}\Planer
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
DisableDirPage=no
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename={#SetupBaseName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile={#SourceDir}\planers.ico
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=Planer {#AppVersion}
UninstallDisplayIcon={app}\planer.exe
CloseApplications=yes
RestartIfNeededByRun=no


[Dirs]
; Пустая папка для пакетов: владелец кладёт туда пакет ещё до первого запуска. При удалении не трогается.
; config\, secrets\, bcast\, keystreams\, logs\ и app\state\ — данные владельца: удаление их не трогает.
Name: "{app}\bcast"; Flags: uninsneveruninstall

[Files]
Source: "{#SourceDir}\planer.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\planer.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\planers.ico"; DestDir: "{app}"; Flags: ignoreversion
; Технические настройки planer.json: ставятся, только если файла ещё нет, — правки владельца
; переустановка не трогает, удаление тоже. channels.json владелец создаёт сам по шаблону из консоли.
Source: "{#SourceDir}\config\planer.json"; DestDir: "{app}\config"; Flags: onlyifdoesntexist uninsneveruninstall
#if IncludeSecrets
; build_local.bat: данные разработчика — вся папка secrets\ (паспорт программы Google и токены каналов),
; config\channels.json и привязки app\state\bindings.json: установленная программа должна делать прогон
; сразу, без OAuth и ручного копирования. Источник — dist\planer\, куда их положил build_release.bat.
; В build_release.bat этого нет: владелец получает client_secret.json отдельно
; (messages_ru.CLIENT_SECRET_MISSING), а channels.json создаёт сам по шаблону из консоли.
Source: "{#SourceDir}\secrets\*"; DestDir: "{app}\secrets"; Flags: ignoreversion uninsneveruninstall
Source: "{#SourceDir}\config\channels.json"; DestDir: "{app}\config"; Flags: ignoreversion uninsneveruninstall
Source: "{#SourceDir}\app\state\bindings.json"; DestDir: "{app}\app\state"; Flags: ignoreversion uninsneveruninstall skipifsourcedoesntexist
#endif

[Icons]
; Ярлыки — на planer.bat, а не на exe: без pause окно консоли закрылось бы вместе с программой.
Name: "{autodesktop}\Planer"; Filename: "{app}\planer.bat"; WorkingDir: "{app}"; IconFilename: "{app}\planers.ico"
;Name: "{autodesktop}\Planer_"; Filename: "{app}\bcast"
;Name: "{autoprograms}\Planer"; Filename: "{app}\planer.bat"; WorkingDir: "{app}"; IconFilename: "{app}\planers.ico"
