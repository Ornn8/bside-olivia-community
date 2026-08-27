[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Iscc,
    [Parameter(Mandatory)]
    [string]$SetupScript
)

$ErrorActionPreference = 'Stop'
$root = Join-Path $env:RUNNER_TEMP ('olivia-setup-smoke-' + [guid]::NewGuid().ToString('N'))
$payload = Join-Path $root 'payload'
$output = Join-Path $root 'output'
$install = Join-Path $root 'install'
$log = Join-Path $root 'setup.log'
$fixture = Join-Path $payload 'installer\Install.ps1'

try {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $fixture), $output | Out-Null
    $utf8NoBom = [Text.UTF8Encoding]::new($false)
    [IO.File]::WriteAllText(
        (Join-Path $payload 'LICENSE'),
        'Synthetic test payload.',
        $utf8NoBom
    )
    $fixtureContent = @'
param(
    [string]$PayloadRoot,
    [string]$Destination,
    [string]$OfficialRoot,
    [string]$OfflineAssetsRoot,
    [string]$SetupResultPath,
    [switch]$NonInteractive
)
$utf8NoBom = [Text.UTF8Encoding]::new($false)
[IO.File]::WriteAllText(
    $SetupResultPath,
    'OLIVIA_SETUP_ERROR=TEST_INSTALL_FAILURE',
    $utf8NoBom
)
Write-Output 'synthetic private-looking path C:\Users\fixture'
exit 23
'@
    [IO.File]::WriteAllText($fixture, $fixtureContent, $utf8NoBom)

    & $Iscc "/DPayloadRoot=$payload" "/DOutputDir=$output" '/DAppVersion=smoke' $SetupScript
    if ($LASTEXITCODE -ne 0) { throw 'SETUP_SMOKE_COMPILE_FAILED' }

    $setup = Join-Path $output 'Olivia-Setup-x64.exe'
    $arguments = @(
        '/VERYSILENT',
        '/SUPPRESSMSGBOXES',
        '/NORESTART',
        ('/InstallRoot="' + $install + '"'),
        ('/LOG="' + $log + '"')
    )
    $process = Start-Process -FilePath $setup -ArgumentList $arguments -PassThru -Wait
    if ($process.ExitCode -ne 7) { throw 'SETUP_SMOKE_EXIT_CODE_INVALID' }

    $logText = Get-Content -Raw -LiteralPath $log
    if ($logText -notmatch 'Olivia installer code: TEST_INSTALL_FAILURE') {
        throw 'SETUP_SMOKE_STABLE_CODE_MISSING'
    }
    if ($logText -match 'synthetic private-looking') {
        throw 'SETUP_SMOKE_PRIVATE_OUTPUT_LEAKED'
    }
} finally {
    if (Test-Path -LiteralPath $root) {
        Remove-Item -LiteralPath $root -Recurse -Force
    }
}
