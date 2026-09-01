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
DefaultDirName={localappdata}\BSideOliviaLocal
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
SetupIconFile={#PayloadRoot}\installer\assets\olivia.ico
DisableProgramGroupPage=yes
SetupLogging=yes

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#PayloadRoot}\*"; Excludes: "offline\video-runtime\*"; DestDir: "{tmp}\OliviaPayload"; Flags: recursesubdirs createallsubdirs dontcopy noencryption ignoreversion

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加选项："

[Icons]
Name: "{userprograms}\Olivia 本地版"; Filename: "{sys}\wscript.exe"; Parameters: "//B //Nologo ""{code:GetInstallRoot}\install\START.vbs"""; WorkingDir: "{code:GetInstallRoot}\install"; IconFilename: "{code:GetInstallRoot}\install\local_backend\installer\assets\olivia.ico"
Name: "{userdesktop}\Olivia 本地版"; Filename: "{sys}\wscript.exe"; Parameters: "//B //Nologo ""{code:GetInstallRoot}\install\START.vbs"""; WorkingDir: "{code:GetInstallRoot}\install"; IconFilename: "{code:GetInstallRoot}\install\local_backend\installer\assets\olivia.ico"; Tasks: desktopicon

[Run]
Filename: "{sys}\wscript.exe"; Parameters: "//B //Nologo ""{code:GetInstallRoot}\install\START.vbs"""; Description: "立即启动 Olivia"; WorkingDir: "{code:GetInstallRoot}\install"; Flags: postinstall nowait skipifsilent

[Code]
var
  InstallDirPage: TInputDirWizardPage;
  OfficialDirPage: TInputQueryWizardPage;
  OfficialBrowseButton: TNewButton;
  InstallProgressPage: TOutputProgressWizardPage;
  StableInstallCode: String;
  SetupResultPath: String;
  LastInstallPhase: String;

function InstallPhaseCaption(const Phase: String): String;
begin
  if Phase = 'PREPARE' then
    Result := '正在准备安装…'
  else if Phase = 'VERIFY_OFFICIAL' then
    Result := '正在校验正版游戏文件…'
  else if Phase = 'VERIFY_CORE' then
    Result := '正在校验本地核心组件…'
  else if Phase = 'INSTALL_CORE' then
    Result := '正在安装本地运行环境…'
  else if Phase = 'INSTALL_PATCH' then
    Result := '正在安装 Olivia 本地补丁…'
  else if Phase = 'COPY_VIDEO_RUNTIME' then
    Result := '正在复制视频运行包…'
  else if Phase = 'VERIFY_VIDEO_OFFLINE' then
    Result := '正在校验视频离线组件…'
  else if Phase = 'INSTALL_ORDINARY_VIDEO' then
    Result := '正在安装说话与口型基础组件…'
  else if Phase = 'INSTALL_MUSIC_VIDEO' then
    Result := '正在安装音乐生成与合成组件…'
  else if Phase = 'EXTRACT_VIDEO_RUNTIME' then
    Result := '正在解压视频运行环境…'
  else if Phase = 'VERIFY_VIDEO_RUNTIME' then
    Result := '正在校验视频运行环境…'
  else if Phase = 'TEST_VIDEO_RUNTIME' then
    Result := '正在检测视频运行环境…'
  else if Phase = 'FINALIZE' then
    Result := '正在完成安装…'
  else
    Result := '';
end;

procedure InstallOutputLine(const S: String; const Error, FirstLine: Boolean);
var
  Prefix: String;
  Payload: String;
  Remaining: String;
  Phase: String;
  CurrentText: String;
  TotalText: String;
  Caption: String;
  Detail: String;
  Separator: Integer;
  CurrentBytes: Int64;
  TotalBytes: Int64;
  Position: Longint;
begin
  if Error then
  begin
    Log('Olivia installer progress stream unavailable.');
    Exit;
  end;

  Prefix := 'OLIVIA_SETUP_PROGRESS=';
  if Copy(S, 1, Length(Prefix)) <> Prefix then
    Exit;
  Payload := Copy(S, Length(Prefix) + 1, MaxInt);
  Separator := Pos('|', Payload);
  if Separator <= 1 then
    Exit;
  Phase := Copy(Payload, 1, Separator - 1);
  Remaining := Copy(Payload, Separator + 1, MaxInt);
  Separator := Pos('|', Remaining);
  if Separator <= 1 then
    Exit;
  CurrentText := Copy(Remaining, 1, Separator - 1);
  TotalText := Copy(Remaining, Separator + 1, MaxInt);
  if (TotalText = '') or (Pos('|', TotalText) > 0) then
    Exit;
  Caption := InstallPhaseCaption(Phase);
  if Caption = '' then
    Exit;
  CurrentBytes := StrToInt64Def(CurrentText, -1);
  TotalBytes := StrToInt64Def(TotalText, -1);
  if (CurrentBytes < 0) or (TotalBytes < 0) or
    ((TotalBytes > 0) and (CurrentBytes > TotalBytes)) then
    Exit;

  if Phase <> LastInstallPhase then
  begin
    Log('Olivia installer phase: ' + Phase);
    LastInstallPhase := Phase;
  end;
  if TotalBytes > 0 then
  begin
    Position := (CurrentBytes * 10000) div TotalBytes;
    InstallProgressPage.SetProgress(Position, 10000);
    Detail := '已处理 ' + IntToStr(CurrentBytes div 1048576) +
      ' MiB / ' + IntToStr(TotalBytes div 1048576) + ' MiB';
  end
  else
  begin
    InstallProgressPage.SetProgress(0, 1);
    Detail := '请保持窗口开启。';
  end;
  InstallProgressPage.SetText(Caption, Detail);
end;

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
  InstallProgressPage := CreateOutputProgressPage(
    '正在安装 Olivia 本地版',
    '进度来自安装程序实际处理量。'
  );
  LastInstallPhase := '';

  InstallDirPage := CreateInputDirPage(
    wpSelectDir,
    '选择产品目录',
    'Olivia 本地版将在产品目录内分别管理客户端与运行环境。',
    '建议保留默认位置；升级补丁会继续使用这里的受管安装。',
    False,
    ''
  );
  InstallDirPage.Add('产品目录：');
  InstallDirPage.Values[0] := ExpandConstant(
    '{param:InstallRoot|{localappdata}\BSideOliviaLocal}'
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
    MsgBox('请选择产品目录。', mbError, MB_OK);
    Result := False;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  PowerShell: String;
  Params: String;
  ExitCode: Integer;
  ExecSucceeded: Boolean;
  DiagnosticContent: AnsiString;
begin
  Result := '';
  StableInstallCode := '';
  SetupResultPath := ExpandConstant('{tmp}\olivia-setup-result.txt');
  DeleteFile(SetupResultPath);
  DeleteFile(SetupResultPath + '.diagnostic.json');
  ExitCode := -1;
  InstallProgressPage.SetText('正在解包安装文件…', '请保持窗口开启。');
  InstallProgressPage.SetProgress(0, 1);
  InstallProgressPage.Show;
  try
    try
      ExtractTemporaryFiles('{tmp}\OliviaPayload\*');
    except
      StableInstallCode := 'SETUP_PAYLOAD_EXTRACT_FAILED';
      Log('Olivia installer code: ' + StableInstallCode);
      Result := '安装失败：' + StableInstallCode + '。请保留安装日志后重试。';
      Exit;
    end;

    PowerShell := ExpandConstant('{sysnative}\WindowsPowerShell\v1.0\powershell.exe');
    Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' +
      AddQuotes(ExpandConstant('{tmp}\OliviaPayload\installer\Install.ps1')) +
      ' -PayloadRoot ' + AddQuotes(ExpandConstant('{tmp}\OliviaPayload')) +
      ' -Destination ' + AddQuotes(InstallDirPage.Values[0]) +
      ' -OfflineAssetsRoot ' + AddQuotes(ExpandConstant('{tmp}\OliviaPayload\offline')) +
      ' -SetupResultPath ' + AddQuotes(SetupResultPath) +
      ' -NonInteractive' +
      ' -SkipShortcut';
#ifdef PrivatePayload
    Params := Params +
      ' -VideoRuntimePath ' + AddQuotes(
        ExpandConstant('{src}\Olivia-video-runtime-private.zip')
      ) +
      ' -VideoOfflineRoot ' + AddQuotes(
        ExpandConstant('{src}\Olivia-video-offline-private')
      );
#endif
    if Trim(OfficialDirPage.Values[0]) <> '' then
      Params := Params + ' -OfficialRoot ' + AddQuotes(OfficialDirPage.Values[0]);

    try
      ExecSucceeded := ExecAndLogOutput(
        PowerShell,
        Params,
        '',
        SW_HIDE,
        ewWaitUntilTerminated,
        ExitCode,
        @InstallOutputLine
      );
    except
      ExecSucceeded := False;
      ExitCode := -1;
      Log('Olivia installer progress capture failed.');
    end;
    if (not ExecSucceeded) or (ExitCode <> 0) then
    begin
      StableInstallCode := LoadStableInstallCode(SetupResultPath);
      if StableInstallCode = '' then
        StableInstallCode := 'SETUP_INSTALL_FAILED';
      Log('Olivia installer code: ' + StableInstallCode);
      if LoadStringFromFile(SetupResultPath + '.diagnostic.json', DiagnosticContent) then
        Log('Olivia installer diagnostic: ' + String(DiagnosticContent));
      if StableInstallCode = 'OFFICIAL_INSTALL_AMBIGUOUS' then
        Result := '检测到多个 Olivia 正版目录，且无法自动确认当前副本。请点击“上一步”，明确选择 Steam 中正在使用的正版游戏目录。'
      else if StableInstallCode <> '' then
        Result := '安装失败：' + StableInstallCode + '。请保留安装日志后重试。'
      else
        Result := Format('安装失败（进程错误码 %d）。请保留安装日志后重试。', [ExitCode]);
    end;
  finally
    InstallProgressPage.Hide;
  end;
end;
