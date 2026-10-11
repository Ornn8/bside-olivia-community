[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InstallRoot,
    [string]$ShortcutPath,
    [string]$DesktopPath = $env:OLIVIA_SHORTCUT_DESKTOP,
    [string]$ProgramsPath = $env:OLIVIA_SHORTCUT_PROGRAMS,
    [switch]$RefreshExisting,
    [switch]$RemoveExisting
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $InstallRoot).Path
$native = Join-Path $root 'Olivia.exe'
$start = Join-Path $root 'START.cmd'
$startHidden = Join-Path $root 'START.vbs'
$wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
$shell = New-Object -ComObject WScript.Shell

function Test-SamePath([string]$Left, [string]$Right) {
    if (-not $Left -or -not $Right) { return $false }
    return [string]::Equals([IO.Path]::GetFullPath($Left), [IO.Path]::GetFullPath($Right), [StringComparison]::OrdinalIgnoreCase)
}

function Test-OwnedShortcut($Shortcut) {
    if (Test-SamePath $Shortcut.TargetPath $start) { return $true }
    if (Test-SamePath $Shortcut.TargetPath $native) { return -not $Shortcut.Arguments }
    if (-not (Test-SamePath $Shortcut.TargetPath $wscript)) { return $false }
    # Accept only our VBS and known host switches, never another script/argument.
    $arguments = [string]$Shortcut.Arguments
    if ($arguments -notmatch '^\s*(?:(?://B|//Nologo)\s+)*"([^"]+)"\s*$') { return $false }
    return Test-SamePath $Matches[1] $startHidden
}

$folders = @()
foreach ($folderName in @('Desktop', 'Programs')) {
    try {
        $folder = if ($folderName -eq 'Desktop') { $DesktopPath } else { $ProgramsPath }
        if (-not $folder) { $folder = [Environment]::GetFolderPath($folderName) }
        if ($folder) { $folders += $folder }
    }
    catch { }
}
if ($ShortcutPath) { $candidates = @($ShortcutPath) }
else {
    $candidates = @(foreach ($folder in $folders) {
        foreach ($name in @('Olivia.lnk', 'Olivia 本地版.lnk')) {
            $candidate = Join-Path $folder $name
            try {
                if (Test-Path -LiteralPath $candidate -PathType Leaf) { $candidate }
            }
            catch { }
        }
    })
    if (-not $RefreshExisting -and -not $RemoveExisting) {
        $desktop = if ($DesktopPath) { $DesktopPath } else { [Environment]::GetFolderPath('Desktop') }
        $candidates = @((Join-Path $desktop 'Olivia.lnk')) + $candidates | Select-Object -Unique
    }
}

if (-not $RemoveExisting) {
    if (-not (Test-Path -LiteralPath $native -PathType Leaf)) { throw 'NATIVE_LAUNCHER_NOT_FOUND' }
    $icon = Join-Path $root 'olivia.ico'
    if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) { throw 'NATIVE_ICON_NOT_FOUND' }
}
$changed = @()
$failed = @()
foreach ($path in $candidates) {
    $temporary = $null
    try {
        $exists = Test-Path -LiteralPath $path -PathType Leaf
        if ($exists -and -not (Test-OwnedShortcut ($shell.CreateShortcut($path)))) { continue }
        if ($RemoveExisting) {
            if ($exists) { Remove-Item -LiteralPath $path -Force; $changed += $path }
            continue
        }
        if ($RefreshExisting -and -not $exists) { continue }
        if ($exists -and (Get-Item -LiteralPath $path).IsReadOnly) { throw 'SHORTCUT_READ_ONLY' }
        $parent = Split-Path -Parent $path
        $destination = if ((Split-Path -Leaf $path) -eq 'Olivia 本地版.lnk') { Join-Path $parent 'Olivia.lnk' } else { $path }
        if ((Test-Path -LiteralPath $destination -PathType Leaf)) {
            if (-not (Test-OwnedShortcut ($shell.CreateShortcut($destination)))) { continue }
            if ((Get-Item -LiteralPath $destination).IsReadOnly) { throw 'SHORTCUT_READ_ONLY' }
        }
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        $temporary = Join-Path $parent ('.olivia-' + [Guid]::NewGuid().ToString('N') + '.lnk')
        $shortcut = $shell.CreateShortcut($temporary)
        $shortcut.TargetPath = $native
        $shortcut.Arguments = ''
        $shortcut.WorkingDirectory = $root
        $shortcut.Description = 'Olivia'
        $shortcut.IconLocation = "$icon,0"
        $shortcut.Save()
        Move-Item -LiteralPath $temporary -Destination $destination -Force
        if (-not (Test-SamePath $path $destination)) { Remove-Item -LiteralPath $path }
        $changed += $destination
    }
    catch {
        $failed += $path
        if (-not $RefreshExisting -and -not $RemoveExisting) { throw }
    }
    finally {
        if ($temporary -and (Test-Path -LiteralPath $temporary)) { Remove-Item -LiteralPath $temporary -Force }
    }
}
$status = if ($RemoveExisting) { 'SHORTCUTS_REMOVED' } elseif ($RefreshExisting) { 'SHORTCUTS_REFRESHED' } else { 'SHORTCUT_CREATED' }
[pscustomobject]@{ status = $status; shortcuts = @($changed); failed = @($failed) } | ConvertTo-Json -Compress
