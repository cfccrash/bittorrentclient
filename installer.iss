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

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#MyAppName}"""; Flags: runhidden; RunOnceId: "RemoveFirewallRule"; Check: IsAdminInstallMode

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // The app's own "start with Windows" setting writes the same Run value; make sure it is gone.
  if CurUninstallStep = usPostUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#MyAppId}');
end;

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
