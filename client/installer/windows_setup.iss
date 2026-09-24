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
SetupMutex=BSideOliviaLocalInstaller
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
  InstallProgressPage: TOutputProgressWizardPage;
  StableInstallCode: String;
  SetupResultPath: String;
  LastInstallPhase: String;
  InstallSucceeded: Boolean;
  FailureDetailsButton: TNewButton;
  CloseAndRetryButton: TNewButton;
  CloseRequested: Boolean;

procedure CloseAndRetry(Sender: TObject);
begin
  CloseRequested := True;
  WizardForm.NextButton.OnClick(WizardForm.NextButton);
end;

procedure ShowFailureDetails(Sender: TObject);
begin
  MsgBox('这些信息仅供技术支持定位问题，无需自行处理。' + #13#10 +
    '错误编号：' + StableInstallCode + #13#10 + '发生步骤：' + LastInstallPhase,
    mbInformation, MB_OK);
end;

function RunInstallation: String; forward;

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
var
  PreviousRoot: String;
begin
  InstallProgressPage := CreateOutputProgressPage(
    '正在安装 Olivia 本地版',
    '正在为你准备使用所需的文件，请稍候。'
  );
  LastInstallPhase := '';

  InstallDirPage := CreateInputDirPage(
    wpSelectDir,
    '安装位置',
    '选择 Olivia 的保存位置。',
    '已有安装时请选择原来的位置。程序会识别已有目录，保留信件、记忆和已保存的 Key。',
    False,
    ''
  );
  InstallDirPage.Add('保存到：');
  InstallDirPage.Values[0] := ExpandConstant(
    '{param:InstallRoot|{localappdata}\BSideOliviaLocal}'
  );
  if (ExpandConstant('{param:InstallRoot|}') = '') and
    RegQueryStringValue(HKCU, 'Software\BSideOliviaCommunity', 'ProductRoot', PreviousRoot) then
    if FileExists(AddBackslash(PreviousRoot) + 'install\.olivia-full-patch.json') then
      InstallDirPage.Values[0] := PreviousRoot;
  FailureDetailsButton := TNewButton.Create(WizardForm);
  FailureDetailsButton.Parent := WizardForm.ReadyPage;
  FailureDetailsButton.Left := WizardForm.ReadyMemo.Left;
  FailureDetailsButton.Top := WizardForm.ReadyMemo.Top + WizardForm.ReadyMemo.Height - ScaleY(30);
  FailureDetailsButton.Width := ScaleX(130);
  FailureDetailsButton.Height := ScaleY(26);
  FailureDetailsButton.Caption := '查看技术详情';
  FailureDetailsButton.OnClick := @ShowFailureDetails;
  FailureDetailsButton.Visible := False;
  CloseAndRetryButton := TNewButton.Create(WizardForm);
  CloseAndRetryButton.Parent := WizardForm.ReadyPage;
  CloseAndRetryButton.Left := FailureDetailsButton.Left + FailureDetailsButton.Width + ScaleX(12);
  CloseAndRetryButton.Top := FailureDetailsButton.Top;
  CloseAndRetryButton.Width := ScaleX(180);
  CloseAndRetryButton.Height := FailureDetailsButton.Height;
  CloseAndRetryButton.Caption := '关闭 Olivia 并重试';
  CloseAndRetryButton.OnClick := @CloseAndRetry;
  CloseAndRetryButton.Visible := False;
  WizardForm.ReadyMemo.Height := WizardForm.ReadyMemo.Height - ScaleY(38);
end;

