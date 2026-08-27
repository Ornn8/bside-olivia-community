#ifndef PayloadRoot
  #error PayloadRoot is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
#ifndef AppVersion
  #error AppVersion is required
#endif

[Setup]
AppId={{E7B6A4B1-2AC0-4B1E-85DA-E17E66D6EF0A}
AppName=Olivia 本地版
AppVersion={#AppVersion}
AppPublisher=BSide Olivia Community
DefaultDirName={localappdata}\BSideOliviaLocal\install
CreateAppDir=no
Uninstallable=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=Olivia-Setup-x64
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
LicenseFile={#PayloadRoot}\LICENSE
DisableProgramGroupPage=yes
SetupLogging=yes

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#PayloadRoot}\*"; DestDir: "{tmp}\OliviaPayload"; Flags: recursesubdirs createallsubdirs deleteafterinstall ignoreversion

[Code]
var
  InstallDirPage: TInputDirWizardPage;
  OfficialDirPage: TInputDirWizardPage;

procedure InitializeWizard;
begin
  InstallDirPage := CreateInputDirPage(
    wpSelectDir,
    '选择安装位置',
    'Olivia 本地版将安装到当前 Windows 用户目录。',
    '建议保留默认位置；升级补丁会继续使用这里的受管安装。',
    False,
    ''
  );
  InstallDirPage.Add('安装位置：');
  InstallDirPage.Values[0] := ExpandConstant(
    '{param:InstallRoot|{localappdata}\BSideOliviaLocal\install}'
  );

  OfficialDirPage := CreateInputDirPage(
    InstallDirPage.ID,
    '选择正版游戏目录',
    '可选择 Steam 中 Olivia 的正版安装目录。',
    '留空时安装器会按 Steam AppID 自动发现；不会写入正版目录。',
    False,
    ''
  );
  OfficialDirPage.Add('正版游戏目录（可留空）：');
  OfficialDirPage.Values[0] := ExpandConstant('{param:OfficialRoot|}');
end;

function GetInstallRoot(Param: String): String;
begin
  Result := InstallDirPage.Values[0];
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = InstallDirPage.ID) and (Trim(InstallDirPage.Values[0]) = '') then
  begin
    MsgBox('请选择安装位置。', mbError, MB_OK);
    Result := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  PowerShell: String;
  Params: String;
  ExitCode: Integer;
begin
  if CurStep <> ssPostInstall then
    Exit;

  WizardForm.StatusLabel.Caption := '正在校验并安装离线核心组件…';
  PowerShell := ExpandConstant('{sysnative}\WindowsPowerShell\v1.0\powershell.exe');
  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' +
    AddQuotes(ExpandConstant('{tmp}\OliviaPayload\installer\Install.ps1')) +
    ' -PayloadRoot ' + AddQuotes(ExpandConstant('{tmp}\OliviaPayload')) +
    ' -Destination ' + AddQuotes(InstallDirPage.Values[0]) +
    ' -OfflineAssetsRoot ' + AddQuotes(ExpandConstant('{tmp}\OliviaPayload\offline')) +
    ' -NonInteractive';
  if Trim(OfficialDirPage.Values[0]) <> '' then
    Params := Params + ' -OfficialRoot ' + AddQuotes(OfficialDirPage.Values[0]);

  if (not Exec(PowerShell, Params, '', SW_HIDE, ewWaitUntilTerminated, ExitCode)) or
     (ExitCode <> 0) then
    RaiseException(Format('安装失败（错误码 %d）。请保留安装日志后重试。', [ExitCode]));
end;
