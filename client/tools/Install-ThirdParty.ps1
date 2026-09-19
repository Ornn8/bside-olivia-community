[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $DataRoot,
    [string] $Manifest = (Join-Path $PSScriptRoot '..\contracts\third_party_manifest.example.json'),
    [string] $Python = 'python',
    [switch] $Install,
    [switch] $AcceptLicenses,
    [string[]] $Item
)

$arguments = @(
    (Join-Path $PSScriptRoot 'download_third_party.py'),
    '--manifest', $Manifest,
    '--data-root', $DataRoot
)
if ($Install) { $arguments += '--install' } else { $arguments += '--dry-run' }
if ($AcceptLicenses) { $arguments += '--accept-licenses' }
foreach ($id in ($Item | Where-Object { $_ })) { $arguments += @('--item', $id) }

& $Python @arguments
exit $LASTEXITCODE