function GetInstallRoot(Param: String): String;
begin
  Result := InstallDirPage.Values[0];
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  FailureMessage: String;
begin
  Result := True;
  if (CurPageID = InstallDirPage.ID) and (Trim(InstallDirPage.Values[0]) = '') then
  begin
    MsgBox('请选择 Olivia 的保存位置。', mbError, MB_OK);
    Result := False;
  end;
  if (CurPageID = wpReady) and not WizardSilent and not InstallSucceeded then
  begin
    FailureDetailsButton.Visible := False;
    CloseAndRetryButton.Visible := False;
    FailureMessage := RunInstallation;
    if FailureMessage <> '' then
    begin
      Result := False;
      WizardForm.PageNameLabel.Caption := '安装未完成';
      WizardForm.PageDescriptionLabel.Caption := '请按下面的说明处理后重试。';
      WizardForm.ReadyLabel.Visible := False;
      WizardForm.ReadyMemo.Text := FailureMessage + #13#10 + #13#10 +
        '处理后点击“重试安装”；更换位置请点击“上一步”。不需要卸载已有程序。';
      WizardForm.NextButton.Caption := '重试安装';
      FailureDetailsButton.Visible := True;
      CloseAndRetryButton.Visible := StableInstallCode = 'INSTALL_APPLICATION_RUNNING';
    end;
  end;
end;

function RunInstallation: String;
var
  PowerShell: String;
  Params: String;
  ExitCode: Integer;
  ExecSucceeded: Boolean;
  DiagnosticContent: AnsiString;
  ResolvedProductRoot: AnsiString;
  CloseThisAttempt: Boolean;
begin
  CloseThisAttempt := CloseRequested;
  CloseRequested := False;
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
      Result := '安装文件未能解压。请确认磁盘有足够空间后重试；如果仍然失败，请重新下载完整安装包。';
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
    if CloseThisAttempt then
      Params := Params + ' -CloseRunningApplication';
#ifdef VideoRuntimePayload
    Params := Params +
      ' -VideoRuntimePath ' + AddQuotes(
        ExpandConstant('{src}\Olivia-video-runtime-private.zip')
      );
#endif
#ifdef VideoOfflinePayload
    Params := Params +
      ' -VideoOfflineRoot ' + AddQuotes(
        ExpandConstant('{src}\Olivia-video-offline-private')
      );
