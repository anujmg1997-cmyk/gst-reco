; Inno Setup script - builds GSTReconX-Setup-<version>.exe from the PyInstaller output in dist\GSTReconX
; Build: iscc /DMyAppVersion=1.0.0 installer\GSTReco.iss

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppName "GST ReconX"
#define MyAppExe "GSTReconX.exe"
#define MyPublisher "Total Strategic Solutions Pvt. Ltd."

[Setup]
; Keep AppId the same forever - it is how updates find the existing installation.
AppId={{8F3C2A71-5B4E-4D2A-9C1F-6E7B3A2D9F10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyPublisher}
AppCopyright=Copyright (C) {#MyPublisher}
VersionInfoCompany={#MyPublisher}
VersionInfoVersion={#MyAppVersion}
; Per-user install: no admin rights needed, so updates install silently.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\GSTReco
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputDir=..\dist\installer
OutputBaseFilename=GSTReconX-Setup-{#MyAppVersion}
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
; v1.0 was called "GST Reco" with GSTReco.exe - remove the old program file and shortcuts
Type: files; Name: "{app}\GSTReco.exe"
Type: files; Name: "{autoprograms}\GST Reco.lnk"
Type: files; Name: "{autodesktop}\GST Reco.lnk"

[Files]
Source: "..\dist\GSTReconX\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Run]
; Runs after a normal install (checkbox) and also after a silent in-app update (re-opens the app).
Filename: "{app}\{#MyAppExe}"; Description: "Open {#MyAppName}"; Flags: nowait postinstall
