; ChatHub Installer Script for Inno Setup
;
; To build the installer:
; 1. Install Inno Setup from https://jrsoftware.org/isinfo.php
; 2. Open this file in Inno Setup Compiler
; 3. Click Build > Compile
;
; Prerequisites:
; - Run 'pyinstaller chathub.spec' first to create dist/ChatHub folder

#define MyAppName "ChatHub"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "ChatHub"
#define MyAppURL "https://github.com/tonyyang0504/chathub"
#define MyAppExeName "ChatHub.exe"

[Setup]
; Application info
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}

; Installation directories
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes

; Output settings
OutputDir=..\dist\installer
OutputBaseFilename=ChatHub_Setup_{#MyAppVersion}
SetupIconFile=..\static\favicon.ico

; Compression
Compression=lzma2
SolidCompression=yes

; Privileges (don't require admin if possible)
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; UI settings
WizardStyle=modern
WizardImageFile=compiler:WizModernImage.bmp
WizardSmallImageFile=compiler:WizModernSmallImage.bmp

; Uninstaller
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startupicon"; Description: "Start ChatHub when Windows starts"; GroupDescription: "Startup Options:"; Flags: unchecked

[Files]
; Main application files from PyInstaller dist folder
Source: "..\dist\ChatHub\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Start Menu
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"

; Desktop icon (optional)
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

; Startup (optional)
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startupicon

[Run]
; Run after installation
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean up data directory on uninstall (optional - commented out to preserve user data)
; Type: filesandordirs; Name: "{userappdata}\ChatHub"

[Code]
// Custom code for installation

// Check if application is running before uninstall
function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;

  // Try to stop the application gracefully
  if FileExists(ExpandConstant('{app}\{#MyAppExeName}')) then
  begin
    Exec('taskkill.exe', '/F /IM {#MyAppExeName}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;

// Show welcome message
procedure InitializeWizard();
begin
  // Custom initialization if needed
end;

// Create data directory after installation
procedure CurStepChanged(CurStep: TSetupStep);
var
  DataDir: String;
begin
  if CurStep = ssPostInstall then
  begin
    DataDir := ExpandConstant('{userappdata}\ChatHub');
    if not DirExists(DataDir) then
    begin
      CreateDir(DataDir);
      CreateDir(DataDir + '\sessions');
      CreateDir(DataDir + '\logs');
    end;
  end;
end;
