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
  QuitEvent = 'dictate_py_quit_request';        { leetsay closes itself when this is set }
  RunningMutex = 'dictate_py_single_instance';
  EVENT_MODIFY_STATE = $0002;

var
  RemoveChoice: Integer;                        { 2 = remove everything, else keep }

{ Plain numbers for Windows' BOOL (0 = no), the safest for these calls. }
function OpenEventW(Access: Cardinal; Inherit: Cardinal; Name: String): THandle;
  external 'OpenEventW@kernel32.dll stdcall';
function SetEvent(Handle: THandle): Cardinal;
  external 'SetEvent@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): Cardinal;
  external 'CloseHandle@kernel32.dll stdcall';

{ A running leetsay is asked to close (the same signal a newer copy sends),
  and given up to 5 seconds, so nobody has to close it by hand first. }
procedure AskLeetsayToClose();
var
  Handle: THandle;
  I: Integer;
begin
  if not CheckForMutexes(RunningMutex) then
    Exit;
  Handle := OpenEventW(EVENT_MODIFY_STATE, 0, QuitEvent);
  if Handle <> 0 then
  begin
    SetEvent(Handle);
    CloseHandle(Handle);
  end;
  for I := 1 to 50 do
  begin
    if not CheckForMutexes(RunningMutex) then
      Break;
    Sleep(100);
  end;
end;

function InitializeSetup(): Boolean;
begin
  AskLeetsayToClose();          { installing a new version over a running one }
  Result := True;
end;

{ Asked as uninstalling starts (owner: asked at the end, it looked as if it
  was still installed): keep the settings (and with them the downloaded speech
  model; the default), or remove everything. 0 or 2. Closing the window keeps
  everything. }
function AskWhatToRemove(): Integer;
var
  Form: TSetupForm;
  Info: TNewStaticText;
  KeepAll, Everything: TNewRadioButton;
  OkButton: TNewButton;
begin
  Result := 0;
  { Inno Setup 6.6+: the size is given here (scaled by it), and fixed. }
  Form := CreateCustomForm(420, 160, True, True);
  try
    Form.Caption := 'Uninstall';

    Info := TNewStaticText.Create(Form);
    Info.Parent := Form;
    Info.Left := ScaleX(18);
    Info.Top := ScaleY(16);
    Info.Width := Form.ClientWidth - ScaleX(36);
    Info.AutoSize := False;
    Info.WordWrap := True;
    Info.Height := ScaleY(34);
    Info.Caption := '{#AppName} will be removed. What should happen to your settings?';

    KeepAll := TNewRadioButton.Create(Form);
    KeepAll.Parent := Form;
    KeepAll.Left := ScaleX(18);
    KeepAll.Top := ScaleY(60);
    KeepAll.Width := Form.ClientWidth - ScaleX(36);
    KeepAll.Caption := 'Keep my settings';
    KeepAll.Checked := True;

    Everything := TNewRadioButton.Create(Form);
    Everything.Parent := Form;
    Everything.Left := ScaleX(18);
    Everything.Top := ScaleY(86);
    Everything.Width := Form.ClientWidth - ScaleX(36);
    Everything.Caption := 'Remove everything';

    OkButton := TNewButton.Create(Form);
    OkButton.Parent := Form;
    OkButton.Caption := 'OK';
    OkButton.Width := ScaleX(84);
    OkButton.Height := ScaleY(26);
    OkButton.Left := Form.ClientWidth - OkButton.Width - ScaleX(16);
    OkButton.Top := Form.ClientHeight - OkButton.Height - ScaleY(14);
    OkButton.ModalResult := mrOk;
    OkButton.Default := True;

    Form.ActiveControl := KeepAll;
    if Form.ShowModal() = mrOk then
    begin
      if Everything.Checked then
        Result := 2;
    end;
  finally
    Form.Free();
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usAppMutexCheck then
    AskLeetsayToClose();        { just before Uninstall checks whether it runs }
  if CurUninstallStep = usUninstall then
  begin
    { Asked first, acted on once the program is gone (below). }
    RemoveChoice := 0;
    if not UninstallSilent() then
      RemoveChoice := AskWhatToRemove();
    { The program is going: so does its "Start with Windows" entry. }
    RegDeleteValue(HKEY_CURRENT_USER, RunKey, '{#AppName}');
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    if RemoveChoice = 2 then    { settings, log and the downloaded speech models }
      DelTree(ExpandConstant('{localappdata}\{#AppName}'), True, True, True);
  end;
end;