#endif
    if Trim(ExpandConstant('{param:OfficialRoot|}')) <> '' then
      Params := Params + ' -OfficialRoot ' + AddQuotes(ExpandConstant('{param:OfficialRoot|}'));

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
    if ExecSucceeded and (ExitCode = 0) then
    begin
      if LoadStringFromFile(SetupResultPath + '.product-root', ResolvedProductRoot) then
        InstallDirPage.Values[0] := UTF8Decode(ResolvedProductRoot);
      InstallSucceeded := True;
      RegWriteStringValue(HKCU, 'Software\BSideOliviaCommunity', 'ProductRoot', InstallDirPage.Values[0]);
    end;
    if (not ExecSucceeded) or (ExitCode <> 0) then
    begin
      StableInstallCode := LoadStableInstallCode(SetupResultPath);
      if StableInstallCode = '' then
        StableInstallCode := 'SETUP_INSTALL_FAILED';
      Log('Olivia installer code: ' + StableInstallCode);
      if LoadStringFromFile(SetupResultPath + '.diagnostic.json', DiagnosticContent) then
        Log('Olivia installer diagnostic: ' + String(DiagnosticContent));
      if StableInstallCode = 'INSTALL_APPLICATION_RUNNING' then
        Result := 'Olivia 还在运行。请先退出 Olivia，再点击“重试安装”。'
      else if StableInstallCode = 'INSTALL_PROCESS_CHECK_FAILED' then
        Result := '暂时无法确认 Olivia 是否已退出，安装尚未开始。请关闭 Olivia 后重试；如果仍然失败，请通过“查看技术详情”联系支持。'
      else if StableInstallCode = 'OFFLINE_CORE_RUNTIME_PARENT_INVALID' then
        Result := '这个位置不能用于安装。请返回上一步，选择普通文件夹，例如 D:\Olivia；不要直接选择整个磁盘或文件夹快捷链接。'
      else if (Pos('ROLLBACK', StableInstallCode) > 0) or (Pos('TRANSACTION', StableInstallCode) > 0) then
        Result := '上次安装没有正常结束，程序暂时无法自动恢复。请保留现有目录，通过“查看技术详情”联系支持；不要卸载或删除数据。'
      else if StableInstallCode = 'PATCH_PAYLOAD_REFRESH_FAILED' then
        Result := '替换旧版程序文件时失败。请关闭 Olivia，确认安装目录可写后重试；无需重新下载安装包。'
      else if (Pos('HASH', StableInstallCode) > 0) or (Pos('MANIFEST', StableInstallCode) > 0) or
        (Pos('WHEEL', StableInstallCode) > 0) or (Pos('PAYLOAD', StableInstallCode) > 0) then
        Result := '安装包中的部分文件不完整或版本不一致。请重新下载同一版本的完整安装包，再重试。'
      else if StableInstallCode = 'OFFICIAL_INSTALL_AMBIGUOUS' then
        Result := '检测到多个正版副本，但均未匹配支持的版本。请在 Steam 中更新 Olivia 并验证游戏文件完整性，然后重试；安装器会自动重新检测。'
      else if StableInstallCode = 'OFFICIAL_INSTALL_NOT_FOUND' then
        Result := '未自动找到完整的正版游戏文件。请确认 Steam 已安装 Olivia，并在 Steam 中验证游戏文件完整性，然后重试；无需手动选择游戏目录。'
      else if StableInstallCode = 'INSTALL_ROOT_OVERLAPS_OFFICIAL' then
        Result := '本地版安装目录与正版游戏目录重叠。请返回上一步，为本地版选择独立目录，例如 D:\Olivia。'
      else if StableInstallCode = 'OFFICIAL_INSTALL_PATH_REPARSE_POINT' then
        Result := '自动检测到的正版游戏目录经过了链接或重定向，暂不支持此位置。请通过 Steam 的存储管理将游戏移动到普通目录后重试。'
      else if StableInstallCode = 'SETUP_FILE_PERMISSION_DENIED' then
        Result := '无法读取或写入安装所需文件。请确认目录访问权限，并检查安全软件的拦截记录；无需删除现有数据。'
      else if StableInstallCode = 'SETUP_FILE_IN_USE' then
        Result := '安装所需文件正在被占用。请关闭原版游戏及 Olivia 后点击重试；无需重新下载安装包。'
      else if StableInstallCode = 'SETUP_FILE_MISSING' then
        Result := '安装所需文件缺失。请检查正版游戏完整性及安全软件隔离记录；安装日志已记录失败阶段。'
      else if StableInstallCode = 'SETUP_DISK_FULL' then
        Result := '安装盘或临时目录所在盘空间不足。请释放空间后重试；不要删除信件与记忆目录。'
      else if StableInstallCode = 'SETUP_PATH_TOO_LONG' then
        Result := '安装文件路径过长。请返回上一步选择较短的目录，例如 D:\Olivia。'
      else if StableInstallCode = 'SETUP_PATCH_FILE_IN_USE' then
        Result := '安装文件正在被占用。请关闭旧版 Olivia 后重试；不要删除信件和记忆目录。'
      else if StableInstallCode = 'SETUP_PATCH_PERMISSION_DENIED' then
        Result := '安装时无法写入或访问文件。请确认目标目录可写、旧版 Olivia 已关闭，并检查安全软件的拦截记录。请保留安装日志。'
      else if StableInstallCode = 'SETUP_PATCH_DISK_FULL' then
        Result := '安装写入时系统报告空间不足。失败后回滚会释放部分空间，因此现在看到的剩余空间可能已增加。请保留安装日志，核对安装盘及临时目录所在盘。'
      else if StableInstallCode = 'SETUP_PATCH_FILE_MISSING' then
        Result := '安装过程中所需文件不存在。请保留安装日志，并检查原版游戏文件和安全软件的隔离记录。'
      else if StableInstallCode = 'SETUP_PATCH_PATH_TOO_LONG' then
        Result := '复制文件时路径过长。请改用较短的安装目录后重试，例如 D:\Olivia；若仍失败，请保留安装日志。'
      else if StableInstallCode = 'SETUP_PATCH_COPY_FAILED' then
        Result := '复制客户端文件失败。请关闭原版游戏和 Olivia 后重试，并保留安装日志以确认具体原因。'
      else
        Result := '这次安装没有完成。请点击“重试安装”；如果仍然失败，可通过“查看技术详情”联系支持。不要卸载已有程序。';
    end;
  finally
    InstallProgressPage.Hide;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  { Silent setup skips the interactive retry handler; retain its failure exit code. }
  if not InstallSucceeded then
    Result := RunInstallation;
end;
