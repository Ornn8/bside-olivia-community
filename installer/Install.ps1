[CmdletBinding()]
param(
    [string]$PayloadRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Destination = (Join-Path $env:LOCALAPPDATA 'BSideOliviaLocal\install'),
    [string]$OfficialRoot = '',
    [string]$OfflineAssetsRoot = '',
    [switch]$SkipShortcut,
    [ValidateRange(1, 65535)]
    [int]$Port = 8899
)

$ErrorActionPreference = 'Stop'
$env:MEM0_TELEMETRY = 'False'
$runtimeRoot = Join-Path $env:LOCALAPPDATA 'BSideOliviaLocal\runtime\python-3.12.10-embed-amd64'
$runtimeExe = Join-Path $runtimeRoot 'python.exe'
$offlineRoot = if ($OfflineAssetsRoot) { $OfflineAssetsRoot } else { Join-Path $PayloadRoot 'offline' }
$offlineManifestPath = if ($OfflineAssetsRoot) { Join-Path $OfflineAssetsRoot 'offline-core-assets.json' } else { Join-Path $PayloadRoot 'offline\offline-core-assets.json' }
$requirements = Join-Path $PayloadRoot 'installer\runtime-requirements.txt'

function Get-Sha256 {
    param(
        [Parameter(Mandatory)]
        [string]$LiteralPath
    )

    $stream = [IO.File]::OpenRead($LiteralPath)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($hasher.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
    } finally {
        $hasher.Dispose()
        $stream.Dispose()
    }
}

function Assert-OfflineObjectShape {
    param(
        [Parameter(Mandatory)]
        [AllowNull()]
        [object]$Value,
        [Parameter(Mandatory)]
        [string[]]$Names
    )

    if ($Value -isnot [pscustomobject]) { throw 'OFFLINE_CORE_MANIFEST_INVALID' }
    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    $expected = @($Names | Sort-Object)
    if ($actual.Count -ne $expected.Count -or [string]::Join("`n", $actual) -cne [string]::Join("`n", $expected)) {
        throw 'OFFLINE_CORE_MANIFEST_INVALID'
    }
}

function Get-ExpectedOfflineWheels {
    $expected = [Collections.Generic.Dictionary[string,string]]::new([StringComparer]::Ordinal)
    $expected.Add('wheelhouse/aiohappyeyeballs-2.7.1-py3-none-any.whl', '9243213661e29250eb41368e5daa826fc017156c3b8a11440826b2e3ed376472')
    $expected.Add('wheelhouse/aiohttp-3.14.1-cp312-cp312-win_amd64.whl', '2aa92c87868cd13674989f9ee83e5f9f7ea4237589b728048e1f0c8f6caa3271')
    $expected.Add('wheelhouse/aiosignal-1.4.0-py3-none-any.whl', '053243f8b92b990551949e63930a839ff0cf0b0ebbe0597b0f3fb19e1a0fe82e')
    $expected.Add('wheelhouse/attrs-26.1.0-py3-none-any.whl', 'c647aa4a12dfbad9333ca4e71fe62ddc36f4e63b2d260a37a8b83d2f043ac309')
    $expected.Add('wheelhouse/frozenlist-1.8.0-cp312-cp312-win_amd64.whl', '34187385b08f866104f0c0617404c8eb08165ab1272e884abc89c112e9c00746')
    $expected.Add('wheelhouse/idna-3.18-py3-none-any.whl', '7f952cbe720b688055e3f87de14f5c3e5fdaa8bc3928985c4077ca689de849a2')
    $expected.Add('wheelhouse/jsonschema-4.26.0-py3-none-any.whl', 'd489f15263b8d200f8387e64b4c3a75f06629559fb73deb8fdfb525f2dab50ce')
    $expected.Add('wheelhouse/jsonschema_specifications-2025.9.1-py3-none-any.whl', '98802fee3a11ee76ecaca44429fda8a41bff98b00a0f2838151b113f210cc6fe')
    $expected.Add('wheelhouse/multidict-6.7.1-cp312-cp312-win_amd64.whl', 'fcee94dfbd638784645b066074b338bc9cc155d4b4bffa4adce1615c5a426c19')
    $expected.Add('wheelhouse/propcache-0.5.2-cp312-cp312-win_amd64.whl', 'd9ee8826a7d47863a08ac44e1a5f611a462eefc3a194b492da242128bec75b42')
    $expected.Add('wheelhouse/referencing-0.37.0-py3-none-any.whl', '381329a9f99628c9069361716891d34ad94af76e461dcb0335825aecc7692231')
    $expected.Add('wheelhouse/rpds_py-2026.6.3-cp312-cp312-win_amd64.whl', '2c958bf94822e9290a40aaf2a822d4bc5c88099093e3948ad6c571eca9272e5f')
    $expected.Add('wheelhouse/typing_extensions-4.16.0-py3-none-any.whl', '481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8')
    $expected.Add('wheelhouse/yarl-1.24.2-cp312-cp312-win_amd64.whl', '7dafe10c12ddd4d120d528c4b5599c953bd7b12845347d507b95451195bb6cad')
    return ,$expected
}

