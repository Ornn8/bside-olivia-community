[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Iscc,
    [Parameter(Mandatory)][string]$SetupScript,
    [string]$TestRoot = $env:RUNNER_TEMP
)
$ErrorActionPreference = 'Stop'
if (-not $TestRoot) { $TestRoot = [IO.Path]::GetTempPath() }
$testBase = [IO.Path]::GetFullPath($TestRoot).TrimEnd('\')
$root = Join-Path $testBase ('olivia-setup-smoke-' + [guid]::NewGuid().ToString('N'))
$payload = Join-Path $root 'payload'
$output = Join-Path $root 'output'
$setup = Join-Path $output 'Olivia-Setup-x64.exe'
$fixture = Join-Path $payload 'installer\Install.ps1'
$setupScriptPath = (Resolve-Path -LiteralPath $SetupScript).Path
$repositoryRoot = Split-Path -Parent (Split-Path -Parent $setupScriptPath)
$sourceIcon = Join-Path $repositoryRoot 'installer\assets\olivia.ico'
$registryPath = 'HKCU:\Software\BSideOliviaCommunity'
$previousRoot = (Get-ItemProperty -LiteralPath $registryPath -Name ProductRoot -ErrorAction SilentlyContinue).ProductRoot

function Invoke-SmokeSetup([string]$Target, [string]$LogPath, [int]$ExpectedExit, [bool]$VerifyPayload = $true, [string]$ProductRoot = '') {
    if (-not $ProductRoot) { $ProductRoot = $Target }
    $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOICONS',
        ('/InstallRoot="' + $Target + '"'), ('/LOG="' + $LogPath + '"'))
    $process = Start-Process -FilePath $setup -ArgumentList $arguments -WindowStyle Hidden -PassThru -Wait
    $receipt = Join-Path $ProductRoot 'observed.json'
    if ($VerifyPayload -and -not (Test-Path -LiteralPath $receipt)) { throw 'SETUP_SMOKE_RECEIPT_MISSING' }
    if ($VerifyPayload -and (Test-Path -LiteralPath $receipt)) {
        $observed = Get-Content -Raw -LiteralPath $receipt | ConvertFrom-Json
        foreach ($path in @($observed.payload, $observed.temp, $observed.tmp)) {
            if (-not $path.StartsWith($ProductRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
                throw "SETUP_SMOKE_WRONG_PAYLOAD_DRIVE: $path"
            }
        }
        if ($observed.body_size -ne 64MB) { throw 'SETUP_SMOKE_INCOMPLETE_PAYLOAD' }
        if ($variant -eq 'bundled' -and $observed.official -ne (Join-Path $observed.payload 'offline\original-client')) {
            throw 'SETUP_SMOKE_WRONG_BUNDLED_SOURCE'
        }
    }
    if ($process.ExitCode -ne $ExpectedExit) {
        Write-Output (Get-Content -Raw -LiteralPath $LogPath)
        throw "SETUP_SMOKE_EXIT_CODE_INVALID: $($process.ExitCode), expected $ExpectedExit; $LogPath"
    }
}
function Assert-OwnedStagingRemoved([string]$Target) {
    if (@(Get-ChildItem -LiteralPath $Target -Force -Directory |
        Where-Object { $_.Name -like '.olivia-setup-*' -and $_.Name -ne '.olivia-setup-user-kept' }).Count) {
        throw 'SETUP_SMOKE_STAGING_RETAINED'
    }
}
try {
    $fixtureIcon = Join-Path $payload 'installer\assets\olivia.ico'
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $fixtureIcon), $output | Out-Null
    $utf8NoBom = [Text.UTF8Encoding]::new($false)
    [IO.File]::WriteAllText((Join-Path $payload 'LICENSE'), 'Synthetic test payload.', $utf8NoBom)
    Copy-Item -LiteralPath $sourceIcon -Destination $fixtureIcon
    $original = Join-Path $payload 'offline\original-client'
    New-Item -ItemType Directory -Path $original | Out-Null
    $body = [IO.File]::Create((Join-Path $original 'client.bin'))
    try { $body.SetLength(64MB) } finally { $body.Dispose() }
    foreach ($name in @('scene.mp4', 'song.mp3')) {
        [IO.File]::WriteAllText((Join-Path $original $name), 'Synthetic media', $utf8NoBom)
    }
    # Exercise the production path guard, rather than accepting an overlapping
    # bundled source just because the rest of installation uses a test fixture.
    $tokens = $null; $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $repositoryRoot 'installer\Install.ps1'), [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw 'SETUP_SMOKE_SOURCE_PARSE_FAILED' }
    $guards = $ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]}, $false) |
        Where-Object { $_.Name -in @('Resolve-ProductRoot', 'Test-PathsOverlap', 'Assert-NoReparsePointsInPath', 'Assert-OfficialSourceLocation') } |
        ForEach-Object { $_.Extent.Text }
    [IO.File]::WriteAllText((Join-Path $payload 'installer\source-guards.ps1'), ($guards -join "`n"), $utf8NoBom)
    $fixtureContent = @'
