[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallRoot,
    [string]$ShortcutPath,
    [switch]$RefreshExisting,
    [switch]$RemoveExisting
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $InstallRoot).Path
$start = Join-Path $root 'START.cmd'
$startHidden = Join-Path $root 'START.vbs'
$startHiddenTemplate = Join-Path $PSScriptRoot 'start_hidden.vbs.txt'
$wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
$hiddenArguments = '//B //Nologo "' + $startHidden + '"'

if ($RemoveExisting) {
    $shortcutPaths = if ($ShortcutPath) { @($ShortcutPath) } else {
        @(foreach ($folderName in @('Desktop', 'Programs')) {
            try {
                $folder = [Environment]::GetFolderPath($folderName)
                if ($folder) {
                    $candidate = Join-Path $folder 'Olivia 本地版.lnk'
                    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                        $candidate
                    }
                }
            }
            catch {
                # One unavailable shell folder must not suppress the other.
            }
        })
    }
    $shell = New-Object -ComObject WScript.Shell
    foreach ($path in $shortcutPaths) {
        try {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
            $shortcut = $shell.CreateShortcut($path)
            $target = [IO.Path]::GetFullPath([string]$shortcut.TargetPath)
            $isLegacyShortcut = [string]::Equals(
                $target, [IO.Path]::GetFullPath($start), [StringComparison]::OrdinalIgnoreCase
            )
            $isHiddenShortcut = [string]::Equals(
                $target, [IO.Path]::GetFullPath($wscript), [StringComparison]::OrdinalIgnoreCase
            ) -and [string]::Equals(
                [string]$shortcut.Arguments, $hiddenArguments, [StringComparison]::OrdinalIgnoreCase
            )
            if ($isLegacyShortcut -or $isHiddenShortcut) {
                Remove-Item -LiteralPath $path -Force
            }
        }
        catch {
            # Shortcut cleanup is best-effort and must not block safe uninstall.
        }
    }
    [pscustomobject]@{ status = 'SHORTCUTS_REMOVED' }
    return
}

$markerPath = Join-Path $root '.olivia-full-patch.json'
if (-not (Test-Path -LiteralPath $start -PathType Leaf)) {
    throw 'LOCAL_START_ENTRYPOINT_NOT_FOUND'
}
if (-not (Test-Path -LiteralPath $startHiddenTemplate -PathType Leaf)) {
    throw 'LOCAL_HIDDEN_START_TEMPLATE_NOT_FOUND'
}
if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
    throw 'PATCH_MARKER_NOT_FOUND'
}

$marker = Get-Content -Raw -LiteralPath $markerPath | ConvertFrom-Json
$client = Join-Path $root (Join-Path 'app' (Join-Path ([string]$marker.client_version) 'Olivia.exe'))
if (-not (Test-Path -LiteralPath $client -PathType Leaf)) {
    throw 'ISOLATED_CLIENT_NOT_FOUND'
}

function Resolve-OliviaIcon {
    $statePath = Join-Path $root '.olivia-update-state.json'
    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        try {
            $state = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
            $payloadPath = [string]$state.active_components.local_backend.payload_path
            if ($payloadPath -and -not [IO.Path]::IsPathRooted($payloadPath) -and
                ($payloadPath -split '[\\/]') -notcontains '..') {
                $activeIcon = Join-Path $root (Join-Path ($payloadPath -replace '/', '\') 'installer\assets\olivia.ico')
                if (Test-Path -LiteralPath $activeIcon -PathType Leaf) {
                    return (Resolve-Path -LiteralPath $activeIcon).Path
                }
            }
        }
        catch {
            # Fall back to the initial packaged icon or client executable.
        }
    }
    $packagedIcon = Join-Path $root 'local_backend\installer\assets\olivia.ico'
    if (Test-Path -LiteralPath $packagedIcon -PathType Leaf) {
        return (Resolve-Path -LiteralPath $packagedIcon).Path
    }
    return $client
}

$icon = Resolve-OliviaIcon

$hiddenLauncher = [IO.File]::ReadAllText($startHiddenTemplate, [Text.Encoding]::UTF8)
$hiddenLauncherTemp = $startHidden + '.tmp-' + [Guid]::NewGuid().ToString('N')
try {
    [IO.File]::WriteAllText($hiddenLauncherTemp, $hiddenLauncher, [Text.Encoding]::Unicode)
    Move-Item -LiteralPath $hiddenLauncherTemp -Destination $startHidden -Force
}
finally {
    if (Test-Path -LiteralPath $hiddenLauncherTemp -PathType Leaf) {
        Remove-Item -LiteralPath $hiddenLauncherTemp -Force
    }
}

if ($RefreshExisting) {
    $shortcutPaths = @()
    if ($ShortcutPath) {
        try {
            if (Test-Path -LiteralPath $ShortcutPath -PathType Leaf) {
                $shortcutPaths += $ShortcutPath
            }
        }
        catch {
            # Explicit refresh is optional; leave the candidate list empty.
        }
    }
    else {
        foreach ($folderName in @('Desktop', 'Programs')) {
            try {
                $folder = [Environment]::GetFolderPath($folderName)
                if ($folder) {
                    $candidate = Join-Path $folder 'Olivia 本地版.lnk'
                    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                        $shortcutPaths += $candidate
                    }
                }
            }
            catch {
                # One unavailable shell folder must not suppress the other.
            }
        }
    }
}
elseif ($ShortcutPath) {
    $shortcutPaths = @($ShortcutPath)
}
else {
    $shortcutPaths = @(
        (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Olivia 本地版.lnk')
    )
}

$shell = New-Object -ComObject WScript.Shell
foreach ($path in $shortcutPaths) {
    try {
        $shortcutParent = Split-Path -Parent $path
        if ($shortcutParent) {
            New-Item -ItemType Directory -Force -Path $shortcutParent | Out-Null
        }
        $shortcut = $shell.CreateShortcut($path)
        if ($RefreshExisting) {
            $target = [IO.Path]::GetFullPath([string]$shortcut.TargetPath)
            $isLegacyShortcut = [string]::Equals(
                $target,
                [IO.Path]::GetFullPath($start),
                [StringComparison]::OrdinalIgnoreCase
            )
            $isHiddenShortcut = [string]::Equals(
                $target,
                [IO.Path]::GetFullPath($wscript),
                [StringComparison]::OrdinalIgnoreCase
            ) -and [string]::Equals(
                [string]$shortcut.Arguments,
                $hiddenArguments,
                [StringComparison]::OrdinalIgnoreCase
            )
            if (-not ($isLegacyShortcut -or $isHiddenShortcut)) {
                continue
            }
        }
        $shortcut.TargetPath = $wscript
        $shortcut.Arguments = $hiddenArguments
        $shortcut.WorkingDirectory = $root
        $shortcut.Description = 'Olivia local client and backend'
        $shortcut.IconLocation = "$icon,0"
        $shortcut.Save()
    }
    catch {
        if (-not $RefreshExisting) {
            throw
        }
        # Refresh every other discovered shortcut even if one cannot be saved.
    }
}

$resultStatus = if ($RefreshExisting) { 'SHORTCUTS_REFRESHED' } else { 'SHORTCUT_CREATED' }
$resultShortcut = if (@($shortcutPaths).Count -eq 1) { [string]$shortcutPaths[0] } else { $null }
[pscustomobject]@{
    status = $resultStatus
    shortcut = $resultShortcut
    shortcuts = @($shortcutPaths)
} | ConvertTo-Json -Compress
