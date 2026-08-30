[CmdletBinding()]
param(
    [string]$PayloadRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Destination = (Join-Path $env:LOCALAPPDATA 'BSideOliviaLocal'),
    [string]$OfficialRoot = '',
    [string]$OfflineAssetsRoot = '',
    [string]$SetupResultPath = '',
    [switch]$NonInteractive,
    [switch]$SkipShortcut,
    [ValidateRange(1, 65535)]
    [int]$Port = 8899
)

$ErrorActionPreference = 'Stop'
$env:MEM0_TELEMETRY = 'False'
$offlineRoot = if ($OfflineAssetsRoot) { $OfflineAssetsRoot } else { Join-Path $PayloadRoot 'offline' }
$offlineManifestPath = if ($OfflineAssetsRoot) { Join-Path $OfflineAssetsRoot 'offline-core-assets.json' } else { Join-Path $PayloadRoot 'offline\offline-core-assets.json' }
$requirements = Join-Path $PayloadRoot 'installer\runtime-requirements.txt'
$setupDiagnosticPath = if ($SetupResultPath) { $SetupResultPath + '.diagnostic.json' } else { '' }
$script:OfficialSourceDiagnostic = $null

function Get-SafeSetupErrorCode {
    param([string]$Code)

    if ($Code -cmatch '^[A-Z][A-Z0-9_]{3,95}$') {
        $Code
    } else {
        'SETUP_INSTALL_FAILED'
    }
}

function Write-SetupErrorResult {
    param([string]$Code)

    if (-not $SetupResultPath) { return }
    $safeCode = Get-SafeSetupErrorCode -Code $Code
    try {
        $utf8NoBom = [Text.UTF8Encoding]::new($false)
        [IO.File]::WriteAllText(
            $SetupResultPath,
            ('OLIVIA_SETUP_ERROR=' + $safeCode),
            $utf8NoBom
        )
    } catch {
        # The installer still receives the non-zero process code.
    }
}

function Write-SetupDiagnosticResult {
    param([AllowNull()][object]$Diagnostic)

    if (-not $setupDiagnosticPath -or $null -eq $Diagnostic) { return }
    try {
        $utf8NoBom = [Text.UTF8Encoding]::new($false)
        [IO.File]::WriteAllText(
            $setupDiagnosticPath,
            ($Diagnostic | ConvertTo-Json -Compress -Depth 4),
            $utf8NoBom
        )
    } catch {
        # Diagnostics must never replace the stable installer error code.
    }
}

trap {
    $safeCode = Get-SafeSetupErrorCode -Code ([string]$_.Exception.Message)
    Write-SetupDiagnosticResult -Diagnostic $script:OfficialSourceDiagnostic
    Write-SetupErrorResult -Code $safeCode
    if (-not $SetupResultPath) { Write-Output $safeCode }
    exit 2
}

