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
Source: "{#PayloadRoot}\*"; DestDir: "{tmp}\OliviaPayload"; Flags: recursesubdirs createallsubdirs dontcopy noencryption ignoreversion

[Code]
var
  InstallDirPage: TInputDirWizardPage;
  OfficialDirPage: TInputQueryWizardPage;
  OfficialBrowseButton: TNewButton;
  StableInstallCode: String;
  SetupResultPath: String;

procedure OfficialBrowseButtonClick(Sender: TObject);
var
  Selected: String;
begin
  Selected := OfficialDirPage.Values[0];
  if BrowseForFolder('选择正版 Steam 游戏目录', Selected, False) then
    OfficialDirPage.Values[0] := Selected;
end;

function IsStableErrorCode(const Value: String): Boolean;
var
  Index: Integer;
begin
  Result := (Length(Value) >= 4) and (Length(Value) <= 96) and
    (Value[1] >= 'A') and (Value[1] <= 'Z') and (Pos('_', Value) > 0);
  if not Result then
    Exit;
  for Index := 1 to Length(Value) do
    if not (((Value[Index] >= 'A') and (Value[Index] <= 'Z')) or
      ((Value[Index] >= '0') and (Value[Index] <= '9')) or
      (Value[Index] = '_')) then
    begin
      Result := False;
      Exit;
    end;
end;

function LoadStableInstallCode(const ResultPath: String): String;
var
  Content: AnsiString;
  Prefix: String;
  Candidate: String;
begin
  Result := '';
  Prefix := 'OLIVIA_SETUP_ERROR=';
  if LoadStringFromFile(ResultPath, Content) then
  begin
    Candidate := Copy(String(Content), Length(Prefix) + 1, MaxInt);
    if (Copy(String(Content), 1, Length(Prefix)) = Prefix) and
      IsStableErrorCode(Candidate) then
      Result := Candidate;
  end;
end;

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

  OfficialDirPage := CreateInputQueryPage(
    InstallDirPage.ID,
    '选择正版游戏目录',
    '可选择 Steam 中 Olivia 的正版安装目录。',
    '留空时安装器会按 Steam AppID 自动发现；不会写入正版目录。'
  );
  OfficialDirPage.Add('正版游戏目录（可留空）：', False);
  OfficialDirPage.Values[0] := ExpandConstant('{param:OfficialRoot|}');

  OfficialBrowseButton := TNewButton.Create(OfficialDirPage);
  OfficialBrowseButton.Parent := OfficialDirPage.Surface;
  OfficialBrowseButton.Caption := '浏览…';
  OfficialBrowseButton.Width := ScaleX(80);
  OfficialBrowseButton.Height := OfficialDirPage.Edits[0].Height;
  OfficialBrowseButton.Left := OfficialDirPage.Edits[0].Left +
    OfficialDirPage.Edits[0].Width - OfficialBrowseButton.Width;
  OfficialBrowseButton.Top := OfficialDirPage.Edits[0].Top;
  OfficialBrowseButton.OnClick := @OfficialBrowseButtonClick;
  OfficialDirPage.Edits[0].Width := OfficialDirPage.Edits[0].Width -
    OfficialBrowseButton.Width - ScaleX(8);
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

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  PowerShell: String;
  Params: String;
  ExitCode: Integer;
begin
  Result := '';
  StableInstallCode := '';
  SetupResultPath := ExpandConstant('{tmp}\olivia-setup-result.txt');
  DeleteFile(SetupResultPath);
  ExitCode := -1;
  try
    ExtractTemporaryFiles('{tmp}\OliviaPayload\*');
  except
    StableInstallCode := 'SETUP_PAYLOAD_EXTRACT_FAILED';
    Log('Olivia installer code: ' + StableInstallCode);
    Result := '安装失败：' + StableInstallCode + '。请保留安装日志后重试。';
    Exit;
  end;

  WizardForm.StatusLabel.Caption := '正在校验并安装离线核心组件…';
  PowerShell := ExpandConstant('{sysnative}\WindowsPowerShell\v1.0\powershell.exe');
  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' +
    AddQuotes(ExpandConstant('{tmp}\OliviaPayload\installer\Install.ps1')) +
    ' -PayloadRoot ' + AddQuotes(ExpandConstant('{tmp}\OliviaPayload')) +
    ' -Destination ' + AddQuotes(InstallDirPage.Values[0]) +
    ' -OfflineAssetsRoot ' + AddQuotes(ExpandConstant('{tmp}\OliviaPayload\offline')) +
    ' -SetupResultPath ' + AddQuotes(SetupResultPath) +
    ' -NonInteractive';
  if Trim(OfficialDirPage.Values[0]) <> '' then
    Params := Params + ' -OfficialRoot ' + AddQuotes(OfficialDirPage.Values[0]);

  if (not Exec(
       PowerShell,
       Params,
       '',
       SW_HIDE,
       ewWaitUntilTerminated,
       ExitCode
     )) or
     (ExitCode <> 0) then
  begin
    StableInstallCode := LoadStableInstallCode(SetupResultPath);
    if StableInstallCode = '' then
      StableInstallCode := 'SETUP_INSTALL_FAILED';
    Log('Olivia installer code: ' + StableInstallCode);
    if StableInstallCode <> '' then
      Result := '安装失败：' + StableInstallCode + '。请保留安装日志后重试。'
    else
      Result := Format('安装失败（进程错误码 %d）。请保留安装日志后重试。', [ExitCode]);
  end;
end;
