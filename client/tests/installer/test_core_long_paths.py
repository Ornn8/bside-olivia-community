import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows installer path handling")


@pytest.mark.parametrize("failure", ["", "bootstrap", "dependencies"])
def test_core_bootstrap_extends_paths_and_restores_temporary_environment(tmp_path, failure):
    source = (ROOT / "installer/Install.ps1").read_text(encoding="utf-8-sig")
    functions = "\n".join(
        "function " + name + source.split("function " + name, 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        for name in ("ConvertTo-ExtendedPath", "Install-ManagedCoreDependencies")
    )
    site = tmp_path / "packages with spaces"
    site.mkdir()
    wheel = tmp_path / "pip.whl"
    result_path = tmp_path / "observed.json"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("pip/__init__.py", "")
        archive.writestr("pip/__main__.py", (
            "import json,os,sys\nfrom pathlib import Path\n"
            "Path(os.environ['PROBE_RESULT']).write_text(json.dumps({'args':sys.argv[1:],"
            "'temp':{k:os.environ.get(k) for k in ('TEMP','TMP','TMPDIR')}}))\n"
            "raise SystemExit(int(os.environ['PROBE_EXIT']))\n"
        ))
    if failure == "bootstrap":
        wheel.write_bytes(b"invalid archive")
    temporary = tmp_path / "temporary files"
    temporary.mkdir()
    env = dict(os.environ, PYTHONPATH=str(site), PROBE_RESULT=str(result_path),
               PROBE_EXIT="1" if failure == "dependencies" else "0",
               TEMP=str(temporary), TMP=str(temporary))
    env.pop("TMPDIR", None)

    def literal(value):
        return "'" + str(value).replace("'", "''") + "'"

    script = tmp_path / "probe.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n" + functions
        + "try { Install-ManagedCoreDependencies -PythonExe " + literal(sys.executable)
        + " -PipBootstrap " + literal(wheel) + " -SitePackages " + literal(site)
        + " -Wheelhouse " + literal(tmp_path / "offline wheels")
        + " -Requirements " + literal(tmp_path / "requirements.txt")
        + " -PipInstaller " + literal(ROOT / "installer/pip_runtime.py")
        + " } catch { Write-Output $_.Exception.Message }\n"
        + "if ($env:TEMP -cne " + literal(temporary) + " -or $env:TMP -cne "
        + literal(temporary) + " -or (Test-Path Env:TMPDIR)) { exit 7 }\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                            env=env, capture_output=True, text=True, errors="replace", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = {"bootstrap": "OFFLINE_CORE_PIP_BOOTSTRAP_FAILED",
                "dependencies": "OFFLINE_CORE_DEPENDENCY_INSTALL_FAILED"}.get(failure)
    if expected:
        assert expected in result.stdout
    else:
        assert not result.stdout.strip(), result.stdout
    if failure == "bootstrap":
        assert not result_path.exists()
        return
    observed = json.loads(result_path.read_text())
    for flag, target in (("--target", site), ("--find-links", tmp_path / "offline wheels"),
                         ("-r", tmp_path / "requirements.txt")):
        value = observed["args"][observed["args"].index(flag) + 1]
        assert value == "\\\\?\\" + str(target)
    assert "--no-index" in observed["args"] and "--require-hashes" in observed["args"]
    assert all(value == "\\\\?\\" + str(temporary) for value in observed["temp"].values())
