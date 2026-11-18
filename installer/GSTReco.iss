; Inno Setup script - builds GSTReco-Setup-<version>.exe from the PyInstaller output in dist\GSTReco
; Build: iscc /DMyAppVersion=1.0.0 installer\GSTReco.iss

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppName "GST Reco"
#define MyAppExe "GSTReco.exe"

[Setup]
; Keep AppId the same forever - it is how updates find the existing installation.
AppId={{8F3C2A71-5B4E-4D2A-9C1F-6E7B3A2D9F10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=GST Reco
VersionInfoVersion={#MyAppVersion}
; Per-user install: no admin rights needed, so updates install silently.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\GSTReco
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputDir=..\dist\installer
OutputBaseFilename=GSTReco-Setup-{#MyAppVersion}
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\{#MyAppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[InstallDelete]
; remove files from the previous version before copying the new one
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\GSTReco\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Run]
; Runs after a normal install (checkbox) and also after a silent in-app update (re-opens the app).
Filename: "{app}\{#MyAppExe}"; Description: "Open {#MyAppName}"; Flags: nowait postinstall