param(
    [string]$PayloadRoot, [string]$Destination, [string]$OfficialRoot,
    [string]$OfflineAssetsRoot, [string]$SetupResultPath, [string]$MemoryOfflinePackagePath,
    [switch]$NonInteractive, [switch]$SkipShortcut, [switch]$CloseRunningApplication
)
$ErrorActionPreference = 'Stop'
$utf8NoBom = [Text.UTF8Encoding]::new($false)
. (Join-Path $PayloadRoot 'installer\source-guards.ps1')
$Destination = Resolve-ProductRoot $Destination
if ($OfficialRoot) {
    try { Assert-OfficialSourceLocation -ProductRoot $Destination -PayloadRoot $PayloadRoot -SourceRoot $OfficialRoot }
    catch {
        [IO.File]::WriteAllText($SetupResultPath, ('OLIVIA_SETUP_ERROR=' + $_.Exception.Message), $utf8NoBom)
        exit 23
    }
}
[void][IO.Directory]::CreateDirectory($Destination)
[IO.File]::AppendAllText((Join-Path $Destination 'attempts.txt'), "attempt`n", $utf8NoBom)
# Cleanup must unlink a junction in staging without following it into user data.
New-Item -ItemType Junction -Path (Join-Path $env:TEMP 'user-data-link') -Value (Join-Path $Destination 'data') | Out-Null
@{payload=$PayloadRoot; temp=$env:TEMP; tmp=$env:TMP; official=$OfficialRoot;
    body_size=(Get-Item -LiteralPath (Join-Path $PayloadRoot 'offline\original-client\client.bin')).Length} |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Destination 'observed.json') -Encoding UTF8
