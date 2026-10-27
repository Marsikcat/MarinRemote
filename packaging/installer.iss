; Installer for MarinRemote (Inno Setup 6). Built by tools\build.py:
;   ISCC /DAppVersion=X.Y.Z packaging\installer.iss
; Per-user install, no administrator rights: %LOCALAPPDATA%\Programs\MarinRemote.
; (Stage 2 — a Windows service for the login screen and UAC — will need an all-users install.)

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "MarinRemote"
#define AppExe "MarinRemote.exe"

[Setup]
AppId={{1A26400A-4E9A-45F7-AB10-B052620B8E40}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Marsikcat
AppPublisherURL=https://github.com/Marsikcat/MarinRemote
AppSupportURL=https://github.com/Marsikcat/MarinRemote/issues
AppUpdatesURL=https://github.com/Marsikcat/MarinRemote/releases
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} — установщик
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=MarinRemote-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "Запускать вместе с Windows (к этому ПК можно будет подключиться)"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\MarinRemote\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; files of an older version that the new one no longer ships
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; the same value the app writes from «Этот ПК → Запускать вместе с Windows»
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExe}"" --autostart"; Tasks: autostart
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "{#AppName}"; Flags: uninsdeletevalue
; marinremote://mr-… links open the app ready to connect (it asks first)
Root: HKCU; Subkey: "Software\Classes\marinremote"; ValueType: string; ValueName: ""; ValueData: "URL:MarinRemote"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\marinremote"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\marinremote\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"",0"
Root: HKCU; Subkey: "Software\Classes\marinremote\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" --connect ""%1"""

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; a silent run is the in-app update: start the new version right away
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: WizardSilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im {#AppExe}"; Flags: runhidden; RunOnceId: "StopApp"
