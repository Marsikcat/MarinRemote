; Installer for MarinRemote (Inno Setup 6). Built by tools\build.py:
;   ISCC /DAppVersion=X.Y.Z packaging\installer.iss
; One installation in protected Program Files, including the Windows input service.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "MarinRemote"
#define AppExe "MarinRemote.exe"

[Setup]
; Keep the identity of the former Admin installer so those copies upgrade in place.
AppId=MarinRemote.Admin
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Marsikcat
AppPublisherURL=https://github.com/Marsikcat/MarinRemote
AppSupportURL=https://github.com/Marsikcat/MarinRemote/issues
AppUpdatesURL=https://github.com/Marsikcat/MarinRemote/releases
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} — установщик
; Do not inherit a per-user path or allow /DIR to put a SYSTEM binary in a writable folder.
DefaultDirName={commonpf64}\{#AppName}
UsePreviousAppDir=no
DisableDirPage=yes
PrivilegesRequired=admin
OutputBaseFilename=MarinRemote-Setup-{#AppVersion}
UninstallDisplayName={#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
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
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent; Check: ShouldLaunchApp
; a silent run is the in-app update: start the new version right away
Filename: "{app}\{#AppExe}"; Flags: nowait runasoriginaluser; Check: ShouldLaunchSilentApp

[UninstallRun]
; Session 0 belongs to services and is stopped separately before uninstalling.
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im {#AppExe} /fi ""SESSION ne 0"""; Flags: runhidden; RunOnceId: "StopApp"

[Code]
const
  InputServiceName = 'MarinRemoteInput';
  LegacyUserKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{1A26400A-4E9A-45F7-AB10-B052620B8E40}_is1';
  SC_MANAGER_CONNECT = $0001;
  SERVICE_QUERY_STATUS = $0004;
  SERVICE_STOPPED = 1;
  SERVICE_START_PENDING = 2;
  SERVICE_STOP_PENDING = 3;
  ERROR_SERVICE_DOES_NOT_EXIST = 1060;
  ERROR_SERVICE_CANNOT_ACCEPT_CTRL = 1061;
  ERROR_SERVICE_NOT_ACTIVE = 1062;
  ERROR_SERVICE_MARKED_FOR_DELETE = 1072;
  ServiceWaitAttempts = 120;  // 60 seconds at 500 ms per poll

type
  TServiceStatus = record
    ServiceType, CurrentState, ControlsAccepted, Win32ExitCode,
      ServiceSpecificExitCode, CheckPoint, WaitHint: LongWord;
  end;

// Inno Setup 6 uses a 32-bit setup process; these two pointer arguments are always NULL.
function OpenSCManager(MachineName, DatabaseName: LongWord; Access: LongWord): THandle;
  external 'OpenSCManagerW@advapi32.dll stdcall';
function OpenService(Manager: THandle; Name: String; Access: LongWord): THandle;
  external 'OpenServiceW@advapi32.dll stdcall';
function QueryServiceStatus(Service: THandle; var Status: TServiceStatus): Integer;
  external 'QueryServiceStatus@advapi32.dll stdcall';
function CloseServiceHandle(Handle: THandle): Integer;
  external 'CloseServiceHandle@advapi32.dll stdcall';

function ServiceError(Operation: String; ErrorCode: LongWord): String;
begin
  Result := Operation + ' (' + InputServiceName + '): ' + SysErrorMessage(ErrorCode);
end;

// Read SCM state rather than parsing localized sc.exe output. State 0 means absent.
// Release every handle so a pending deletion can finish before files are removed.
function ReadInputServiceState(var State: LongWord): String;
var
  Manager, Service: THandle;
  Status: TServiceStatus;
  ErrorCode: LongWord;
begin
  Result := '';
  State := 0;
  Manager := OpenSCManager(0, 0, SC_MANAGER_CONNECT);
  if Manager = 0 then begin
    Result := ServiceError('OpenSCManager', DLLGetLastError);
    Exit;
  end;
  try
    Service := OpenService(Manager, InputServiceName, SERVICE_QUERY_STATUS);
    if Service = 0 then begin
      ErrorCode := DLLGetLastError;
      if ErrorCode = ERROR_SERVICE_MARKED_FOR_DELETE then
        State := SERVICE_STOP_PENDING
      else if ErrorCode <> ERROR_SERVICE_DOES_NOT_EXIST then
        Result := ServiceError('OpenService', ErrorCode);
      Exit;
    end;
    try
      if QueryServiceStatus(Service, Status) = 0 then
        Result := ServiceError('QueryServiceStatus', DLLGetLastError)
      else
        State := Status.CurrentState;
    finally
      CloseServiceHandle(Service);
    end;
  finally
    CloseServiceHandle(Manager);
  end;
end;

function StopInputService: String;
var
  Attempt, ExitCode: Integer;
  State: LongWord;
  StopRequested: Boolean;
begin
  StopRequested := False;
  for Attempt := 0 to ServiceWaitAttempts do begin
    Result := ReadInputServiceState(State);
    if Result <> '' then Exit;
    if (State = 0) or (State = SERVICE_STOPPED) then Exit;
    if Attempt = ServiceWaitAttempts then Break;
    // A service cannot accept STOP while starting. Retry transient stop races;
    // sc.exe returning success only acknowledges the request, not completion.
    if not StopRequested and (State <> SERVICE_START_PENDING) and
        (State <> SERVICE_STOP_PENDING) then begin
      if not Exec(ExpandConstant('{sys}\sc.exe'), 'stop ' + InputServiceName,
          '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then begin
        Result := ServiceError('sc.exe stop', ExitCode);
        Exit;
      end;
      Log(Format('sc.exe stop %s: %d', [InputServiceName, ExitCode]));
      if ExitCode = 0 then
        StopRequested := True
      else if (ExitCode <> ERROR_SERVICE_CANNOT_ACCEPT_CTRL) and
          (ExitCode <> ERROR_SERVICE_NOT_ACTIVE) and
          (ExitCode <> ERROR_SERVICE_DOES_NOT_EXIST) and
          (ExitCode <> ERROR_SERVICE_MARKED_FOR_DELETE) then begin
        Result := ServiceError('sc.exe stop', ExitCode);
        Exit;
      end;
    end;
    Sleep(500);
  end;
  Result := 'Не удалось остановить ' + InputServiceName + ' за 60 секунд. ' +
    'Файлы не изменены. Закройте сеансы или перезагрузите ПК и повторите попытку.';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  // Never run an old uninstaller from a user-writable folder with elevated rights.
  // Its data folder is outside the installation and survives the normal uninstall.
  if RegKeyExists(HKCU, LegacyUserKey) then begin
    Result := 'На этом ПК установлена прежняя версия MarinRemote для одного пользователя. ' +
      'Закройте MarinRemote через трей → «Выход», удалите прежнюю версию в ' +
      '«Параметры Windows → Приложения» и запустите этот установщик снова. ' +
      'Ключ устройства, контакты и настройки сохранятся.';
    Exit;
  end;
  if CompareText(RemoveBackslash(ExpandFileName(ExpandConstant('{app}'))),
      ExpandConstant('{commonpf64}\{#AppName}')) <> 0 then begin
    Result := 'MarinRemote должен быть установлен в ' +
      ExpandConstant('{commonpf64}\{#AppName}') + '.';
    Exit;
  end;
  // Runs before CloseApplications, [InstallDelete], and [Files], including silent updates.
  Result := StopInputService;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ExitCode: Integer;
begin
  if CurStep <> ssPostInstall then Exit;
  // A failing [Run] program is not necessarily an installer error. Check its exit code here
  // before showing success or launching the user app.
  if not Exec(ExpandConstant('{app}\{#AppExe}'), '--install-input-service',
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ExitCode) then
    RaiseException(ServiceError('--install-input-service', ExitCode));
  if ExitCode <> 0 then
    RaiseException(ServiceError('--install-input-service', ExitCode));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Error: String;
  State: LongWord;
  Attempt, ExitCode: Integer;
begin
  if CurUninstallStep <> usUninstall then Exit;
  // Abort on failure while the executable and its dependencies are still present.
  // usUninstall precedes [UninstallRun], so taskkill cannot kill this command.
  Error := StopInputService;
  if Error <> '' then RaiseException(Error);
  Error := ReadInputServiceState(State);
  if Error <> '' then RaiseException(Error);
  if State = 0 then Exit;
  if not Exec(ExpandConstant('{app}\{#AppExe}'), '--remove-input-service',
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ExitCode) then
    RaiseException(ServiceError('--remove-input-service', ExitCode));
  if ExitCode <> 0 then
    RaiseException(ServiceError('--remove-input-service', ExitCode));
  for Attempt := 0 to ServiceWaitAttempts do begin
    Error := ReadInputServiceState(State);
    if Error <> '' then RaiseException(Error);
    if State = 0 then Exit;
    if Attempt = ServiceWaitAttempts then Break;
    Sleep(500);
  end;
  RaiseException('Не удалось удалить ' + InputServiceName +
    ' за 60 секунд. Файлы сохранены; закройте оснастку служб или перезагрузите ПК и повторите удаление.');
end;

function ShouldLaunchApp: Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/NOLAUNCH') = 0 then Exit;
  Result := True;
end;

function ShouldLaunchSilentApp: Boolean;
begin
  Result := WizardSilent and ShouldLaunchApp;
end;