$productRoot = [IO.Path]::GetFullPath($Destination)
$legacyDefaultInstall = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'BSideOliviaLocal\install'))
if ([string]::Equals($productRoot.TrimEnd('\'), $legacyDefaultInstall.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
    $productRoot = Split-Path -Parent $productRoot
}
$Destination = Join-Path $productRoot 'install'
$runtimeRoot = Join-Path $productRoot 'runtime\python-3.12.10-embed-amd64'
$runtimeExe = Join-Path $runtimeRoot 'python.exe'

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

function New-OfficialSourceDiagnostic {
    param(
        [Parameter(Mandatory)]
        [object]$Selection,
        [Parameter(Mandatory)]
        [string]$ManifestPath
    )

    $manifest = [IO.File]::ReadAllText($ManifestPath) | ConvertFrom-Json
    $selected = [string]$Selection.Path
    $normalizedSelected = [IO.Path]::GetFullPath($selected).TrimEnd('\').ToLowerInvariant()
    $pathHasher = [Security.Cryptography.SHA256]::Create()
    try {
        $selectedId = ([BitConverter]::ToString(
            $pathHasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($normalizedSelected))
        )).Replace('-', '').ToLowerInvariant().Substring(0, 16)
    } finally {
        $pathHasher.Dispose()
    }
    $version = [string]$manifest.client_version
    $resources = Join-Path (Join-Path $selected $version) 'resources'
    $feapp = Join-Path $resources 'feapp.dat'
    $webplayer = Join-Path $resources 'webplayer.dat'
    $feappInfo = if ([IO.File]::Exists($feapp)) { [IO.FileInfo]::new($feapp) } else { $null }
    $webplayerInfo = if ([IO.File]::Exists($webplayer)) { [IO.FileInfo]::new($webplayer) } else { $null }
    return [ordered]@{
        schema_version = 'olivia.setup-source-diagnostic.v1'
        selection_mode = [string]$Selection.SelectionMode
        candidate_count = [int]$Selection.CandidateCount
        selected_official_id = $selectedId
        client_version = $version
        observed_feapp_size = if ($feappInfo) { $feappInfo.Length } else { $null }
        observed_feapp_sha256 = if ($feappInfo) { Get-Sha256 -LiteralPath $feapp } else { $null }
        observed_webplayer_size = if ($webplayerInfo) { $webplayerInfo.Length } else { $null }
        observed_webplayer_sha256 = if ($webplayerInfo) { Get-Sha256 -LiteralPath $webplayer } else { $null }
        manifest_feapp_sha256 = [string]$manifest.feapp_sha256
        manifest_webplayer_sha256 = [string]$manifest.webplayer_sha256
    }
}

function Assert-NoReparsePointsInPath {
    param(
        [Parameter(Mandatory)]
        [string]$LiteralPath,
        [string]$ErrorCode = 'OFFLINE_CORE_RUNTIME_PARENT_INVALID'
    )

    $full = [IO.Path]::GetFullPath($LiteralPath)
    $root = [IO.Path]::GetPathRoot($full)
    if (-not $root) { throw $ErrorCode }
    if (
        -not [IO.Directory]::Exists($root) -or
        (([IO.File]::GetAttributes($root) -band [IO.FileAttributes]::ReparsePoint) -ne 0)
    ) {
        throw $ErrorCode
    }
    $current = $root
    $parts = $full.Substring($root.Length).Split(
        [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar),
        [StringSplitOptions]::RemoveEmptyEntries
    )
    foreach ($part in $parts) {
        $current = Join-Path $current $part
        if (Test-Path -LiteralPath $current) {
            if (
                -not [IO.Directory]::Exists($current) -or
                (([IO.File]::GetAttributes($current) -band [IO.FileAttributes]::ReparsePoint) -ne 0)
            ) {
                throw $ErrorCode
            }
        }
    }
}

function Test-NoReparsePointsInTree {
    param(
        [Parameter(Mandatory)]
        [string]$LiteralPath
    )

    try {
        $root = [IO.Path]::GetFullPath($LiteralPath)
        if (-not [IO.Directory]::Exists($root)) { return $false }
        $pending = [Collections.Generic.Stack[string]]::new()
        $pending.Push($root)
        while ($pending.Count -gt 0) {
            $current = $pending.Pop()
            foreach ($entry in [IO.Directory]::EnumerateFileSystemEntries($current)) {
                $attributes = [IO.File]::GetAttributes($entry)
                if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                    return $false
                }
                if (($attributes -band [IO.FileAttributes]::Directory) -ne 0) {
                    $pending.Push($entry)
                }
            }
        }
        return $true
    } catch {
        return $false
    }
}

function Test-PathsOverlap {
    param(
        [Parameter(Mandatory)]
        [string]$Left,
        [Parameter(Mandatory)]
        [string]$Right
    )

    $leftFull = [IO.Path]::GetFullPath($Left).TrimEnd('\')
    $rightFull = [IO.Path]::GetFullPath($Right).TrimEnd('\')
    if ([string]::Equals($leftFull, $rightFull, [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return (
        $leftFull.StartsWith($rightFull + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $rightFull.StartsWith($leftFull + '\', [StringComparison]::OrdinalIgnoreCase)
    )
}

function Resolve-OfficialInstall {
    param(
        [string]$RequestedRoot,
        [Parameter(Mandatory)]
        [string]$ManifestPath,
        [string[]]$SteamRoots = @()
    )

    if ($RequestedRoot) {
        return [pscustomobject]@{
            Path = [IO.Path]::GetFullPath($RequestedRoot)
            SelectionMode = 'explicit'
            CandidateCount = 1
        }
    }
    if (-not $PSBoundParameters.ContainsKey('SteamRoots')) {
        $discoveredSteamRoots = [Collections.Generic.List[string]]::new()
        try {
            $steamPath = (Get-ItemProperty -LiteralPath 'HKCU:\Software\Valve\Steam' -Name SteamPath).SteamPath
            if ($steamPath) { $discoveredSteamRoots.Add([string]$steamPath) }
        } catch {
            # Continue with conventional Steam roots.
        }
        foreach ($drive in 'CDEFGHIJKLMNOPQRSTUVWXYZ'.ToCharArray()) {
            $candidate = $drive + ':\steam'
            if ([IO.Directory]::Exists($candidate)) {
                $discoveredSteamRoots.Add($candidate)
            }
        }
        $SteamRoots = $discoveredSteamRoots.ToArray()
    }
    $libraryRoots = [Collections.Generic.List[string]]::new()
    foreach ($steamRoot in $SteamRoots) {
        try {
            $steamFull = [IO.Path]::GetFullPath($steamRoot)
            $libraryRoots.Add($steamFull)
            $vdf = Join-Path $steamFull 'steamapps\libraryfolders.vdf'
            if ([IO.File]::Exists($vdf)) {
                $text = [IO.File]::ReadAllText($vdf)
                foreach ($match in [regex]::Matches($text, '"path"\s+"([^"]+)"', [Text.RegularExpressions.RegexOptions]::IgnoreCase)) {
                    $libraryRoots.Add($match.Groups[1].Value.Replace('\\', '\'))
                }
            }
        } catch {
            # Ignore malformed or inaccessible Steam library entries.
        }
    }
    $candidates = [Collections.Generic.List[string]]::new()
    $seenCandidates = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    foreach ($libraryRoot in $libraryRoots) {
        try {
            $libraryFull = [IO.Path]::GetFullPath($libraryRoot)
            $appManifest = Join-Path $libraryFull 'steamapps\appmanifest_4532590.acf'
            if (-not [IO.File]::Exists($appManifest)) { continue }
            $match = [regex]::Match(
                [IO.File]::ReadAllText($appManifest),
                '"installdir"\s+"([^"]+)"',
                [Text.RegularExpressions.RegexOptions]::IgnoreCase
            )
            if (-not $match.Success) { continue }
            $candidate = Join-Path $libraryFull ('steamapps\common\' + $match.Groups[1].Value)
            if ([IO.Directory]::Exists($candidate)) {
                $candidateFull = [IO.Path]::GetFullPath($candidate)
                if ($seenCandidates.Add($candidateFull)) {
                    $candidates.Add($candidateFull)
                }
            }
        } catch {
            # Continue to the next Steam library.
        }
    }
    if ($candidates.Count -eq 0) { throw 'OFFICIAL_INSTALL_NOT_FOUND' }
    if ($candidates.Count -eq 1) {
        return [pscustomobject]@{
            Path = $candidates[0]
            SelectionMode = 'auto_single'
            CandidateCount = 1
        }
    }

    try {
        $manifest = [IO.File]::ReadAllText($ManifestPath) | ConvertFrom-Json
        $version = [string]$manifest.client_version
        $expectedFeapp = [string]$manifest.feapp_sha256
        $expectedWebplayer = [string]$manifest.webplayer_sha256
        if (
            -not $version -or
            $expectedFeapp -cnotmatch '^[0-9a-fA-F]{64}$' -or
            $expectedWebplayer -cnotmatch '^[0-9a-fA-F]{64}$'
        ) {
            throw 'PATCH_MANIFEST_INVALID'
        }
    } catch {
        if ([string]$_.Exception.Message -eq 'PATCH_MANIFEST_INVALID') { throw }
        throw 'PATCH_MANIFEST_INVALID'
    }
    $candidateDiagnostics = [Collections.Generic.List[object]]::new()
    $candidateIndex = 0
    foreach ($candidate in $candidates) {
        $resources = Join-Path (Join-Path $candidate $version) 'resources'
        $feapp = Join-Path $resources 'feapp.dat'
        $webplayer = Join-Path $resources 'webplayer.dat'
        $observedFeappSize = $null
        $observedWebplayerSize = $null
        $actualFeapp = $null
        $actualWebplayer = $null
        if ([IO.File]::Exists($feapp)) {
            try {
                $observedFeappSize = [IO.FileInfo]::new($feapp).Length
                $actualFeapp = Get-Sha256 -LiteralPath $feapp
            } catch {
                # This candidate remains observable but cannot be an exact match.
            }
        }
        if ([IO.File]::Exists($webplayer)) {
            try {
                $observedWebplayerSize = [IO.FileInfo]::new($webplayer).Length
                $actualWebplayer = Get-Sha256 -LiteralPath $webplayer
            } catch {
                # This candidate remains observable but cannot be an exact match.
            }
        }
        $candidateDiagnostics.Add([ordered]@{
            candidate_index = $candidateIndex
            observed_feapp_size = $observedFeappSize
            observed_feapp_sha256 = $actualFeapp
            observed_webplayer_size = $observedWebplayerSize
            observed_webplayer_sha256 = $actualWebplayer
        })
        $candidateIndex += 1
        if (
            $actualFeapp -ceq $expectedFeapp.ToLowerInvariant() -and
            $actualWebplayer -ceq $expectedWebplayer.ToLowerInvariant()
        ) {
            return [pscustomobject]@{
                Path = $candidate
                SelectionMode = 'auto_manifest_match'
                CandidateCount = $candidates.Count
            }
        }
    }
    $script:OfficialSourceDiagnostic = [ordered]@{
        schema_version = 'olivia.setup-source-diagnostic.v1'
        selection_mode = 'auto_ambiguous'
        candidate_count = $candidates.Count
        selected_official_id = $null
        client_version = $version
        manifest_feapp_sha256 = $expectedFeapp.ToLowerInvariant()
        manifest_webplayer_sha256 = $expectedWebplayer.ToLowerInvariant()
        candidates = $candidateDiagnostics.ToArray()
    }
    throw 'OFFICIAL_INSTALL_AMBIGUOUS'
}

function Assert-OfficialSource {
    param(
        [Parameter(Mandatory)]
        [string]$SourceRoot,
        [Parameter(Mandatory)]
        [string]$ManifestPath
    )

    try {
        $manifest = [IO.File]::ReadAllText($ManifestPath) | ConvertFrom-Json
        if (
            $manifest.schema_version -ne 'olivia.full-patch.v2' -or
            $manifest.steam_app_id -ne '4532590' -or
            $manifest.patch_mode -ne 'isolated-copy' -or
            $manifest.client_version -isnot [string] -or
            $manifest.feapp_sha256 -cnotmatch '^[0-9a-fA-F]{64}$' -or
            $manifest.webplayer_sha256 -cnotmatch '^[0-9a-fA-F]{64}$'
        ) {
            throw 'PATCH_MANIFEST_INVALID'
        }
    } catch {
        if ([string]$_.Exception.Message -eq 'PATCH_MANIFEST_INVALID') { throw }
        throw 'PATCH_MANIFEST_INVALID'
    }
    $versionRoot = Join-Path $SourceRoot $manifest.client_version
    $resources = Join-Path $versionRoot 'resources'
    $launcher = Join-Path $SourceRoot 'launcher.exe'
    $client = Join-Path $versionRoot 'Olivia.exe'
    $feapp = Join-Path $resources 'feapp.dat'
    $webplayer = Join-Path $resources 'webplayer.dat'
    foreach ($required in @($launcher, $client, $feapp, $webplayer)) {
        if (-not [IO.File]::Exists($required)) { throw 'OFFICIAL_INSTALL_NOT_FOUND' }
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
        [string]$ProductRoot,
        [Parameter(Mandatory)]
        [string]$RuntimePath
    )

    try {
        $productFull = [IO.Path]::GetFullPath($ProductRoot)
        $volumeRoot = [IO.Path]::GetPathRoot($productFull)
        if (
            -not $volumeRoot -or
            [string]::Equals($productFull.TrimEnd('\'), $volumeRoot.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)
        ) {
            throw 'OFFLINE_CORE_RUNTIME_PARENT_INVALID'
        }
        $runtimeParent = Join-Path $productFull 'runtime'
        $expectedRuntime = Join-Path $runtimeParent 'python-3.12.10-embed-amd64'
        if (-not [string]::Equals([IO.Path]::GetFullPath($RuntimePath), $expectedRuntime, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'OFFLINE_CORE_RUNTIME_PARENT_INVALID'
        }
        Assert-NoReparsePointsInPath -LiteralPath $expectedRuntime
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

function Get-PcmWaveMetadata {
    param([Parameter(Mandatory)][string]$Path)
    try {
        $bytes = [IO.File]::ReadAllBytes($Path)
        if ($bytes.Length -lt 44 -or [Text.Encoding]::ASCII.GetString($bytes, 0, 4) -cne 'RIFF' -or
            [Text.Encoding]::ASCII.GetString($bytes, 8, 4) -cne 'WAVE' -or [int64][BitConverter]::ToUInt32($bytes, 4) + 8 -ne $bytes.Length) { throw 'invalid wave container' }
        $offset = 12; $format = $null; $dataBytes = $null
        while ($offset + 8 -le $bytes.Length) {
            $chunk = [Text.Encoding]::ASCII.GetString($bytes, $offset, 4)
            $length = [int64][BitConverter]::ToUInt32($bytes, $offset + 4); $body = $offset + 8; $next = $body + $length + ($length % 2)
            if ($length -gt [int]::MaxValue -or $next -gt $bytes.Length) { throw 'invalid wave chunk' }
            if ($chunk -ceq 'fmt ') {
                if ($null -ne $format -or $length -lt 16) { throw 'invalid wave format' }
                $format = @([BitConverter]::ToUInt16($bytes, $body), [BitConverter]::ToUInt16($bytes, $body + 2),
                    [BitConverter]::ToUInt32($bytes, $body + 4), [BitConverter]::ToUInt32($bytes, $body + 8),
                    [BitConverter]::ToUInt16($bytes, $body + 12), [BitConverter]::ToUInt16($bytes, $body + 14))
            } elseif ($chunk -ceq 'data') {
                if ($null -ne $dataBytes) { throw 'duplicate wave data' }
                $dataBytes = $length
            }
            $offset = [int]$next
        }
        if ($offset -ne $bytes.Length -or $null -eq $format -or $null -eq $dataBytes -or
            $format[0] -ne 1 -or $format[1] -ne 1 -or $format[2] -ne 16000 -or
            $format[3] -ne 32000 -or $format[4] -ne 2 -or $format[5] -ne 16 -or
            $dataBytes -lt 2 -or ($dataBytes % 2) -ne 0) { throw 'unsupported wave format' }
        return [ordered]@{ channels = 1; sample_width_bytes = 2; sample_rate_hz = 16000; frame_count = [int64]($dataBytes / 2); compression_type = 'NONE' }
    } catch { throw 'VOICE_REFERENCE_INVALID' }
}

function Set-DurableTransactionState {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$State)
    $next = "$Path.next"; [IO.File]::WriteAllText($next, $State, [Text.UTF8Encoding]::new($false)); [IO.File]::Move($next, $Path)
}

function Repair-ManagedVoiceTransaction {
    param([Parameter(Mandatory)][string]$SharedRoot)
    $journal = Join-Path $SharedRoot '.linli-reference.transaction'; $publishMarker = "$journal.publish"; $rollbackMarker = "$journal.rollback"; $cleanupMarker = "$journal.cleanup"
    try { foreach ($next in @("$journal.next", "$publishMarker.next")) { [IO.File]::Delete($next) } } catch { throw 'VOICE_REFERENCE_INSTALL_CLEANUP_FAILED' }
    if ((@($publishMarker, $rollbackMarker, $cleanupMarker) | Where-Object { [IO.File]::Exists($_) }).Count -gt 1) { throw 'VOICE_REFERENCE_INSTALL_ROLLBACK_FAILED' }
    $marker = if ([IO.File]::Exists($cleanupMarker)) { $cleanupMarker } elseif ([IO.File]::Exists($rollbackMarker)) { $rollbackMarker } elseif ([IO.File]::Exists($publishMarker)) { $publishMarker } elseif ([IO.File]::Exists($journal)) { $journal } else {
        try { foreach ($orphan in [IO.Directory]::EnumerateFiles($SharedRoot, '.linli-reference.*')) { if ([IO.Path]::GetFileName($orphan) -cmatch '^\.linli-reference\.[0-9a-f]{32}\.(wav|json)\.(tmp|bak)$') { [IO.File]::Delete($orphan) } } }
        catch { throw 'VOICE_REFERENCE_INSTALL_CLEANUP_FAILED' }
        return
    }
    $phase = if ($marker -ceq $cleanupMarker) { 'cleanup' } elseif ($marker -ceq $rollbackMarker) { 'rollback' } elseif ($marker -ceq $publishMarker) { 'publish' } else { 'staging' }
    $errorCode = if ($phase -ceq 'cleanup') { 'VOICE_REFERENCE_INSTALL_CLEANUP_FAILED' } else { 'VOICE_REFERENCE_INSTALL_ROLLBACK_FAILED' }
    try {
        $state = [IO.File]::ReadAllText($marker)
        if ($state -cnotmatch '^(?<id>[0-9a-f]{32})\|(?<target>[01])\|(?<manifest>[01])$') { throw 'invalid voice transaction' }
        $id = $Matches.id; $hadTarget = $Matches.target -ceq '1'; $hadManifest = $Matches.manifest -ceq '1'
        $target = Join-Path $SharedRoot 'linli-reference.wav'; $manifest = Join-Path $SharedRoot 'linli-reference.json'
        $entries = @(
            @{ Active = $target; Staged = Join-Path $SharedRoot ".linli-reference.$id.wav.tmp"; Backup = Join-Path $SharedRoot ".linli-reference.$id.wav.bak"; Had = $hadTarget },
            @{ Active = $manifest; Staged = Join-Path $SharedRoot ".linli-reference.$id.json.tmp"; Backup = Join-Path $SharedRoot ".linli-reference.$id.json.bak"; Had = $hadManifest }
        )
        foreach ($entry in $entries) {
            foreach ($path in @($entry.Active, $entry.Staged, $entry.Backup, $marker)) { if ((Test-Path -LiteralPath $path) -and (([IO.File]::GetAttributes($path) -band [IO.FileAttributes]::ReparsePoint) -ne 0)) { throw 'unsafe voice transaction' } }
            if ($phase -ceq 'publish' -and -not [IO.File]::Exists($entry.Staged)) {
                if ($entry.Had) {
                    if (-not [IO.File]::Exists($entry.Backup)) { throw 'missing voice rollback' }
                    [IO.File]::Copy($entry.Backup, $entry.Active, $true)
                } else { [IO.File]::Delete($entry.Active) }
            }
        }
        if ($phase -ceq 'publish') { [IO.File]::Move($publishMarker, $rollbackMarker); $marker = $rollbackMarker; $phase = 'rollback' }
        foreach ($entry in $entries) { [IO.File]::Delete($entry.Staged); [IO.File]::Delete($entry.Backup) }
        [IO.File]::Delete($marker)
        [IO.File]::Delete($journal)
    } catch { throw $errorCode }
}

function Install-ManagedVoiceReference {
    param([AllowNull()][object]$VoiceReference, [Parameter(Mandatory)][string]$InstallRoot)
    $sharedRoot = Join-Path $InstallRoot 'data\capabilities\video\shared'
    Assert-NoReparsePointsInPath -LiteralPath $sharedRoot -ErrorCode 'VOICE_REFERENCE_INSTALL_PATH_INVALID'
    if ($null -eq $VoiceReference) { return }
    $source = [string]$VoiceReference.path; $expectedHash = [string]$VoiceReference.sha256
    $expectedSize = [int64]$VoiceReference.size_bytes; $wave = $VoiceReference.wave
    if (-not [IO.File]::Exists($source)) { throw 'VOICE_REFERENCE_MISSING' }
    foreach ($field in @('channels', 'sample_width_bytes', 'sample_rate_hz', 'frame_count')) { if ($wave.$field -isnot [int] -and $wave.$field -isnot [long]) { throw 'VOICE_REFERENCE_INVALID' } }
    if ($null -eq $wave -or [int]$wave.channels -ne 1 -or [int]$wave.sample_width_bytes -ne 2 -or
        [int]$wave.sample_rate_hz -ne 16000 -or [int64]$wave.frame_count -lt 1 -or
        $wave.compression_type -isnot [string] -or [string]$wave.compression_type -cne 'NONE') { throw 'VOICE_REFERENCE_INVALID' }
    if ($expectedSize -lt 1 -or [IO.FileInfo]::new($source).Length -ne $expectedSize -or (Get-Sha256 -LiteralPath $source) -cne $expectedHash) { throw 'VOICE_REFERENCE_HASH_MISMATCH' }

    New-Item -ItemType Directory -Force -Path $sharedRoot | Out-Null
    $target = Join-Path $sharedRoot 'linli-reference.wav'; $manifestPath = Join-Path $sharedRoot 'linli-reference.json'
    foreach ($leaf in @($target, $manifestPath)) { if ((Test-Path -LiteralPath $leaf) -and (([IO.File]::GetAttributes($leaf) -band [IO.FileAttributes]::ReparsePoint) -ne 0)) { throw 'VOICE_REFERENCE_INSTALL_PATH_INVALID' } }
    $transactionId = [guid]::NewGuid().ToString('N')
    $transactionRoot = Join-Path $sharedRoot ('.linli-reference.' + $transactionId)
    $stagedTarget = "$transactionRoot.wav.tmp"; $stagedManifest = "$transactionRoot.json.tmp"
    $targetBackup = "$transactionRoot.wav.bak"; $manifestBackup = "$transactionRoot.json.bak"
    $journal = Join-Path $sharedRoot '.linli-reference.transaction'; $cleanupMarker = "$journal.cleanup"
    $utf8NoBom = [Text.UTF8Encoding]::new($false); $state = "$transactionId|$([int][IO.File]::Exists($target))|$([int][IO.File]::Exists($manifestPath))"
    try {
        Set-DurableTransactionState -Path $journal -State $state
        [IO.File]::Copy($source, $stagedTarget, $false)
        if ([IO.FileInfo]::new($stagedTarget).Length -ne $expectedSize -or (Get-Sha256 -LiteralPath $stagedTarget) -cne $expectedHash) { throw 'VOICE_REFERENCE_HASH_MISMATCH' }
        $actualWave = Get-PcmWaveMetadata -Path $stagedTarget
        foreach ($field in @('channels', 'sample_width_bytes', 'sample_rate_hz', 'frame_count', 'compression_type')) { if ($actualWave.$field -cne $wave.$field) { throw 'VOICE_REFERENCE_INVALID' } }
        $integrity = [ordered]@{ schema_version = 'olivia.managed-voice-reference.v1'; path = 'linli-reference.wav'; size_bytes = $expectedSize; sha256 = $expectedHash
            wave = [ordered]@{ channels = [int]$wave.channels; sample_width_bytes = [int]$wave.sample_width_bytes; sample_rate_hz = [int]$wave.sample_rate_hz; frame_count = [int64]$wave.frame_count; compression_type = [string]$wave.compression_type } }
        [IO.File]::WriteAllText($stagedManifest, ($integrity | ConvertTo-Json -Compress), $utf8NoBom)
        Set-DurableTransactionState -Path "$journal.publish" -State $state
        if ([IO.File]::Exists($target)) { [IO.File]::Replace($stagedTarget, $target, $targetBackup) } else { [IO.File]::Move($stagedTarget, $target) }
        if ([IO.File]::Exists($manifestPath)) { [IO.File]::Replace($stagedManifest, $manifestPath, $manifestBackup) } else { [IO.File]::Move($stagedManifest, $manifestPath) }
        [IO.File]::Move("$journal.publish", $cleanupMarker)
    } catch {
        $failure = $_.Exception.Message
        try {
            if ([IO.File]::Exists($journal) -or [IO.File]::Exists("$journal.publish") -or [IO.File]::Exists("$journal.rollback") -or [IO.File]::Exists($cleanupMarker)) { Repair-ManagedVoiceTransaction -SharedRoot $sharedRoot }
            else { foreach ($cleanup in @($stagedTarget, $stagedManifest, $targetBackup, $manifestBackup)) { [IO.File]::Delete($cleanup) } }
        } catch { throw $_.Exception.Message }
        if ($failure -in @('VOICE_REFERENCE_HASH_MISMATCH', 'VOICE_REFERENCE_INSTALL_PATH_INVALID', 'VOICE_REFERENCE_INVALID', 'VOICE_REFERENCE_INSTALL_CLEANUP_FAILED')) { throw $failure }
        throw 'VOICE_REFERENCE_INSTALL_FAILED'
    }
}

function Get-ManagedInstallTransactionNames {
    return @('local_backend', 'launcher', 'START.cmd', 'START.vbs', 'CONFIGURE.cmd', 'UNINSTALL.cmd', '.olivia-full-patch.json')
}

function New-ManagedInstallRollbackSnapshot {
    param([Parameter(Mandatory)][string]$InstallRoot, [Parameter(Mandatory)][string]$Snapshot)
    if (-not (Test-Path -LiteralPath $InstallRoot)) { return }
    if (-not [IO.Directory]::Exists($InstallRoot) -or (([IO.File]::GetAttributes($InstallRoot) -band [IO.FileAttributes]::ReparsePoint) -ne 0)) { throw 'INSTALL_TRANSACTION_SNAPSHOT_FAILED' }
    try {
        New-Item -ItemType Directory -Path $Snapshot | Out-Null
        foreach ($name in Get-ManagedInstallTransactionNames) {
            $source = Join-Path $InstallRoot $name
            if ([IO.Directory]::Exists($source)) {
                if (-not (Test-NoReparsePointsInTree -LiteralPath $source)) { throw 'INSTALL_TRANSACTION_SNAPSHOT_FAILED' }
                Copy-Item -LiteralPath $source -Destination (Join-Path $Snapshot $name) -Recurse -Force
            } elseif ([IO.File]::Exists($source)) {
                if (([IO.File]::GetAttributes($source) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'INSTALL_TRANSACTION_SNAPSHOT_FAILED' }
                [IO.File]::Copy($source, (Join-Path $Snapshot $name), $false)
            } elseif (Test-Path -LiteralPath $source) { throw 'INSTALL_TRANSACTION_SNAPSHOT_FAILED' }
        }
    } catch { throw 'INSTALL_TRANSACTION_SNAPSHOT_FAILED' }
}

function Restore-ManagedInstallRollbackSnapshot {
    param([Parameter(Mandatory)][string]$InstallRoot, [Parameter(Mandatory)][bool]$InstallRootExisted, [string]$Snapshot = '')
    if (-not $InstallRootExisted) {
        if (Test-Path -LiteralPath $InstallRoot) { Remove-Item -LiteralPath $InstallRoot -Recurse -Force }
        return
    }
    if (-not $Snapshot -or -not [IO.Directory]::Exists($Snapshot)) { throw 'VOICE_REFERENCE_INSTALL_ROLLBACK_FAILED' }
    foreach ($name in Get-ManagedInstallTransactionNames) {
        $active = Join-Path $InstallRoot $name
        if (Test-Path -LiteralPath $active) { Remove-Item -LiteralPath $active -Recurse -Force }
        $backup = Join-Path $Snapshot $name
        if ([IO.Directory]::Exists($backup)) { Copy-Item -LiteralPath $backup -Destination $active -Recurse -Force }
        elseif ([IO.File]::Exists($backup)) { [IO.File]::Copy($backup, $active, $true) }
        elseif (Test-Path -LiteralPath $backup) { throw 'VOICE_REFERENCE_INSTALL_ROLLBACK_FAILED' }
    }
}

function Restore-ManagedRuntimeTransaction {
    param([Parameter(Mandatory)][string]$RuntimeRoot, [string]$RuntimeBackup = '', [Parameter(Mandatory)][bool]$RuntimeRootExisted)
    if ($RuntimeBackup -and [IO.Directory]::Exists($RuntimeBackup)) {
        if (Test-Path -LiteralPath $RuntimeRoot) { Remove-Item -LiteralPath $RuntimeRoot -Recurse -Force }
        [IO.Directory]::Move($RuntimeBackup, $RuntimeRoot)
    } elseif (-not $RuntimeRootExisted -and (Test-Path -LiteralPath $RuntimeRoot)) {
        Remove-Item -LiteralPath $RuntimeRoot -Recurse -Force
    }
}

function Remove-ManagedInstallRollbackSnapshot {
    param([string]$Snapshot = '')
    if ($Snapshot -and (Test-Path -LiteralPath $Snapshot)) { Remove-Item -LiteralPath $Snapshot -Recurse -Force }
}

function Repair-ManagedInstallTransaction {
    param([Parameter(Mandatory)][string]$ProductRoot, [Parameter(Mandatory)][string]$InstallRoot, [Parameter(Mandatory)][string]$RuntimeRoot); if (-not [IO.Directory]::Exists($ProductRoot)) { return }
    $journal = Join-Path $ProductRoot '.install.transaction'; $activeMarker = "$journal.active"; $rollbackMarker = "$journal.rollback"; $cleanupMarker = "$journal.cleanup"; try { foreach ($next in @("$journal.next", "$activeMarker.next")) { [IO.File]::Delete($next) } } catch { throw 'VOICE_REFERENCE_INSTALL_CLEANUP_FAILED' }
    if ((@($activeMarker, $rollbackMarker, $cleanupMarker) | Where-Object { [IO.File]::Exists($_) }).Count -gt 1) { throw 'VOICE_REFERENCE_INSTALL_ROLLBACK_FAILED' }
    if (-not [IO.File]::Exists($journal) -and ([IO.File]::Exists($activeMarker) -or [IO.File]::Exists($rollbackMarker) -or [IO.File]::Exists($cleanupMarker))) { throw 'VOICE_REFERENCE_INSTALL_ROLLBACK_FAILED' }
    if (-not [IO.File]::Exists($journal)) { $shared = Join-Path $InstallRoot 'data\capabilities\video\shared'; if ([IO.Directory]::Exists($shared)) { Repair-ManagedVoiceTransaction -SharedRoot $shared }; return }
    try {
        $marker = if ([IO.File]::Exists($cleanupMarker)) { $cleanupMarker } elseif ([IO.File]::Exists($rollbackMarker)) { $rollbackMarker } elseif ([IO.File]::Exists($activeMarker)) { $activeMarker } else { $journal }; $phase = if ($marker -ceq $cleanupMarker) { 'cleanup' } elseif ($marker -ceq $rollbackMarker) { 'rollback' } elseif ($marker -ceq $activeMarker) { 'active' } else { 'prepare' }
        $state = [IO.File]::ReadAllText($marker)
        if ($state -cnotmatch '^(?<id>[0-9a-f]{32})\|(?<install>[01])\|(?<runtime>[01])$') { throw 'invalid install transaction' }
        $id = $Matches.id; $installExisted = $Matches.install -ceq '1'; $runtimeExisted = $Matches.runtime -ceq '1'
        $snapshot = Join-Path $ProductRoot ".install.rollback.$id"; $staging = "$RuntimeRoot.staging.$id"; $backup = "$RuntimeRoot.backup.$id"; $shared = Join-Path $InstallRoot 'data\capabilities\video\shared'
        $committed = $phase -ceq 'cleanup' -or [IO.File]::Exists((Join-Path $shared '.linli-reference.transaction.cleanup'))
        if ($committed -and $phase -eq 'active') { [IO.File]::Move($activeMarker, $cleanupMarker); $marker = $cleanupMarker; $phase = 'cleanup' }
        if ($phase -ceq 'active') {
            if ([IO.Directory]::Exists($shared)) { Repair-ManagedVoiceTransaction -SharedRoot $shared }
            Restore-ManagedInstallRollbackSnapshot -InstallRoot $InstallRoot -InstallRootExisted $installExisted -Snapshot $snapshot
            Restore-ManagedRuntimeTransaction -RuntimeRoot $RuntimeRoot -RuntimeBackup $backup -RuntimeRootExisted $runtimeExisted
            [IO.File]::Move($activeMarker, $rollbackMarker); $marker = $rollbackMarker; $phase = 'rollback'
        } elseif ($phase -ceq 'cleanup' -and [IO.Directory]::Exists($shared)) { Repair-ManagedVoiceTransaction -SharedRoot $shared }
        foreach ($path in @($staging, $backup)) { if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force } }
        Remove-ManagedInstallRollbackSnapshot -Snapshot $snapshot
        if ($marker -cne $journal) { [IO.File]::Delete($marker) }
        [IO.File]::Delete($journal)
    } catch { if ($phase -ceq 'cleanup' -or $committed) { throw 'VOICE_REFERENCE_INSTALL_CLEANUP_FAILED' }; throw 'VOICE_REFERENCE_INSTALL_ROLLBACK_FAILED' }
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
    $manifestNames = @('schema_version', 'python_runtime', 'pip_bootstrap', 'requirements_sha256', 'wheels')
    $hasVoiceReference = $manifest.PSObject.Properties.Name -ccontains 'voice_reference'
    $hasVideoRuntime = $manifest.PSObject.Properties.Name -ccontains 'video_runtime'
    $hasDistribution = $manifest.PSObject.Properties.Name -ccontains 'distribution'
    if (
        $hasVideoRuntime -ne $hasVoiceReference -or
        $hasVoiceReference -ne $hasDistribution -or
        ($hasDistribution -and $manifest.distribution -cne 'private')
    ) {
        throw 'VOICE_REFERENCE_PRIVATE_MANIFEST_REQUIRED'
    }
    if ($hasVoiceReference) { $manifestNames += @('distribution', 'voice_reference', 'video_runtime') }
    Assert-OfflineObjectShape -Value $manifest -Names $manifestNames
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
    $voiceReference = $null
    $videoRuntime = $null
    if ($hasVoiceReference) {
        Assert-OfflineObjectShape -Value $manifest.voice_reference -Names @('path', 'size_bytes', 'sha256', 'wave')
        Assert-OfflineObjectShape -Value $manifest.voice_reference.wave -Names @('channels', 'sample_width_bytes', 'sample_rate_hz', 'frame_count', 'compression_type')
        if ($manifest.voice_reference.path -cne 'voice/olivia-reference.wav') {
            throw 'OFFLINE_CORE_MANIFEST_INVALID'
        }
        try {
            $voiceReferencePath = Resolve-OfflineAsset -Root $Root -Asset $manifest.voice_reference
        } catch {
            if ($_.Exception.Message -eq 'OFFLINE_CORE_ASSET_MISSING') {
                throw 'VOICE_REFERENCE_MISSING'
            }
            if ($_.Exception.Message -in @('OFFLINE_CORE_ASSET_SIZE_MISMATCH', 'OFFLINE_CORE_ASSET_HASH_MISMATCH')) {
                throw 'VOICE_REFERENCE_HASH_MISMATCH'
            }
            throw 'VOICE_REFERENCE_INVALID'
        }
        $voiceReference = $manifest.voice_reference
        $voiceReference.path = $voiceReferencePath
        Assert-OfflineObjectShape -Value $manifest.video_runtime -Names @('path', 'size_bytes', 'sha256')
        if ($manifest.video_runtime.path -cne 'video-runtime/Olivia-video-runtime-private.zip') {
            throw 'OFFLINE_CORE_MANIFEST_INVALID'
        }
        $videoRuntime = Resolve-OfflineAsset -Root $Root -Asset $manifest.video_runtime
    }
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
        VoiceReference = $voiceReference
        VideoRuntime = $videoRuntime
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

function Register-VerifiedMemoryRuntime {
    param(
        [Parameter(Mandatory)]
        [string]$CandidateExe,
        [Parameter(Mandatory)]
        [string]$PthPath,
        [Parameter(Mandatory)]
        [string]$MemoryRuntimePath,
        [Parameter(Mandatory)]
        [string]$RequirementsPath,
        [Parameter(Mandatory)]
        [string]$VerifierPath
    )

    if (
        -not [IO.File]::Exists($CandidateExe) -or
        -not [IO.File]::Exists($PthPath) -or
        -not [IO.Directory]::Exists($MemoryRuntimePath) -or
        -not [IO.File]::Exists($RequirementsPath) -or
        -not [IO.File]::Exists($VerifierPath)
    ) {
        return $false
    }
    try {
        Assert-NoReparsePointsInPath -LiteralPath $MemoryRuntimePath
        if (-not (Test-NoReparsePointsInTree -LiteralPath $MemoryRuntimePath)) {
            return $false
        }
        & $CandidateExe $VerifierPath $MemoryRuntimePath $RequirementsPath *> $null
        if ($LASTEXITCODE -ne 0) { return $false }
        Update-ManagedPythonPath -PthPath $PthPath -MemoryRuntimePath $MemoryRuntimePath
        return $true
    } catch {
        return $false
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

Assert-ManagedRuntimeParent -ProductRoot $productRoot -RuntimePath $runtimeRoot
$selectedOfficial = $OfficialRoot
if (-not $selectedOfficial -and -not $NonInteractive) {
    $selectedOfficial = Read-Host 'Steam 游戏目录（留空则按 AppID 自动发现）'
}
$manifestPath = Join-Path $PayloadRoot 'installer\full-patch-manifest.json'
$selectedOfficial = Resolve-OfficialInstall -RequestedRoot $selectedOfficial -ManifestPath $manifestPath
$officialSelection = $selectedOfficial
$selectedOfficial = [string]$officialSelection.Path
Assert-NoReparsePointsInPath -LiteralPath $selectedOfficial -ErrorCode 'OFFICIAL_INSTALL_PATH_REPARSE_POINT'
if (Test-PathsOverlap -Left $productRoot -Right $selectedOfficial) {
    throw 'INSTALL_ROOT_OVERLAPS_OFFICIAL'
}
$script:OfficialSourceDiagnostic = New-OfficialSourceDiagnostic -Selection $officialSelection -ManifestPath $manifestPath
Write-SetupDiagnosticResult -Diagnostic $script:OfficialSourceDiagnostic
Assert-OfficialSource -SourceRoot $selectedOfficial -ManifestPath $manifestPath
$coreAssets = Get-OfflineCoreAssets -Root $offlineRoot -ManifestPath $offlineManifestPath -RequirementsPath $requirements
Repair-ManagedInstallTransaction -ProductRoot $productRoot -InstallRoot $Destination -RuntimeRoot $runtimeRoot
New-Item -ItemType Directory -Force -Path $productRoot | Out-Null
$installRootExisted = [IO.Directory]::Exists($Destination); $runtimeRootExisted = Test-Path -LiteralPath $runtimeRoot
$installTransactionId = [guid]::NewGuid().ToString('N')
$installTransaction = Join-Path $productRoot '.install.transaction'; $installRollbackSnapshot = Join-Path $productRoot ".install.rollback.$installTransactionId"
$runtimeStaging = "$runtimeRoot.staging.$installTransactionId"
$runtimeBackup = "$runtimeRoot.backup.$installTransactionId"
$installState = "$installTransactionId|$([int]$installRootExisted)|$([int]$runtimeRootExisted)"
Set-DurableTransactionState -Path $installTransaction -State $installState
New-ManagedInstallRollbackSnapshot -InstallRoot $Destination -Snapshot $installRollbackSnapshot
Set-DurableTransactionState -Path "$installTransaction.active" -State $installState
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
    $existingMemoryRuntime = Join-Path $productRoot 'runtime\mem0-site-packages'
    $memoryRuntimeRequirements = Join-Path $PayloadRoot 'installer\mem0-runtime-requirements.txt'
    $memoryRuntimeVerifier = Join-Path $PayloadRoot 'installer\verify_mem0_runtime.py'
    [void](Register-VerifiedMemoryRuntime -CandidateExe $candidateExe -PthPath $pth.FullName -MemoryRuntimePath $existingMemoryRuntime -RequirementsPath $memoryRuntimeRequirements -VerifierPath $memoryRuntimeVerifier)
    if (Test-Path -LiteralPath $runtimeRoot) {
        if (
            -not [IO.Directory]::Exists($runtimeRoot) -or
            (([IO.File]::GetAttributes($runtimeRoot) -band [IO.FileAttributes]::ReparsePoint) -ne 0)
        ) {
            throw 'OFFLINE_CORE_RUNTIME_INVALID'
        }
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
} catch {
    Repair-ManagedInstallTransaction -ProductRoot $productRoot -InstallRoot $Destination -RuntimeRoot $runtimeRoot
    throw
}
$runner = @{ File = $runtimeExe; Args = @() }

$arguments = @('install', '--payload', $PayloadRoot, '--destination', $Destination, '--manifest', $manifestPath, '--port', $Port, '--official-root', $selectedOfficial)
$oldPythonPath = if ($env:PYTHONPATH) { $env:PYTHONPATH } else { '' }
$env:PYTHONPATH = $PayloadRoot + [IO.Path]::PathSeparator + $oldPythonPath
$bootstrap = Join-Path $PayloadRoot 'installer\bootstrap_install.py'
$installOutput = @(& $runner.File @($runner.Args + @($bootstrap, $PayloadRoot) + $arguments))
$installExitCode = $LASTEXITCODE
if ($installExitCode -ne 0) {
    Repair-ManagedInstallTransaction -ProductRoot $productRoot -InstallRoot $Destination -RuntimeRoot $runtimeRoot
    $installCode = 'SETUP_INSTALL_FAILED'
    foreach ($line in $installOutput) {
        try {
            $record = $line | ConvertFrom-Json
            if (
                $record.status -eq 'ERROR' -and
                $record.code -is [string] -and
                $record.code -cmatch '^[A-Z][A-Z0-9_]{3,95}$'
            ) {
                $installCode = $record.code
            }
        } catch {
            # Ignore non-JSON child output; it must never reach the setup log.
        }
    }
    Write-SetupErrorResult -Code $installCode
    if (-not $SetupResultPath) { $installOutput | Write-Output }
    exit $installExitCode
}
try {
    Install-ManagedVoiceReference -VoiceReference $coreAssets.VoiceReference -InstallRoot $Destination
} catch {
    $voiceFailure = [string]$_.Exception.Message
    try {
        Repair-ManagedInstallTransaction -ProductRoot $productRoot -InstallRoot $Destination -RuntimeRoot $runtimeRoot
    } catch { throw 'VOICE_REFERENCE_INSTALL_ROLLBACK_FAILED' }
    throw $voiceFailure
}

try { [IO.File]::Move("$installTransaction.active", "$installTransaction.cleanup"); Repair-ManagedInstallTransaction -ProductRoot $productRoot -InstallRoot $Destination -RuntimeRoot $runtimeRoot }
catch { throw 'VOICE_REFERENCE_INSTALL_CLEANUP_FAILED' }
if (-not $SetupResultPath) { $installOutput | Write-Output }

$LASTEXITCODE = 0

if (-not $SkipShortcut) {
    & (Join-Path $PSScriptRoot 'Create-Shortcut.ps1') -InstallRoot $Destination
}
exit 0
