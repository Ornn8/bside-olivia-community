[CmdletBinding()]
param(
    [string]$PayloadRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Destination = (Join-Path $env:LOCALAPPDATA 'BSideOliviaLocal\install'),
    [string]$OfficialRoot = '',
    [ValidateRange(1, 65535)]
    [int]$Port = 8899
)

$ErrorActionPreference = 'Stop'
$runtimeRoot = Join-Path $env:LOCALAPPDATA 'BSideOliviaLocal\runtime\python-3.12.10-embed-amd64'
$runtimeExe = Join-Path $runtimeRoot 'python.exe'
$runtimeZip = Join-Path $env:TEMP 'python-3.12.10-embed-amd64.zip'
$runtimeUrl = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip'
$runtimeSha256 = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'

function Update-ManagedPythonPath {
    param(
        [Parameter(Mandatory)]
        [string]$PthPath,
        [Parameter(Mandatory)]
        [string]$SitePackages
    )

    $pthDirectory = Split-Path -Parent $PthPath
    $sitePackagesFull = [IO.Path]::GetFullPath($SitePackages)
    $keptLines = New-Object 'System.Collections.Generic.List[string]'
    $hasSitePackages = $false
    $hasImportSite = $false
    foreach ($line in @(Get-Content -LiteralPath $PthPath)) {
        $trimmed = $line.Trim()
        if ($trimmed -eq 'import site') {
            if (-not $hasImportSite) {
                $keptLines.Add('import site')
                $hasImportSite = $true
            }
            continue
        }

        $candidate = $null
        if ($trimmed -and -not $trimmed.StartsWith('#')) {
            try {
                $candidate = if ([IO.Path]::IsPathRooted($trimmed)) {
                    [IO.Path]::GetFullPath($trimmed)
                } else {
                    [IO.Path]::GetFullPath((Join-Path $pthDirectory $trimmed))
                }
            } catch {
                $candidate = $null
            }
        }
        if ($candidate -and [StringComparer]::OrdinalIgnoreCase.Equals($candidate, $sitePackagesFull)) {
            if (-not $hasSitePackages) {
                $keptLines.Add('site-packages')
                $hasSitePackages = $true
            }
            continue
        }
        if ([IO.Path]::IsPathRooted($trimmed)) { continue }
        $keptLines.Add($line)
    }

    if (-not $hasSitePackages) { $keptLines.Add('site-packages') }
    if (-not $hasImportSite) { $keptLines.Add('import site') }
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [IO.File]::WriteAllLines($PthPath, $keptLines.ToArray(), $utf8NoBom)
}

$runner = @{ File = $runtimeExe; Args = @() }
if (-not (Test-Path -LiteralPath $runtimeExe)) {
    Write-Host 'The managed Python 3.12 runtime is not installed. The next step downloads the official PSF embeddable runtime.'
    Write-Host "Source: $runtimeUrl"
    Write-Host 'License: Python Software Foundation License (PSF).'
    $answer = Read-Host 'Accept this runtime license and download it? [Y/N]'
    if ($answer -notmatch '^(y|yes)$') { throw 'PYTHON_LICENSE_NOT_ACCEPTED' }
    New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
    Invoke-WebRequest -Uri $runtimeUrl -OutFile $runtimeZip
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $runtimeZip).Hash.ToLowerInvariant()
    if ($actual -ne $runtimeSha256) { Remove-Item -LiteralPath $runtimeZip -Force; throw 'PYTHON_RUNTIME_HASH_MISMATCH' }
    Expand-Archive -LiteralPath $runtimeZip -DestinationPath $runtimeRoot -Force
    Remove-Item -LiteralPath $runtimeZip -Force
}

if ($runner.File -eq $runtimeExe) {
    $sitePackages = Join-Path $runtimeRoot 'site-packages'
    New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null
    $pth = Get-ChildItem -LiteralPath $runtimeRoot -Filter '*._pth' | Select-Object -First 1
    if ($pth) { Update-ManagedPythonPath -PthPath $pth.FullName -SitePackages $sitePackages }
    & $runner.File '-c' 'import aiohttp,jsonschema' 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'The local server needs aiohttp, jsonschema, and their fixed Windows/Python 3.12 dependency closure.'
        Write-Host 'Licenses: aiohttp Apache-2.0; jsonschema MIT; transitive packages retain their upstream licenses.'
        $answer = Read-Host 'Accept these licenses and download the pinned wheels? [Y/N]'
        if ($answer -notmatch '^(y|yes)$') { throw 'AIOHTTP_LICENSE_NOT_ACCEPTED' }
        $pipScript = Join-Path $env:TEMP 'get-pip.py'
        Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $pipScript
        $pipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $pipScript).Hash.ToLowerInvariant()
        if ($pipHash -ne 'fb24e693bab954209a063d90953621412ccad4a500905a726286e038f508ddf6') { Remove-Item -LiteralPath $pipScript -Force; throw 'PIP_BOOTSTRAP_HASH_MISMATCH' }
        & $runner.File $pipScript --no-warn-script-location
        if ($LASTEXITCODE -ne 0) { throw 'PIP_BOOTSTRAP_FAILED' }
        Remove-Item -LiteralPath $pipScript -Force
        $requirements = Join-Path $PayloadRoot 'installer\runtime-requirements.txt'
        & $runner.File '-m' 'pip' 'install' '--disable-pip-version-check' '--require-hashes' '--only-binary=:all:' '--target' $sitePackages '-r' $requirements
        if ($LASTEXITCODE -ne 0) { throw 'AIOHTTP_INSTALL_FAILED' }
    }
}

$arguments = @('install', '--payload', $PayloadRoot, '--destination', $Destination, '--manifest', (Join-Path $PayloadRoot 'installer\full-patch-manifest.json'), '--port', $Port)
$selectedOfficial = $OfficialRoot
if (-not $selectedOfficial) {
    $selectedOfficial = Read-Host 'Steam 游戏目录（留空则按 AppID 自动发现）'
}
if ($selectedOfficial) { $arguments += @('--official-root', $selectedOfficial) }
$oldPythonPath = if ($env:PYTHONPATH) { $env:PYTHONPATH } else { '' }
$env:PYTHONPATH = $PayloadRoot + [IO.Path]::PathSeparator + $oldPythonPath
$bootstrap = 'import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module("installer",run_name="__main__")'
& $runner.File @($runner.Args + @('-c', $bootstrap, $PayloadRoot) + $arguments)
$installExitCode = $LASTEXITCODE
if ($installExitCode -ne 0) { exit $installExitCode }

& (Join-Path $PSScriptRoot 'Create-Shortcut.ps1') -InstallRoot $Destination
exit $LASTEXITCODE
