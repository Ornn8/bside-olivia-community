[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallRoot,
    [string]$ShortcutPath,
    [switch]$RefreshExisting
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $InstallRoot).Path
$start = Join-Path $root 'START.cmd'
$markerPath = Join-Path $root '.olivia-full-patch.json'
if (-not (Test-Path -LiteralPath $start -PathType Leaf)) {
    throw 'LOCAL_START_ENTRYPOINT_NOT_FOUND'
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
$shell = New-Object -ComObject WScript.Shell

if ($RefreshExisting) {
    if ($ShortcutPath) {
        $shortcutPaths = @($ShortcutPath) | Where-Object {
            Test-Path -LiteralPath $_ -PathType Leaf
        }
    }
    else {
        $shortcutPaths = @(
            (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Olivia 本地版.lnk'),
            (Join-Path ([Environment]::GetFolderPath('Programs')) 'Olivia 本地版.lnk')
        ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }
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

foreach ($path in $shortcutPaths) {
    $shortcutParent = Split-Path -Parent $path
    if ($shortcutParent) {
        New-Item -ItemType Directory -Force -Path $shortcutParent | Out-Null
    }
    $shortcut = $shell.CreateShortcut($path)
    if (-not $RefreshExisting) {
        $shortcut.TargetPath = $start
        $shortcut.WorkingDirectory = $root
        $shortcut.Description = 'Olivia local client and backend'
    }
    $shortcut.IconLocation = "$icon,0"
    $shortcut.Save()
}

$resultStatus = if ($RefreshExisting) { 'SHORTCUTS_REFRESHED' } else { 'SHORTCUT_CREATED' }
$resultShortcut = if (@($shortcutPaths).Count -eq 1) { [string]$shortcutPaths[0] } else { $null }
[pscustomobject]@{
    status = $resultStatus
    shortcut = $resultShortcut
    shortcuts = @($shortcutPaths)
} | ConvertTo-Json -Compress
