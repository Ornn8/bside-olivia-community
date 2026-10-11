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
# Use the Unicode Shell interface rather than WSH's ANSI shortcut properties.
if (-not ('OliviaShortcut' -as [type])) {
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Text;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
[ComImport, Guid("00021401-0000-0000-C000-000000000046")]
internal class ShellLinkObject { }
[ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IShellLinkW {
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int count, IntPtr data, uint flags);
    void GetIDList(out IntPtr id);
    void SetIDList(IntPtr id);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int count);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string value);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int count);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string value);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int count);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string value);
    void GetHotkey(out short value);
    void SetHotkey(short value);
    void GetShowCmd(out int value);
    void SetShowCmd(int value);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int count, out int index);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string value, int index);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string value, uint reserved);
    void Resolve(IntPtr window, uint flags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string value);
}
public sealed class OliviaShortcut {
    private readonly string path;
    public string TargetPath = "", Arguments = "", WorkingDirectory = "", Description = "", IconLocation = "";
    public OliviaShortcut(string path) {
        this.path = path;
        if (!File.Exists(path)) return;
        var link = (IShellLinkW)new ShellLinkObject();
        try {
            ((IPersistFile)link).Load(path, 0);
            var text = new StringBuilder(32768);
            link.GetPath(text, text.Capacity, IntPtr.Zero, 0); TargetPath = text.ToString();
            text.Clear(); link.GetArguments(text, text.Capacity); Arguments = text.ToString();
            text.Clear(); link.GetWorkingDirectory(text, text.Capacity); WorkingDirectory = text.ToString();
            text.Clear(); link.GetDescription(text, text.Capacity); Description = text.ToString();
            int index; text.Clear(); link.GetIconLocation(text, text.Capacity, out index);
            if (text.Length > 0) IconLocation = text + "," + index;
        } finally { Marshal.FinalReleaseComObject(link); }
    }
    public void Save() {
        var link = (IShellLinkW)new ShellLinkObject();
        try {
            link.SetPath(TargetPath); link.SetArguments(Arguments);
            link.SetWorkingDirectory(WorkingDirectory); link.SetDescription(Description);
            if (IconLocation.Length > 0) {
                int comma = IconLocation.LastIndexOf(',');
                link.SetIconLocation(IconLocation.Substring(0, comma), Int32.Parse(IconLocation.Substring(comma + 1)));
            }
            ((IPersistFile)link).Save(path, true);
        } finally { Marshal.FinalReleaseComObject(link); }
    }
}
'@
}

function Open-Shortcut([string]$Path) { return New-Object OliviaShortcut($Path) }

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
        if ($exists -and -not (Test-OwnedShortcut (Open-Shortcut $path))) { continue }
        if ($RemoveExisting) {
            if ($exists) { Remove-Item -LiteralPath $path -Force; $changed += $path }
            continue
        }
        if ($RefreshExisting -and -not $exists) { continue }
        if ($exists -and (Get-Item -LiteralPath $path).IsReadOnly) { throw 'SHORTCUT_READ_ONLY' }
        $parent = Split-Path -Parent $path
        $destination = if ((Split-Path -Leaf $path) -eq 'Olivia 本地版.lnk') { Join-Path $parent 'Olivia.lnk' } else { $path }
        if ((Test-Path -LiteralPath $destination -PathType Leaf)) {
            if (-not (Test-OwnedShortcut (Open-Shortcut $destination))) { continue }
            if ((Get-Item -LiteralPath $destination).IsReadOnly) { throw 'SHORTCUT_READ_ONLY' }
        }
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        $temporary = Join-Path $parent ('.olivia-' + [Guid]::NewGuid().ToString('N') + '.lnk')
        $shortcut = Open-Shortcut $temporary
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
