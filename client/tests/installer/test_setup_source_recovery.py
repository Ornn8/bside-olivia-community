import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'installer/Install.ps1'


def run_functions(tmp_path, body):
    shell = shutil.which('powershell')
    if not shell:
        pytest.skip('Windows PowerShell required')
    script = tmp_path / 'check.ps1'
    script.write_text("""$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile('%s', [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'PARSE_FAILED' }
$ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]}, $false) | ForEach-Object { Invoke-Expression $_.Extent.Text }
""" % str(SCRIPT).replace("'", "''") + body, encoding='utf-8-sig')
    result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-File', str(script)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_steam_library_is_found_without_manual_selection(tmp_path):
    value = run_functions(tmp_path, """
New-Item -ItemType Directory -Path 'steam/steamapps/common/Olivia' | Out-Null
Set-Content -Path 'steam/steamapps/appmanifest_4532590.acf' -Value '"installdir" "Olivia"'
$a = Resolve-OfficialInstall -SteamRoots @((Join-Path $pwd 'steam')) -ManifestPath 'unused'
@{found=($a.Path -eq (Join-Path $pwd 'steam/steamapps/common/Olivia')); count=$a.CandidateCount} | ConvertTo-Json
""")
    assert value == {'found': True, 'count': 1}


@pytest.mark.parametrize(('expression', 'expected'), [
    ("[UnauthorizedAccessException]::new('private-secret')", 'SETUP_FILE_PERMISSION_DENIED'),
    ("[IO.FileNotFoundException]::new('private-secret')", 'SETUP_FILE_MISSING'),
    ("[IO.IOException]::new('private-secret', -2147024864)", 'SETUP_FILE_IN_USE'),
    ("[IO.IOException]::new('private-secret', -2147024784)", 'SETUP_DISK_FULL'),
    ("[IO.PathTooLongException]::new('private-secret')", 'SETUP_PATH_TOO_LONG'),
    ("[Exception]::new('OFFICIAL_INSTALL_NOT_FOUND')", 'OFFICIAL_INSTALL_NOT_FOUND'),
    ("[Exception]::new('private-secret')", 'SETUP_INSTALL_FAILED'),
])
def test_safe_failure_details(tmp_path, expression, expected):
    value = run_functions(tmp_path, f"""
$script:CurrentSetupPhase = 'VERIFY_OFFICIAL'
try {{ throw {expression} }} catch {{ Get-SetupFailure -Record $_ | ConvertTo-Json }}
""")
    assert value['code'] == expected
    assert value['phase'] == 'VERIFY_OFFICIAL'
    assert isinstance(value['line'], int)
    assert 'private-secret' not in json.dumps(value)


def test_top_level_trap_writes_safe_diagnostics_before_source_selection(tmp_path):
    shell = shutil.which('powershell')
    if not shell:
        pytest.skip('Windows PowerShell required')
    # Exercise the actual global trap with no original-source diagnostic yet.
    source = SCRIPT.read_text(encoding='utf-8-sig')
    prefix = source.split('$productRoot = [IO.Path]::GetFullPath($Destination)', 1)[0]
    harness = tmp_path / 'failure.ps1'
    harness.write_text(prefix + "\nthrow [IO.IOException]::new('private-secret', -2147024864)\n",
                       encoding='utf-8-sig')
    target = tmp_path / 'result.txt'
    result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-File', str(harness),
                             '-PayloadRoot', str(tmp_path), '-SetupResultPath', str(target)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert target.read_text() == 'OLIVIA_SETUP_ERROR=SETUP_FILE_IN_USE'
    diagnostic = Path(str(target) + '.diagnostic.json').read_text()
    assert json.loads(diagnostic)['failure']['phase'] == 'PREPARE'
    assert 'private-secret' not in diagnostic + result.stdout + result.stderr
    assert str(tmp_path) not in diagnostic
