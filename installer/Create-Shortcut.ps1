[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallRoot,
    [string]$ShortcutPath = (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Olivia 本地版.lnk')
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

$shortcutParent = Split-Path -Parent $ShortcutPath
if ($shortcutParent) {
    New-Item -ItemType Directory -Force -Path $shortcutParent | Out-Null
}
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $start
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = "$client,0"
$shortcut.Description = 'Olivia local client and backend'
$shortcut.Save()

[pscustomobject]@{
    status = 'SHORTCUT_CREATED'
    shortcut = $ShortcutPath
} | ConvertTo-Json -Compress
