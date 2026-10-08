; Inno Setup Script for Z Mic Studio
; Produced for Zameer - Version 0.9.3

#define MyAppName "Z Mic Studio"
#define MyAppVersion "0.9.3"
#define MyAppPublisher "Zameer"
#define MyAppURL "https://github.com/zameersts/Z-Mic-Studio-Releases"
#define MyAppExeName "ZMicStudio.exe"
#define SourceDir "D:\Z Mic\ZMicStudio"

[Setup]
AppId={{E589417E-10C4-4B2E-8FA2-2D5392D6B217}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={commonpf}\{#MyAppName}
UsePreviousAppDir=no
DisableProgramGroupPage=yes
LicenseFile=d:\PhoneMic\windows\installer_license.txt
PrivilegesRequired=admin
OutputDir=D:\Z Mic
OutputBaseFilename=ZMicStudioSetup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
