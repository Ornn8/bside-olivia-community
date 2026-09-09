"""Exercise the PowerShell manifest validator with the current pinned closure."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(os.name != 'nt', reason='Windows installer')
@pytest.mark.parametrize('missing_wheel', [False, True])
def test_powershell_accepts_current_closure_and_rejects_missing_wheel(tmp_path, missing_wheel):
    requirements = ROOT / 'installer/runtime-requirements.txt'
    manifest = json.loads((ROOT / 'contracts/offline_core_assets.example.json').read_text(encoding='utf-8'))
    manifest['requirements_sha256'] = hashlib.sha256(requirements.read_bytes()).hexdigest()
    if missing_wheel:
        manifest['wheels'].pop()
    for wheel in manifest['wheels']:
        file = tmp_path / wheel['path']
        file.parent.mkdir(exist_ok=True)
        file.write_bytes(b'fixture')
    manifest_path = tmp_path / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    script = tmp_path / 'check.ps1'
    script.write_text('''param($InstallScript, $Root, $Manifest, $Requirements)
$ErrorActionPreference = 'Stop'
$ast = [Management.Automation.Language.Parser]::ParseFile($InstallScript, [ref]$null, [ref]$null)
$names = @('Assert-OfflineObjectShape', 'Get-ExpectedOfflineWheels', 'Get-Sha256', 'Get-OfflineCoreAssets')
foreach ($fn in $ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]}, $false)) {
    if ($fn.Name -in $names) { . ([scriptblock]::Create($fn.Extent.Text)) }
}
# File integrity is covered separately; retain the real closure/hash-set checks.
function Resolve-OfflineAsset { param($Root, $Asset) [IO.Path]::GetFullPath((Join-Path $Root $Asset.path)) }
try {
    $result = Get-OfflineCoreAssets -Root $Root -ManifestPath $Manifest -RequirementsPath $Requirements
    'VALID'
} catch { Write-Output $_.Exception.Message; exit 2 }
''', encoding='utf-8-sig')
    result = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script),
                             str(ROOT / 'installer/Install.ps1'), str(tmp_path), str(manifest_path), str(requirements)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == (2 if missing_wheel else 0), result.stdout + result.stderr
    if not missing_wheel:
        assert 'VALID' in result.stdout
