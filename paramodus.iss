; paramodus.iss — Inno Setup script for Paramodus
; ============================================================================
; Produces a single ParamodusSetup.exe installer that:
;   - Installs the full dist/Paramodus/ folder to Program Files
;   - Creates a Desktop shortcut and Start Menu entry
;   - Registers an uninstaller in Windows Settings
;
; Prerequisites:
;   1. Build the app first:  python build.py
;   2. Install Inno Setup 6: https://jrsoftware.org/isdl.php
;   3. Open this file in Inno Setup Compiler and press F9,
;      OR run:  iscc paramodus.iss
;
; Output: installer\ParamodusSetup.exe
; ============================================================================

#define AppName     "Paramodus"
#define AppVersion  "1.0"
#define AppPublisher "Centrale73"
#define AppURL      "https://github.com/Centrale73/Paramodus"
#define AppExeName  "Paramodus.exe"

[Setup]
AppId={{A3F2B1C4-7E8D-4F9A-B0C2-D3E4F5A6B7C8}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes
OutputDir=installer
OutputBaseFilename=ParamodusSetup
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
; Source path is relative to the .iss file location (repo root).
; Forward slashes work fine in Inno Setup — avoids backslash parsing issues.
; Recursively bundles everything inside dist/Paramodus/ including _internal/
; (DLLs, Python libs, and the bundled Bonsai-8B.gguf inside _internal/models/).
Source: "dist\Paramodus\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}";           Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";     Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only remove the install dir if it ends up empty after uninstall.
; User data in %USERPROFILE%\.myapp is intentionally preserved.
Type: dirifempty; Name: "{app}"
