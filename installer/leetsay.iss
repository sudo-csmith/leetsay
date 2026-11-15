; Inno Setup script for the leetsay installer.
; Build (after PyInstaller has made dist\leetsay):
;   iscc /DAppVersion=0.1.0 installer\leetsay.iss
; Installs for the current Windows user only: no administrator rights needed.

#define AppName "leetsay"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F1C2A44-7E1B-4C57-9E43-1337C0FFEE01}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=sudo-csmith
AppPublisherURL=https://github.com/sudo-csmith/leetsay
AppSupportURL=https://github.com/sudo-csmith/leetsay
DefaultDirName={localappdata}\Programs\{#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\Output
OutputBaseFilename={#AppName}-setup-{#AppVersion}
SetupIconFile=..\leetsay.ico
UninstallDisplayIcon={app}\{#AppName}.exe
UninstallDisplayName={#AppName}
; The disclaimer and the MIT licence: installing needs "I accept" on this page.
LicenseFile=terms.txt
Compression=lzma2/max
SolidCompression=yes
; Dark, like the app. Needs Inno Setup 6.6 or newer.
WizardStyle=modern dark
WizardImageAlphaFormat=defined
WizardSmallImageFile=wizard-small-58.bmp,wizard-small-77.bmp,wizard-small-97.bmp,wizard-small-116.bmp,wizard-small-124.bmp,wizard-small-143.bmp,wizard-small-159.bmp
WizardImageFile=wizard-large-202.bmp,wizard-large-269.bmp,wizard-large-336.bmp,wizard-large-403.bmp,wizard-large-430.bmp,wizard-large-498.bmp,wizard-large-534.bmp
; A running copy is asked to close before files are replaced or removed.
AppMutex=dictate_py_single_instance
CloseApplications=yes

; No choices here: "Start with Windows" is chosen in the app's introduction
; (and in Settings). The uninstaller removes that entry again.

[Files]
Source: "..\dist\leetsay\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\PRIVACY.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppName}.exe"

[Run]
Filename: "{app}\{#AppName}.exe"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';

function HubFolder(): String;
begin
  { Where the download library keeps models: the same rules leetsay uses. }
  Result := GetEnv('HF_HUB_CACHE');
  if Result = '' then
  begin
    Result := GetEnv('HF_HOME');
    if Result = '' then
      Result := ExpandConstant('{%USERPROFILE}') + '\.cache\huggingface';
    Result := Result + '\hub';
  end;
end;

procedure RemoveLeetsayData();
var
  Hub: String;
begin
  { Settings, log and any model downloaded by hand. }
  DelTree(ExpandConstant('{localappdata}\{#AppName}'), True, True, True);
  { Only the exact model folders leetsay downloads: never anything else. }
  Hub := HubFolder();
  DelTree(Hub + '\models--istupakov--parakeet-tdt-0.6b-v2-onnx', True, True, True);
  DelTree(Hub + '\models--istupakov--parakeet-tdt-0.6b-v3-onnx', True, True, True);
  DelTree(Hub + '\models--istupakov--silero-vad-onnx', True, True, True);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    { The program is going: so does its "Start with Windows" entry. }
    RegDeleteValue(HKEY_CURRENT_USER, RunKey, '{#AppName}');
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    if UninstallSilent() then
      Exit;                     { silent: keep settings and the model }
    if MsgBox('Also remove your {#AppName} settings and the downloaded speech model '
              + '(about 630 MB)?' + #13#10#13#10
              + 'Choose No to keep them, for example if you will install '
              + '{#AppName} again.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      RemoveLeetsayData();
  end;
end;
