; Inno Setup script - builds BitTorrentClient-Setup-<version>.exe
; Build with build.bat (it runs PyInstaller first, then this), or:  ISCC installer.iss /DMyAppVersion=1.0.0
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppName "Bit Torrent Client"
#define MyAppExe "BitTorrentClient.exe"
#define MyAppId "BitTorrentClient"

[Setup]
AppId={{499D7583-F547-46BD-912F-20885A641A40}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Installs for the current user without an administrator prompt; the wizard offers "for all users" as an option.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=installer
OutputBaseFilename=BitTorrentClient-Setup-{#MyAppVersion}
SetupIconFile=BitTorrentClient.ico
UninstallDisplayIcon={app}\{#MyAppExe}
UninstallDisplayName={#MyAppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked
Name: "startup"; Description: "Start {#MyAppName} when I sign in to Windows (minimised to the tray)"; Flags: unchecked
Name: "torrentassoc"; Description: "Make {#MyAppName} available for .torrent files"
Name: "magnetassoc"; Description: "Make {#MyAppName} available for magnet links"
Name: "firewall"; Description: "Allow {#MyAppName} through Windows Firewall (recommended)"; Check: IsAdminInstallMode

[InstallDelete]
; Updates replace the whole Python runtime folder instead of copying over it: otherwise files a newer build no
; longer ships stay behind (seen: Tk 8.6 DLLs and _tcl_data/_tk_data from a Python 3.12 build next to Tk 9).
; Nothing of the user's lives here - settings and torrents are in %APPDATA%\BitTorrentClient.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "dist\BitTorrentClient\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Registry]
; .torrent files
Root: HKA; Subkey: "Software\Classes\.torrent\OpenWithProgids"; ValueType: string; ValueName: "{#MyAppId}.torrent"; ValueData: ""; Flags: uninsdeletevalue; Tasks: torrentassoc
Root: HKA; Subkey: "Software\Classes\{#MyAppId}.torrent"; ValueType: string; ValueData: "Torrent file"; Flags: uninsdeletekey; Tasks: torrentassoc
Root: HKA; Subkey: "Software\Classes\{#MyAppId}.torrent\DefaultIcon"; ValueType: string; ValueData: "{app}\{#MyAppExe},0"; Tasks: torrentassoc
Root: HKA; Subkey: "Software\Classes\{#MyAppId}.torrent\shell\open\command"; ValueType: string; ValueData: """{app}\{#MyAppExe}"" ""%1"""; Tasks: torrentassoc
; magnet links
Root: HKA; Subkey: "Software\Classes\{#MyAppId}.magnet"; ValueType: string; ValueData: "URL:Magnet Link"; Flags: uninsdeletekey; Tasks: magnetassoc
Root: HKA; Subkey: "Software\Classes\{#MyAppId}.magnet"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""; Tasks: magnetassoc
Root: HKA; Subkey: "Software\Classes\{#MyAppId}.magnet\DefaultIcon"; ValueType: string; ValueData: "{app}\{#MyAppExe},0"; Tasks: magnetassoc
Root: HKA; Subkey: "Software\Classes\{#MyAppId}.magnet\shell\open\command"; ValueType: string; ValueData: """{app}\{#MyAppExe}"" ""%1"""; Tasks: magnetassoc
; lists the app under Settings > Apps > Default apps
Root: HKA; Subkey: "Software\{#MyAppId}\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "{#MyAppName}"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\{#MyAppId}\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "A BitTorrent client"
Root: HKA; Subkey: "Software\{#MyAppId}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".torrent"; ValueData: "{#MyAppId}.torrent"; Tasks: torrentassoc
Root: HKA; Subkey: "Software\{#MyAppId}\Capabilities\URLAssociations"; ValueType: string; ValueName: "magnet"; ValueData: "{#MyAppId}.magnet"; Tasks: magnetassoc
Root: HKA; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "{#MyAppId}"; ValueData: "Software\{#MyAppId}\Capabilities"; Flags: uninsdeletevalue
; start with Windows (same value the app's own Settings checkbox uses)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppId}"; ValueData: """{app}\{#MyAppExe}"" --minimized"; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#MyAppName}"""; Flags: runhidden; Tasks: firewall; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#MyAppName}"" dir=in action=allow program=""{app}\{#MyAppExe}"" enable=yes profile=any"; Flags: runhidden; Tasks: firewall; Check: IsAdminInstallMode
Filename: "ms-settings:defaultapps"; Description: "Choose {#MyAppName} as the default app for .torrent files and magnet links"; Flags: shellexec postinstall skipifsilent unchecked
Filename: "{app}\{#MyAppExe}"; Description: "Start {#MyAppName}"; Flags: nowait postinstall skipifsilent
; automatic update from inside the app (updater.py passes /RELAUNCH=1): start the new version again, as the user, not elevated
Filename: "{app}\{#MyAppExe}"; Flags: nowait runasoriginaluser; Check: ShouldRelaunch

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#MyAppName}"""; Flags: runhidden; RunOnceId: "RemoveFirewallRule"; Check: IsAdminInstallMode

[Code]
// The app hides to the tray when Windows asks it to close, so Setup's own "close applications" step cannot stop it.
// Before installing or uninstalling: ask it to quit (BitTorrentClient.exe --quit saves and exits, 1.9.16+), wait,
// and only force it if it is still there (older versions ignore --quit; a stuck copy).

function AppRunning(): Boolean;
var
  Code: Integer;
begin
  // `find` exits 0 when the program is in the task list
  Result := Exec(ExpandConstant('{cmd}'), '/C tasklist /FI "IMAGENAME eq {#MyAppExe}" /NH | find /I "{#MyAppExe}" >nul',
                 '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

procedure CloseRunningApp(Exe: String);
var
  Code, I: Integer;
begin
  if not AppRunning() then
    Exit;
  Log('Bit Torrent Client is running: asking it to quit');
  if FileExists(Exe) then
    Exec(Exe, '--quit', '', SW_HIDE, ewWaitUntilTerminated, Code);
  for I := 1 to 40 do begin            // up to 20 s: it tells the trackers it is stopping, then saves
    if not AppRunning() then
      Exit;
    Sleep(500);
  end;
  // No /T: this installer was started by the app (automatic update), so /T would kill the installer too.
  // /IM already covers detached player windows (same program).
  Log('Still running: stopping it (and any detached player windows)');
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#MyAppExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(700);
end;

function ShouldRelaunch(): Boolean;
begin
  Result := WizardSilent() and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  CloseRunningApp(ExpandConstant('{app}\{#MyAppExe}'));
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  CloseRunningApp(ExpandConstant('{app}\{#MyAppExe}'));
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // The app's own "start with Windows" setting writes the same Run value; make sure it is gone.
  if CurUninstallStep = usPostUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#MyAppId}');
end;

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