function Assert-ManagedRuntimeParent {
    param(
        [Parameter(Mandatory)]
        [string]$LocalAppData,
        [Parameter(Mandatory)]
        [string]$RuntimePath
    )

    try {
        $localFull = [IO.Path]::GetFullPath($LocalAppData)
        $productRoot = Join-Path $localFull 'BSideOliviaLocal'
        $runtimeParent = Join-Path $productRoot 'runtime'
        $expectedRuntime = Join-Path $runtimeParent 'python-3.12.10-embed-amd64'
        if (-not [string]::Equals([IO.Path]::GetFullPath($RuntimePath), $expectedRuntime, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'OFFLINE_CORE_RUNTIME_PARENT_INVALID'
        }
        foreach ($path in @($localFull, $productRoot, $runtimeParent, $expectedRuntime)) {
            if (Test-Path -LiteralPath $path) {
                if (
                    -not [IO.Directory]::Exists($path) -or
                    (([IO.File]::GetAttributes($path) -band [IO.FileAttributes]::ReparsePoint) -ne 0)
                ) {
                    throw 'OFFLINE_CORE_RUNTIME_PARENT_INVALID'
                }
            }
        }
    } catch {
        throw 'OFFLINE_CORE_RUNTIME_PARENT_INVALID'
    }
}

function Resolve-OfflineAsset {
    param(
        [Parameter(Mandatory)]
        [string]$Root,
        [Parameter(Mandatory)]
        [object]$Asset
    )

    if (
        $Asset.path -isnot [string] -or
        ($Asset.size_bytes -isnot [int] -and $Asset.size_bytes -isnot [long]) -or
        [int64]$Asset.size_bytes -lt 1 -or
        $Asset.sha256 -isnot [string] -or
        $Asset.sha256 -cnotmatch '^[0-9a-f]{64}$'
    ) {
        throw 'OFFLINE_CORE_MANIFEST_INVALID'
    }
    $relative = $Asset.path
    if (-not $relative -or [IO.Path]::IsPathRooted($relative) -or $relative.Contains('\')) {
        throw 'OFFLINE_CORE_MANIFEST_INVALID'
    }
    $parts = $relative.Split('/')
    if ($parts.Count -eq 0 -or $parts -contains '' -or $parts -contains '.' -or $parts -contains '..') {
        throw 'OFFLINE_CORE_MANIFEST_INVALID'
    }
    $rootFull = [IO.Path]::GetFullPath($Root)
    $path = [IO.Path]::GetFullPath((Join-Path $rootFull ($relative.Replace('/', '\'))))
    $prefix = $rootFull.TrimEnd('\') + '\'
    if (-not $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'OFFLINE_CORE_MANIFEST_INVALID'
    }
    if (-not [IO.File]::Exists($path)) { throw 'OFFLINE_CORE_ASSET_MISSING' }
    if (([IO.File]::GetAttributes($rootFull) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'OFFLINE_CORE_ASSET_REPARSE_POINT'
    }
    $current = $rootFull
    foreach ($part in $parts) {
        $current = Join-Path $current $part
        if (([IO.File]::GetAttributes($current) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'OFFLINE_CORE_ASSET_REPARSE_POINT'
        }
    }
    if ([IO.FileInfo]::new($path).Length -ne [int64]$Asset.size_bytes) {
        throw 'OFFLINE_CORE_ASSET_SIZE_MISMATCH'
    }
    if ((Get-Sha256 -LiteralPath $path) -ne [string]$Asset.sha256) {
        throw 'OFFLINE_CORE_ASSET_HASH_MISMATCH'
    }
    return $path
}

function Get-OfflineCoreAssets {
    param(
        [Parameter(Mandatory)]
        [string]$Root,
        [Parameter(Mandatory)]
        [string]$ManifestPath,
        [Parameter(Mandatory)]
        [string]$RequirementsPath
    )

    if (-not [IO.File]::Exists($ManifestPath)) { throw 'OFFLINE_CORE_ASSETS_MISSING' }
    try {
        $manifest = [IO.File]::ReadAllText($ManifestPath) | ConvertFrom-Json
    } catch {
        throw 'OFFLINE_CORE_MANIFEST_INVALID'
    }
    Assert-OfflineObjectShape -Value $manifest -Names @('schema_version', 'python_runtime', 'pip_bootstrap', 'requirements_sha256', 'wheels')
    Assert-OfflineObjectShape -Value $manifest.python_runtime -Names @('path', 'size_bytes', 'sha256', 'source_url')
    Assert-OfflineObjectShape -Value $manifest.pip_bootstrap -Names @('path', 'size_bytes', 'sha256', 'package', 'version')
    if ($manifest.schema_version -ne 'olivia.offline-core-assets.v1') {
        throw 'OFFLINE_CORE_MANIFEST_INVALID'
    }
    if (
        $manifest.python_runtime.path -ne 'python-3.12.10-embed-amd64.zip' -or
        $manifest.python_runtime.sha256 -ne '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3' -or
        $manifest.pip_bootstrap.path -ne 'pip-25.2-py3-none-any.whl' -or
        $manifest.pip_bootstrap.sha256 -ne '6d67a2b4e7f14d8b31b8b52648866fa717f45a1eb70e83002f4331d07e953717' -or
        $manifest.pip_bootstrap.package -ne 'pip' -or
        $manifest.pip_bootstrap.version -ne '25.2'
    ) {
        throw 'OFFLINE_CORE_MANIFEST_INVALID'
    }
    $sourceUri = $null
    if (
        $manifest.python_runtime.source_url -isnot [string] -or
        -not [Uri]::TryCreate($manifest.python_runtime.source_url, [UriKind]::Absolute, [ref]$sourceUri) -or
        $sourceUri.Scheme -ne 'https'
    ) {
        throw 'OFFLINE_CORE_MANIFEST_INVALID'
    }
    if (
        $manifest.requirements_sha256 -isnot [string] -or
        $manifest.requirements_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        $manifest.wheels -isnot [array] -or
        $manifest.wheels.Count -ne 14
    ) {
        throw 'OFFLINE_CORE_MANIFEST_INVALID'
    }
    if ((Get-Sha256 -LiteralPath $RequirementsPath) -ne [string]$manifest.requirements_sha256) {
        throw 'OFFLINE_CORE_REQUIREMENTS_MISMATCH'
    }
    $runtime = Resolve-OfflineAsset -Root $Root -Asset $manifest.python_runtime
    $pipBootstrap = Resolve-OfflineAsset -Root $Root -Asset $manifest.pip_bootstrap
    $expectedWheelAssets = Get-ExpectedOfflineWheels
    $wheelPaths = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $manifestWheelHashes = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    foreach ($wheel in @($manifest.wheels)) {
        Assert-OfflineObjectShape -Value $wheel -Names @('path', 'size_bytes', 'sha256')
        if (
            -not $expectedWheelAssets.ContainsKey([string]$wheel.path) -or
            $expectedWheelAssets[[string]$wheel.path] -cne [string]$wheel.sha256
        ) {
            throw 'OFFLINE_CORE_WHEEL_SET_MISMATCH'
        }
        $wheelPath = Resolve-OfflineAsset -Root $Root -Asset $wheel
        if (-not $wheelPath.EndsWith('.whl', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'OFFLINE_CORE_MANIFEST_INVALID'
        }
        if (-not $wheelPaths.Add($wheelPath)) { throw 'OFFLINE_CORE_MANIFEST_INVALID' }
        if (-not $manifestWheelHashes.Add([string]$wheel.sha256)) { throw 'OFFLINE_CORE_MANIFEST_INVALID' }
    }
    $lockedWheelHashes = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    foreach ($line in [IO.File]::ReadAllLines($RequirementsPath)) {
        $match = [regex]::Match($line, '--hash=sha256:([0-9a-f]{64})')
        if ($match.Success -and -not $lockedWheelHashes.Add($match.Groups[1].Value)) {
            throw 'OFFLINE_CORE_REQUIREMENTS_INVALID'
        }
    }
    if ($lockedWheelHashes.Count -ne 14 -or -not $lockedWheelHashes.SetEquals($manifestWheelHashes)) {
        throw 'OFFLINE_CORE_WHEEL_SET_MISMATCH'
    }
    $actualWheels = @(Get-ChildItem -LiteralPath (Join-Path $Root 'wheelhouse') -Filter '*.whl' -File)
    if ($actualWheels.Count -ne $wheelPaths.Count) { throw 'OFFLINE_CORE_WHEEL_SET_MISMATCH' }
    foreach ($wheel in $actualWheels) {
        if (-not $wheelPaths.Contains($wheel.FullName)) { throw 'OFFLINE_CORE_WHEEL_SET_MISMATCH' }
    }
    return @{
        Runtime = $runtime
        PipBootstrap = $pipBootstrap
        Wheelhouse = (Join-Path $Root 'wheelhouse')
    }
}

function Update-ManagedPythonPath {
    param(
        [Parameter(Mandatory)]
        [string]$PthPath,
        [string]$MemoryRuntimePath = ''
    )

    $pthFullPath = [IO.Path]::GetFullPath($PthPath)
    $memoryRuntimeFullPath = if ($MemoryRuntimePath) { [IO.Path]::GetFullPath($MemoryRuntimePath) } else { '' }
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    $keptLines = New-Object 'System.Collections.Generic.List[string]'
    $hasSitePackages = $false
    $hasImportSite = $false
    foreach ($line in @([IO.File]::ReadAllLines($pthFullPath, $utf8NoBom))) {
        $trimmed = $line.Trim()
        if ($trimmed -eq 'site-packages') {
            if (-not $hasSitePackages) {
                $keptLines.Add('site-packages')
                $hasSitePackages = $true
            }
            continue
        }
        if ($trimmed -eq 'import site') {
            if (-not $hasImportSite) {
                $keptLines.Add('import site')
                $hasImportSite = $true
            }
            continue
        }
        if ($memoryRuntimeFullPath -and $trimmed) {
            try { $registeredPath = [IO.Path]::GetFullPath($trimmed) } catch { $registeredPath = '' }
            if ($registeredPath -and $registeredPath -match '[\\/]runtime[\\/]mem0-site-packages(?:[\\/].*)?$') {
                continue
            }
        }
        if ($memoryRuntimeFullPath -and $trimmed -eq $memoryRuntimeFullPath) {
            continue
        }
        $keptLines.Add($line)
    }

    if (-not $hasSitePackages) { $keptLines.Add('site-packages') }
    if ($memoryRuntimeFullPath) {
        $keptLines.Insert(0, (Join-Path $memoryRuntimeFullPath 'win32\lib'))
        $keptLines.Insert(0, (Join-Path $memoryRuntimeFullPath 'win32'))
        $keptLines.Insert(0, $memoryRuntimeFullPath)
    }
    if (-not $hasImportSite) { $keptLines.Add('import site') }
    $transactionId = [guid]::NewGuid().ToString('N')
    $tempName = '.' + [IO.Path]::GetFileName($pthFullPath) + '.' + $transactionId + '.tmp'
    $backupName = '.' + [IO.Path]::GetFileName($pthFullPath) + '.' + $transactionId + '.bak'
    $tempPath = Join-Path ([IO.Path]::GetDirectoryName($pthFullPath)) $tempName
    $backupPath = Join-Path ([IO.Path]::GetDirectoryName($pthFullPath)) $backupName
    try {
        [IO.File]::WriteAllLines($tempPath, $keptLines.ToArray(), $utf8NoBom)
        [IO.File]::Replace($tempPath, $pthFullPath, $backupPath)
    } finally {
        if (Test-Path -LiteralPath $tempPath) {
            Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $backupPath) {
            Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
        }
    }
}

function Test-ManagedServerDependencies {
    param(
        [Parameter(Mandatory)]
        [string]$PythonExe
    )

    try {
        & $PythonExe '-c' 'import aiohttp,jsonschema' 2>$null
        return $LASTEXITCODE -eq 0
    } catch {
        if ($LASTEXITCODE -eq 0) { throw }
        return $false
    }
}

Assert-ManagedRuntimeParent -LocalAppData $env:LOCALAPPDATA -RuntimePath $runtimeRoot
$coreAssets = Get-OfflineCoreAssets -Root $offlineRoot -ManifestPath $offlineManifestPath -RequirementsPath $requirements
$runtimeStaging = $runtimeRoot + '.staging.' + [guid]::NewGuid().ToString('N')
$runtimeBackup = ''
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $runtimeRoot) | Out-Null
try {
    Expand-Archive -LiteralPath $coreAssets.Runtime -DestinationPath $runtimeStaging -Force
    if (-not [IO.File]::Exists((Join-Path $runtimeStaging 'python.exe'))) {
        throw 'OFFLINE_CORE_RUNTIME_INVALID'
    }
    $candidateExe = Join-Path $runtimeStaging 'python.exe'
    $sitePackages = Join-Path $runtimeStaging 'site-packages'
    New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null
    $pth = Get-ChildItem -LiteralPath $runtimeStaging -Filter '*._pth' | Select-Object -First 1
    if (-not $pth) { throw 'OFFLINE_CORE_RUNTIME_INVALID' }
    Update-ManagedPythonPath -PthPath $pth.FullName
    if (-not (Test-ManagedServerDependencies -PythonExe $candidateExe)) {
        & $candidateExe '-m' 'zipfile' '-e' $coreAssets.PipBootstrap $sitePackages
        if ($LASTEXITCODE -ne 0) { throw 'OFFLINE_CORE_PIP_BOOTSTRAP_FAILED' }
        & $candidateExe '-m' 'pip' 'install' '--disable-pip-version-check' '--no-index' '--find-links' $coreAssets.Wheelhouse '--require-hashes' '--only-binary=:all:' '--target' $sitePackages '-r' $requirements
        if ($LASTEXITCODE -ne 0) { throw 'OFFLINE_CORE_DEPENDENCY_INSTALL_FAILED' }
        if (-not (Test-ManagedServerDependencies -PythonExe $candidateExe)) {
            throw 'OFFLINE_CORE_DEPENDENCY_VERIFY_FAILED'
        }
    }
    if (Test-Path -LiteralPath $runtimeRoot) {
        if (
            -not [IO.Directory]::Exists($runtimeRoot) -or
            (([IO.File]::GetAttributes($runtimeRoot) -band [IO.FileAttributes]::ReparsePoint) -ne 0)
        ) {
            throw 'OFFLINE_CORE_RUNTIME_INVALID'
        }
        $runtimeBackup = $runtimeRoot + '.backup.' + [guid]::NewGuid().ToString('N')
        [IO.Directory]::Move($runtimeRoot, $runtimeBackup)
    }
    try {
        [IO.Directory]::Move($runtimeStaging, $runtimeRoot)
    } catch {
        if ($runtimeBackup -and -not (Test-Path -LiteralPath $runtimeRoot) -and (Test-Path -LiteralPath $runtimeBackup)) {
            [IO.Directory]::Move($runtimeBackup, $runtimeRoot)
        }
        throw 'OFFLINE_CORE_RUNTIME_PUBLISH_FAILED'
    }
    if ($runtimeBackup -and (Test-Path -LiteralPath $runtimeBackup)) {
        Remove-Item -LiteralPath $runtimeBackup -Recurse -Force -ErrorAction SilentlyContinue
    }
} catch {
    if (Test-Path -LiteralPath $runtimeStaging) {
        Remove-Item -LiteralPath $runtimeStaging -Recurse -Force
    }
    if ($runtimeBackup -and -not (Test-Path -LiteralPath $runtimeRoot) -and (Test-Path -LiteralPath $runtimeBackup)) {
        [IO.Directory]::Move($runtimeBackup, $runtimeRoot)
    }
    throw
}
$runner = @{ File = $runtimeExe; Args = @() }

$arguments = @('install', '--payload', $PayloadRoot, '--destination', $Destination, '--manifest', (Join-Path $PayloadRoot 'installer\full-patch-manifest.json'), '--port', $Port)
$selectedOfficial = $OfficialRoot
if (-not $selectedOfficial) {
    $selectedOfficial = Read-Host 'Steam 游戏目录（留空则按 AppID 自动发现）'
}
if ($selectedOfficial) { $arguments += @('--official-root', $selectedOfficial) }
$oldPythonPath = if ($env:PYTHONPATH) { $env:PYTHONPATH } else { '' }
$env:PYTHONPATH = $PayloadRoot + [IO.Path]::PathSeparator + $oldPythonPath
$bootstrap = Join-Path $PayloadRoot 'installer\bootstrap_install.py'
& $runner.File @($runner.Args + @($bootstrap, $PayloadRoot) + $arguments)
$installExitCode = $LASTEXITCODE
if ($installExitCode -ne 0) { exit $installExitCode }

$LASTEXITCODE = 0

if (-not $SkipShortcut) {
    & (Join-Path $PSScriptRoot 'Create-Shortcut.ps1') -InstallRoot $Destination
}
exit 0