if ((Split-Path -Leaf $Destination) -ne 'failure') {
    $app = Join-Path $Destination 'install\app'
    [void][IO.Directory]::CreateDirectory($app)
    Copy-Item -LiteralPath (Join-Path $PayloadRoot 'offline\original-client\client.bin') -Destination (Join-Path $app 'client.bin')
    [IO.File]::WriteAllText($SetupResultPath + '.product-root', $Destination, $utf8NoBom)
    exit 0
}
[IO.File]::WriteAllText($SetupResultPath, 'OLIVIA_SETUP_ERROR=TEST_INSTALL_FAILURE', $utf8NoBom)
Write-Output 'synthetic private-looking path C:\Users\fixture'
exit 23
'@
    [IO.File]::WriteAllText($fixture, $fixtureContent, $utf8NoBom)
    foreach ($variant in @('lean', 'bundled')) {
        $defines = @()
        if ($variant -eq 'bundled') { $defines = @('/DOriginalClientPayload=1', '/DMemoryOfflinePayload=1') }
        & $Iscc "/DPayloadRoot=$payload" "/DOutputDir=$output" '/DAppVersion=smoke' @defines $setupScriptPath
        if ($LASTEXITCODE -ne 0) { throw 'SETUP_SMOKE_COMPILE_FAILED' }
        $variantRoot = Join-Path $root $variant
        $failure = Join-Path $variantRoot 'failure'
        $success = Join-Path $variantRoot 'success'
        New-Item -ItemType Directory -Force -Path (Join-Path $failure 'data'),
            (Join-Path $success 'data'), (Join-Path $success '.olivia-setup-user-kept') | Out-Null
        $letters = Join-Path $success 'data\letters.txt'
        Set-Content -LiteralPath $letters -Value 'Keep user letters'
        $failedLetters = Join-Path $failure 'data\letters.txt'
        Set-Content -LiteralPath $failedLetters -Value 'Keep user letters on failure'
        $foreign = Join-Path $success '.olivia-setup-user-kept\keep.txt'
        Set-Content -LiteralPath $foreign -Value 'Keep unrelated folder'
        $log = Join-Path $root ($variant + '-failure.log')
        Invoke-SmokeSetup $failure $log 7
        $logText = Get-Content -Raw -LiteralPath $log
        if ($logText -notmatch 'Olivia installer code: TEST_INSTALL_FAILURE') { throw 'SETUP_SMOKE_STABLE_CODE_MISSING' }
        if ($logText -match 'synthetic private-looking') { throw 'SETUP_SMOKE_PRIVATE_OUTPUT_LEAKED' }
        if (@(Get-Content -LiteralPath (Join-Path $failure 'attempts.txt')).Count -ne 1) { throw 'SETUP_SMOKE_REPEATED_INSTALL' }
        Assert-OwnedStagingRemoved $failure
        if ((Get-Content -Raw -LiteralPath $failedLetters).Trim() -ne 'Keep user letters on failure') { throw 'SETUP_SMOKE_FAILED_USER_DATA_CHANGED' }
        for ($attempt = 1; $attempt -le 2; $attempt++) {
            Invoke-SmokeSetup $success (Join-Path $root ($variant + "-success-$attempt.log")) 0
            Assert-OwnedStagingRemoved $success
        }
        $existingInstall = Join-Path $success 'install'
        Set-Content -LiteralPath (Join-Path $existingInstall '.olivia-full-patch.json') -Value '{}'
        Invoke-SmokeSetup $existingInstall (Join-Path $root ($variant + '-existing-install.log')) 0 $true $success
        Assert-OwnedStagingRemoved $success
        if (Test-Path -LiteralPath (Join-Path $existingInstall 'install')) { throw 'SETUP_SMOKE_NESTED_INSTALL_CREATED' }
        if ((Get-Item -LiteralPath (Join-Path $success 'install\app\client.bin')).Length -ne 64MB) {
            throw 'SETUP_SMOKE_INSTALLED_BODY_MISSING'
        }
        if ((Get-Content -Raw -LiteralPath $letters).Trim() -ne 'Keep user letters' -or
            (Get-Content -Raw -LiteralPath $foreign).Trim() -ne 'Keep unrelated folder') { throw 'SETUP_SMOKE_USER_DATA_CHANGED' }
        $junction = Join-Path $variantRoot 'linked-install'
        New-Item -ItemType Junction -Path $junction -Value $success | Out-Null
        try { Invoke-SmokeSetup $junction (Join-Path $root ($variant + '-junction.log')) 7 $false }
        finally { [IO.Directory]::Delete($junction) }
        if (@(Get-Content -LiteralPath (Join-Path $success 'attempts.txt')).Count -ne 3) { throw 'SETUP_SMOKE_UNSAFE_PATH_INSTALLED' }
        Write-Output ("PASS: $variant selected directory, production source guard, failure cleanup, overwrite, existing install, user data, junction rejection")
    }
} finally {
    if ($null -ne $previousRoot) {
        Set-ItemProperty -LiteralPath $registryPath -Name ProductRoot -Value $previousRoot
    } elseif (Test-Path -LiteralPath $registryPath) {
        Remove-ItemProperty -LiteralPath $registryPath -Name ProductRoot -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $root) {
        if ([IO.Path]::GetFullPath((Split-Path -Parent $root)).TrimEnd('\') -ne $testBase -or
            (Split-Path -Leaf $root) -notlike 'olivia-setup-smoke-*') { throw 'SETUP_SMOKE_CLEANUP_PATH_INVALID' }
        Remove-Item -LiteralPath $root -Recurse -Force
    }
}
