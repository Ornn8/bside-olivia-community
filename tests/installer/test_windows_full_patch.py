from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from installer import full_patch
from installer.full_patch import (
    PAYLOAD_REQUIRED_RELATIVE_FILES,
    PAYLOAD_REQUIRED_ROOT_FILES,
    PatchInstallError,
    copy_project_payload,
    discover_steam_install,
    install_full_patch,
    uninstall_full_patch,
)
from installer.start_local import (
    _client_command,
    _client_environment,
    _client_executable,
)


CURRENT_TEST_CLIENT_VERSION = "0.0.9.627"


def test_video_seed_patch_is_a_required_runtime_payload() -> None:
    assert "installer/seed-vc-overlap-frames.patch" in PAYLOAD_REQUIRED_RELATIVE_FILES
    assert "installer/assets/olivia.ico" in PAYLOAD_REQUIRED_RELATIVE_FILES
    assert {"contracts/video_capability_action.schema.json", "contracts/video_capability_manifest.schema.json", "contracts/video_capability_status.schema.json"} <= PAYLOAD_REQUIRED_RELATIVE_FILES


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_install_entrypoint_is_parseable_by_windows_powershell() -> None:
    if os.name != "nt":
        pytest.skip("Windows PowerShell is only available on Windows")

    repo_root = Path(__file__).parents[2]
    script = repo_root / "installer" / "Install.ps1"
    result = subprocess.run(
        [
            os.environ.get("WINDIR", r"C:\Windows")
            + r"\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-?",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_installer_shortcut_starts_selected_install_entrypoint(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows shortcuts are only available on Windows")

    repo_root = Path(__file__).parents[2]
    script = repo_root / "installer" / "Create-Shortcut.ps1"
    install_root = tmp_path / "selected install with spaces"
    client = install_root / "app" / "0.0.9.615" / "Olivia.exe"
    icon = install_root / "local_backend" / "installer" / "assets" / "olivia.ico"
    start = install_root / "START.cmd"
    shortcut = tmp_path / "desktop" / "Olivia-local.lnk"
    client.parent.mkdir(parents=True)
    icon.parent.mkdir(parents=True)
    client.write_bytes(b"synthetic client")
    icon.write_bytes(b"synthetic icon")
    start.write_text("@exit /b 0\n", encoding="utf-8")
    (install_root / ".olivia-full-patch.json").write_text(
        json.dumps({"client_version": "0.0.9.615"}),
        encoding="utf-8",
    )

    powershell = str(
        Path(os.environ["WINDIR"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    shortcut.parent.mkdir(parents=True)
    stale = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
                "[Console]::In.ReadToEnd().Trim());"
                "$s.TargetPath=$env:COMSPEC;"
                "$s.Arguments='//B //Nologo \"F:\\old\\START.vbs\"';"
                "$s.Save()"
            ),
        ],
        input=str(shortcut),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert stale.returncode == 0, stale.stderr or stale.stdout
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-InstallRoot",
            str(install_root),
            "-ShortcutPath",
            str(shortcut),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    inspect = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
                "[Console]::In.ReadToEnd().Trim());"
                "[pscustomobject]@{target=$s.TargetPath;arguments=$s.Arguments;working=$s.WorkingDirectory;"
                "icon=$s.IconLocation}|ConvertTo-Json -Compress"
            ),
        ],
        input=str(shortcut),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    metadata = json.loads(inspect.stdout)
    assert Path(metadata["target"]) == start
    assert metadata["arguments"] == ""
    assert Path(metadata["working"]) == install_root
    assert metadata["icon"] == f"{icon},0"


def test_shortcut_refresh_repairs_arguments_and_uses_the_active_patch_icon(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows shortcuts are only available on Windows")

    repo_root = Path(__file__).parents[2]
    script = repo_root / "installer" / "Create-Shortcut.ps1"
    install_root = tmp_path / "installation"
    client = install_root / "app" / "0.0.9.615" / "Olivia.exe"
    legacy_icon = install_root / "local_backend" / "installer" / "assets" / "olivia.ico"
    active_root = install_root / "versions" / "local_backend" / f"1.1.0-{'a' * 64}"
    active_icon = active_root / "installer" / "assets" / "olivia.ico"
    start = install_root / "START.cmd"
    shortcut = tmp_path / "desktop" / "Olivia-local.lnk"
    client.parent.mkdir(parents=True)
    legacy_icon.parent.mkdir(parents=True)
    active_icon.parent.mkdir(parents=True)
    client.write_bytes(b"synthetic client")
    legacy_icon.write_bytes(b"legacy icon")
    active_icon.write_bytes(b"active icon")
    start.write_text("@exit /b 0\n", encoding="utf-8")
    (install_root / ".olivia-full-patch.json").write_text(
        json.dumps({"client_version": "0.0.9.615"}),
        encoding="utf-8",
    )

    powershell = str(
        Path(os.environ["WINDIR"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    base_command = [
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-InstallRoot",
        str(install_root),
        "-ShortcutPath",
        str(shortcut),
    ]
    created = subprocess.run(
        base_command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert created.returncode == 0, created.stderr or created.stdout
    arguments = '--preserve "two words"'
    configured = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
                "[Console]::In.ReadToEnd().Trim());"
                f"$s.Arguments='{arguments}';$s.Save()"
            ),
        ],
        input=str(shortcut),
        capture_output=True,
        text=True,
        check=False,
    )
    assert configured.returncode == 0, configured.stderr or configured.stdout
    (install_root / ".olivia-update-state.json").write_text(
        json.dumps(
            {
                "active_components": {
                    "local_backend": {
                        "payload_path": active_root.relative_to(install_root).as_posix()
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    refreshed = subprocess.run(
        [*base_command, "-RefreshExisting"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert refreshed.returncode == 0, refreshed.stderr or refreshed.stdout
    inspect = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
                "[Console]::In.ReadToEnd().Trim());"
                "[pscustomobject]@{target=$s.TargetPath;arguments=$s.Arguments;"
                "icon=$s.IconLocation}"
                "|ConvertTo-Json -Compress"
            ),
        ],
        input=str(shortcut),
        capture_output=True,
        text=True,
        check=False,
    )
    metadata = json.loads(inspect.stdout)
    assert Path(metadata["target"]) == start
    assert metadata["arguments"] == ""
    assert metadata["icon"] == f"{active_icon},0"


def test_shortcut_refresh_does_not_rewrite_another_installation(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows shortcuts are only available on Windows")

    repo_root = Path(__file__).parents[2]
    script = repo_root / "installer" / "Create-Shortcut.ps1"
    install_root = tmp_path / "current installation"
    client = install_root / "app" / "0.0.9.615" / "Olivia.exe"
    icon = install_root / "local_backend" / "installer" / "assets" / "olivia.ico"
    start = install_root / "START.cmd"
    client.parent.mkdir(parents=True)
    icon.parent.mkdir(parents=True)
    client.write_bytes(b"synthetic client")
    icon.write_bytes(b"current icon")
    start.write_text("@exit /b 0\n", encoding="utf-8")
    (install_root / ".olivia-full-patch.json").write_text(
        json.dumps({"client_version": "0.0.9.615"}),
        encoding="utf-8",
    )

    other_root = tmp_path / "other installation"
    other_start = other_root / "START.cmd"
    other_icon = other_root / "olivia.ico"
    other_root.mkdir(parents=True)
    other_start.write_text("@exit /b 0\n", encoding="utf-8")
    other_icon.write_bytes(b"other icon")
    shortcut = tmp_path / "desktop" / "Olivia-local.lnk"
    shortcut.parent.mkdir(parents=True)
    arguments = '--keep "other install"'
    description = "Other Olivia installation"
    powershell = str(
        Path(os.environ["WINDIR"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    configured = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$p=[Console]::In.ReadToEnd()|ConvertFrom-Json;"
                "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($p.shortcut);"
                "$s.TargetPath=$p.target;$s.Arguments=$p.arguments;"
                "$s.WorkingDirectory=$p.working;$s.Description=$p.description;"
                "$s.IconLocation=$p.icon;$s.Save()"
            ),
        ],
        input=json.dumps(
            {
                "shortcut": str(shortcut),
                "target": str(other_start),
                "arguments": arguments,
                "working": str(other_root),
                "description": description,
                "icon": f"{other_icon},0",
            }
        ),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert configured.returncode == 0, configured.stderr or configured.stdout

    refreshed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-InstallRoot",
            str(install_root),
            "-ShortcutPath",
            str(shortcut),
            "-RefreshExisting",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert refreshed.returncode == 0, refreshed.stderr or refreshed.stdout

    inspect = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
                "[Console]::In.ReadToEnd().Trim());"
                "[pscustomobject]@{target=$s.TargetPath;arguments=$s.Arguments;"
                "working=$s.WorkingDirectory;description=$s.Description;"
                "icon=$s.IconLocation}|ConvertTo-Json -Compress"
            ),
        ],
        input=str(shortcut),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    metadata = json.loads(inspect.stdout)
    assert Path(metadata["target"]) == other_start
    assert metadata["arguments"] == arguments
    assert Path(metadata["working"]) == other_root
    assert metadata["description"] == description
    assert metadata["icon"] == f"{other_icon},0"


def test_shortcut_refresh_discovers_desktop_and_programs_independently() -> None:
    repo_root = Path(__file__).parents[2]
    script = (repo_root / "installer" / "Create-Shortcut.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert "foreach ($folderName in @('Desktop', 'Programs'))" in script
    assert "[Environment]::GetFolderPath($folderName)" in script


def test_install_entrypoint_prioritizes_selected_payload() -> None:
    repo_root = Path(__file__).parents[2]
    script = (repo_root / "installer" / "Install.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert "installer\\bootstrap_install.py" in script
    assert "@($bootstrap, $PayloadRoot) + $arguments" in script
    assert (repo_root / "installer" / "bootstrap_install.py").is_file()


def test_install_bootstrap_passes_arguments_to_the_package_cli(tmp_path: Path) -> None:
    repo_root = Path(__file__).parents[2]
    bootstrap = repo_root / "installer" / "bootstrap_install.py"
    missing_official = tmp_path / "official-client"

    result = subprocess.run(
        [
            sys.executable,
            str(bootstrap),
            str(repo_root),
            "doctor",
            "--official-root",
            str(missing_official),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["code"] == "OFFICIAL_INSTALL_NOT_FOUND"


def test_doctor_reports_both_supported_original_archives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from installer import __main__ as installer_cli

    monkeypatch.setattr(
        installer_cli,
        "load_manifest",
        lambda _path: {
            "client_version": "0.0.9.627",
            "live_status": "UNAVAILABLE_PAUSED",
            "media_status": "ORIGINAL_WEBPLAYER_LOCAL_VIDEO",
        },
    )
    monkeypatch.setattr(
        installer_cli,
        "validate_official_source",
        lambda _source, _manifest: (
            "0.0.9.627",
            tmp_path / "feapp.dat",
            tmp_path / "webplayer.dat",
        ),
    )

    assert installer_cli.main(
        ["doctor", "--official-root", str(tmp_path)]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "SUPPORTED"
    assert report["client_version"] == "0.0.9.627"
    assert report["feapp"].endswith("feapp.dat")
    assert report["webplayer"].endswith("webplayer.dat")


def test_install_entrypoint_uses_dotnet_sha256_not_optional_powershell_cmdlet() -> None:
    repo_root = Path(__file__).parents[2]
    script = (repo_root / "installer" / "Install.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert "function Get-Sha256" in script
    assert "[Security.Cryptography.SHA256]::Create()" in script
    assert "Get-FileHash" not in script


def test_managed_runtime_installs_all_server_dependencies() -> None:
    repo_root = Path(__file__).parents[2]
    project = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    script = (repo_root / "installer" / "Install.ps1").read_text(
        encoding="utf-8-sig"
    )
    requirements = (
        repo_root / "installer" / "runtime-requirements.txt"
    ).read_text(encoding="utf-8")

    assert "import aiohttp,jsonschema" in script
    assert '"imageio-ffmpeg>=0.6,<1"' in project
    assert "jsonschema==4.26.0" in requirements
    assert "jsonschema-specifications==2025.9.1" in requirements
    assert "referencing==0.37.0" in requirements
    assert "rpds-py==2026.6.3" in requirements


def test_missing_managed_server_dependencies_are_a_nonfatal_probe_result() -> None:
    if os.name != "nt":
        pytest.skip("Windows PowerShell is only available on Windows")

    repo_root = Path(__file__).parents[2]
    command = (
        "$tokens=$null;$errors=$null;"
        "$ast=[System.Management.Automation.Language.Parser]::ParseFile("
        "$env:BSIDE_INSTALL_SCRIPT,[ref]$tokens,[ref]$errors);"
        "if($errors.Count){throw 'INSTALL_SCRIPT_PARSE_FAILED'};"
        "$function=$ast.Find({param($node)"
        "$node -is [System.Management.Automation.Language.FunctionDefinitionAst]"
        " -and $node.Name -eq 'Test-ManagedServerDependencies'},$true);"
        "if(-not $function){throw 'MANAGED_DEPENDENCY_PROBE_MISSING'};"
        ". ([scriptblock]::Create($function.Extent.Text));"
        "$ErrorActionPreference='Stop';"
        "if(Test-ManagedServerDependencies -PythonExe $env:BSIDE_PROBE_EXECUTABLE){exit 3};"
        "exit 0"
    )
    env = os.environ.copy()
    env.update(
        {
            "BSIDE_INSTALL_SCRIPT": str(repo_root / "installer" / "Install.ps1"),
            "BSIDE_PROBE_EXECUTABLE": str(
                Path(os.environ.get("WINDIR", r"C:\\Windows"))
                / "System32"
                / "WindowsPowerShell"
                / "v1.0"
                / "powershell.exe"
            ),
        }
    )
    powershell = env["BSIDE_PROBE_EXECUTABLE"]
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def _run_managed_python_path_helper(
    *,
    pth_path: Path,
    deny_replace: bool = False,
) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).parents[2]
    command = (
        "$tokens=$null;$errors=$null;"
        "$ast=[System.Management.Automation.Language.Parser]::ParseFile("
        "$env:BSIDE_INSTALL_SCRIPT,[ref]$tokens,[ref]$errors);"
        "if($errors.Count){throw 'INSTALL_SCRIPT_PARSE_FAILED'};"
        "$function=$ast.Find({param($node)"
        "$node -is [System.Management.Automation.Language.FunctionDefinitionAst]"
        " -and $node.Name -eq 'Update-ManagedPythonPath'},$true);"
        "if(-not $function){throw 'MANAGED_PYTHON_PATH_HELPER_MISSING'};"
        ". ([scriptblock]::Create($function.Extent.Text));"
        "$lock=$null;"
        "if($env:BSIDE_DENY_REPLACE -eq '1'){"
        "$lock=[IO.File]::Open($env:BSIDE_PTH_PATH,[IO.FileMode]::Open,"
        "[IO.FileAccess]::Read,[IO.FileShare]::ReadWrite)};"
        "try{Update-ManagedPythonPath -PthPath $env:BSIDE_PTH_PATH}"
        "finally{if($lock){$lock.Dispose()}}"
    )
    env = os.environ.copy()
    env.update(
        {
            "BSIDE_INSTALL_SCRIPT": str(repo_root / "installer" / "Install.ps1"),
            "BSIDE_PTH_PATH": str(pth_path),
            "BSIDE_DENY_REPLACE": "1" if deny_replace else "0",
        }
    )
    powershell = (
        Path(os.environ.get("WINDIR", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    return subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_managed_runtime_pth_never_writes_payload_and_preserves_existing_paths(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows PowerShell is only available on Windows")

    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    pth_path = runtime_root / "python312._pth"
    payload_root = tmp_path / "fresh payload"
    pth_path.write_text(
        "python312.zip\n.\n#import site\n",
        encoding="utf-8",
    )

    result = _run_managed_python_path_helper(
        pth_path=pth_path,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert pth_path.read_text(encoding="utf-8").splitlines() == [
        "python312.zip",
        ".",
        "#import site",
        "site-packages",
        "import site",
    ]
    assert str(payload_root) not in pth_path.read_text(encoding="utf-8")
    assert not pth_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert not list(runtime_root.glob(f".{pth_path.name}.*"))

    original = (
        "python312.zip\n.\n"
        f"{tmp_path / '替换失败时保留'}\n"
        "site-packages\nsite-packages\n"
    ).encode("utf-8")
    pth_path.write_bytes(original)

    result = _run_managed_python_path_helper(
        pth_path=pth_path,
        deny_replace=True,
    )

    assert result.returncode != 0
    assert pth_path.read_bytes() == original
    assert not list(runtime_root.glob(f".{pth_path.name}.*"))

    current_payload = tmp_path / "当前解压目录"
    legacy_payload = tmp_path / "旧版解压目录"
    unrelated_root = tmp_path / "用户 Python 模块"
    pth_path.write_text(
        "\n".join(
            (
                "python312.zip",
                ".",
                str(current_payload),
                str(legacy_payload),
                str(unrelated_root),
                "site-packages",
                "site-packages",
                "import site",
                "import site",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_managed_python_path_helper(
        pth_path=pth_path,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert pth_path.read_text(encoding="utf-8").splitlines() == [
        "python312.zip",
        ".",
        str(current_payload),
        str(legacy_payload),
        str(unrelated_root),
        "site-packages",
        "import site",
    ]


def _run_memory_runtime_registration(
    *,
    pth_path: Path,
    memory_runtime: Path,
    requirements: Path,
    verifier: Path,
) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).parents[2]
    command = (
        "$tokens=$null;$errors=$null;"
        "$ast=[System.Management.Automation.Language.Parser]::ParseFile("
        "$env:BSIDE_INSTALL_SCRIPT,[ref]$tokens,[ref]$errors);"
        "if($errors.Count){throw 'INSTALL_SCRIPT_PARSE_FAILED'};"
        "$names=@('Assert-NoReparsePointsInPath','Test-NoReparsePointsInTree',"
        "'Update-ManagedPythonPath','Register-VerifiedMemoryRuntime');"
        "foreach($name in $names){"
        "$function=$ast.Find({param($node)"
        "$node -is [System.Management.Automation.Language.FunctionDefinitionAst]"
        " -and $node.Name -eq $name},$true);"
        "if(-not $function){throw ('FUNCTION_MISSING:'+$name)};"
        ". ([scriptblock]::Create($function.Extent.Text))};"
        "$registered=Register-VerifiedMemoryRuntime "
        "-CandidateExe $env:BSIDE_PROBE_EXECUTABLE "
        "-PthPath $env:BSIDE_PTH_PATH "
        "-MemoryRuntimePath $env:BSIDE_MEMORY_RUNTIME_PATH "
        "-RequirementsPath $env:BSIDE_MEMORY_REQUIREMENTS "
        "-VerifierPath $env:BSIDE_MEMORY_VERIFIER;"
        "if($registered){exit 0};exit 7"
    )
    env = os.environ.copy()
    env.update(
        {
            "BSIDE_INSTALL_SCRIPT": str(repo_root / "installer" / "Install.ps1"),
            "BSIDE_PROBE_EXECUTABLE": sys.executable,
            "BSIDE_PTH_PATH": str(pth_path),
            "BSIDE_MEMORY_RUNTIME_PATH": str(memory_runtime),
            "BSIDE_MEMORY_REQUIREMENTS": str(requirements),
            "BSIDE_MEMORY_VERIFIER": str(verifier),
        }
    )
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _memory_registration_fixture(
    tmp_path: Path,
    verifier_source: str,
) -> tuple[Path, Path, Path, Path, dict[Path, str]]:
    product = tmp_path / "product"
    python_root = product / "runtime" / "python-3.12.10-embed-amd64"
    python_root.mkdir(parents=True)
    pth_path = python_root / "python312._pth"
    pth_path.write_text(
        "python312.zip\n.\nsite-packages\nimport site\n",
        encoding="utf-8",
    )
    memory_runtime = product / "runtime" / "mem0-site-packages"
    memory_runtime.mkdir()
    runtime_marker = memory_runtime / ".olivia-mem0-runtime-manifest.json"
    runtime_marker.write_text('{"synthetic":"owned"}', encoding="utf-8")
    requirements = tmp_path / "mem0-runtime-requirements.txt"
    requirements.write_text("synthetic==1\n", encoding="utf-8")
    verifier = tmp_path / "verify.py"
    verifier.write_text(verifier_source, encoding="utf-8")
    data_file = product / "install" / "data" / "config" / "llm.json"
    data_file.parent.mkdir(parents=True)
    data_file.write_bytes(b"synthetic-user-data")
    model_manifest = (
        product
        / "install"
        / "data"
        / "memory"
        / "model-cache"
        / "olivia-mem0-embedding-manifest.json"
    )
    model_manifest.parent.mkdir(parents=True)
    model_manifest.write_bytes(b"synthetic-model-manifest")
    preserved = {
        path: _sha256(path)
        for path in (runtime_marker, data_file, model_manifest)
    }
    return pth_path, memory_runtime, requirements, verifier, preserved


def test_core_runtime_upgrade_registers_verified_mem0_without_touching_data(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows PowerShell is only available on Windows")
    pth, runtime, requirements, verifier, preserved = _memory_registration_fixture(
        tmp_path,
        "raise SystemExit(0)\n",
    )

    result = _run_memory_runtime_registration(
        pth_path=pth,
        memory_runtime=runtime,
        requirements=requirements,
        verifier=verifier,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert pth.read_text(encoding="utf-8").splitlines()[:3] == [
        str(runtime),
        str(runtime / "win32"),
        str(runtime / "win32" / "lib"),
    ]
    assert {path: _sha256(path) for path in preserved} == preserved


@pytest.mark.parametrize(
    "verifier_source",
    ("raise SystemExit(2)\n", "raise RuntimeError('synthetic failure')\n"),
)
def test_core_runtime_upgrade_keeps_failed_mem0_unregistered_and_preserved(
    tmp_path: Path,
    verifier_source: str,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows PowerShell is only available on Windows")
    pth, runtime, requirements, verifier, preserved = _memory_registration_fixture(
        tmp_path,
        verifier_source,
    )

    result = _run_memory_runtime_registration(
        pth_path=pth,
        memory_runtime=runtime,
        requirements=requirements,
        verifier=verifier,
    )

    assert result.returncode == 7
    assert all(
        "mem0-site-packages" not in line
        for line in pth.read_text(encoding="utf-8").splitlines()
    )
    assert {path: _sha256(path) for path in preserved} == preserved


@pytest.mark.parametrize("nested", (False, True))
def test_core_runtime_upgrade_rejects_root_and_descendant_reparse_points(
    tmp_path: Path,
    nested: bool,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows junction contract")
    pth, runtime, requirements, verifier, _preserved = _memory_registration_fixture(
        tmp_path,
        "raise SystemExit(0)\n",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    if nested:
        link = runtime / "nested-runtime-link"
    else:
        (runtime / ".olivia-mem0-runtime-manifest.json").unlink()
        runtime.rmdir()
        link = runtime
    linked = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if linked.returncode != 0:
        pytest.skip("junction creation is unavailable")
    try:
        result = _run_memory_runtime_registration(
            pth_path=pth,
            memory_runtime=runtime,
            requirements=requirements,
            verifier=verifier,
        )

        assert result.returncode == 7
        assert all(
            "mem0-site-packages" not in line
            for line in pth.read_text(encoding="utf-8").splitlines()
        )
    finally:
        os.rmdir(link)


def test_published_powershell_scripts_are_utf8_bom_safe() -> None:
    repo_root = Path(__file__).parents[2]
    for relative in (
        Path("installer") / "Install.ps1",
        Path("installer") / "Create-Shortcut.ps1",
        Path("tools") / "Install-ThirdParty.ps1",
    ):
        raw = (repo_root / relative).read_bytes()
        assert raw.startswith(
            b"\xef\xbb\xbf"
        ), f"{relative} must be UTF-8 BOM for Windows PowerShell 5.1"
        raw.decode("utf-8-sig")


def test_install_cmd_preserves_paths_with_spaces_for_windows_powershell(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("cmd.exe is only available on Windows")

    repo_root = Path(__file__).parents[2]
    package = tmp_path / "cmd package with spaces"
    package.mkdir()
    (package / "installer").mkdir()
    shutil.copy2(repo_root / "INSTALL.cmd", package / "INSTALL.cmd")
    (package / "installer" / "Install.ps1").write_text(
        "# fixture",
        encoding="utf-8",
    )
    capture = package / "captured-powershell-arguments.txt"
    (package / "powershell.cmd").write_text(
        "@echo off\n"
        f'>"{capture}" echo %*\n'
        "exit /b 0\n",
        encoding="ascii",
    )

    env = os.environ.copy()
    env["PATH"] = str(package) + os.pathsep + env.get("PATH", "")
    result = subprocess.run(
        [
            os.environ.get(
                "COMSPEC",
                r"C:\Windows\System32\cmd.exe",
            ),
            "/d",
            "/c",
            "call INSTALL.cmd",
        ],
        cwd=package,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    args = capture.read_text(encoding="ascii").strip()
    assert f'-File "{package}\\.\\installer\\Install.ps1"' in args
    assert f'-PayloadRoot "{package}\\."' in args
    assert "-Destination" in args
    assert 'Install.ps1" -PayloadRoot' in args


def test_start_local_matches_first_party_client_launch_contract(
    tmp_path: Path,
) -> None:
    client = tmp_path / "app" / "0.0.9.615" / "Olivia.exe"
    local = tmp_path / "profile" / "Local"
    roaming = tmp_path / "profile" / "Roaming"
    environment = _client_environment(
        {
            "OLIVIA_INSTALL_ROOT": str(tmp_path),
            "SteamAppId": "4532590",
            "SteamGameId": "4532590",
        },
        roaming,
        local,
    )

    assert _client_command(client, local) == [
        str(client),
        f'--user-data-dir={local / "cef"}',
    ]
    assert environment["APPDATA"] == str(roaming)
    assert environment["LOCALAPPDATA"] == str(local)
    assert environment["SteamAppId"] == "4532590"
    assert "SteamGameId" not in environment


def _make_official(root: Path) -> tuple[Path, str, str]:
    version_root = root / CURRENT_TEST_CLIENT_VERSION
    resources = version_root / "resources"
    resources.mkdir(parents=True)
    (root / "launcher.exe").write_bytes(b"official launcher fixture")
    (root / "letter_pairs.json").write_text(
        "synthetic user letters",
        encoding="utf-8",
    )
    (root / "memory_store.json").write_text(
        "synthetic user memory",
        encoding="utf-8",
    )
    (root / "llm_config.json").write_text(
        '{"api_key":"synthetic"}',
        encoding="utf-8",
    )
    (version_root / "Olivia.exe").write_bytes(b"official client fixture")

    javascript = (
        "prefix "
        "He=e=>new Promise((t,n)=>{try{"
        ',"query.response":no(a)}}),t(c)},onFailure:\'suffix\' '
        '!z.isNew||N?(await t.replace({name:ye.Home}),'
        'await h(z.uid.toString(),z.modelGatewayToken||"",!1))'
    )
    feapp = resources / "feapp.dat"
    with zipfile.ZipFile(feapp, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr(
            "index.html",
            "<!doctype html><html><head>"
            '<script type="module" crossorigin '
            'src="./assets/main-917d29fc.js"></script>'
            '<link rel="stylesheet" href="./assets/index.css">'
            "</head><body><div id=\"app\"></div></body></html>",
        )
        output.writestr("assets/main-917d29fc.js", javascript)
        output.writestr("assets/index.css", "body{display:block}")

    webplayer = resources / "webplayer.dat"
    with zipfile.ZipFile(webplayer, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr(
            "index.html",
            "<!doctype html><html><head>"
            '<script type="module" crossorigin '
            'src="./assets/main-752b9fc4.js"></script>'
            "</head><body><div id=\"app\"></div></body></html>",
        )
        output.writestr(
            "assets/main-752b9fc4.js",
            "console.log('synthetic original player')",
        )
    return root, _sha256(feapp), _sha256(webplayer)


def _write_manifest(
    path: Path,
    feapp_sha256: str,
    webplayer_sha256: str,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": "olivia.full-patch.v2",
                "steam_app_id": "4532590",
                "client_version": CURRENT_TEST_CLIENT_VERSION,
                "feapp_sha256": feapp_sha256,
                "webplayer_sha256": webplayer_sha256,
                "patch_mode": "isolated-copy",
                "live_status": "UNAVAILABLE_PAUSED",
                "media_status": "ORIGINAL_WEBPLAYER_LOCAL_VIDEO",
            }
        ),
        encoding="utf-8",
    )
    return path


def _make_payload(repo_root: Path, root: Path) -> Path:
    root.mkdir()
    for name in PAYLOAD_REQUIRED_ROOT_FILES:
        shutil.copy2(repo_root / name, root / name)
    shutil.copytree(repo_root / "control_center", root / "control_center")
    shutil.copytree(repo_root / "contracts", root / "contracts")
    shutil.copytree(repo_root / "installer", root / "installer")
    shutil.copytree(repo_root / "runtime", root / "runtime")
    return root


def test_copy_payload_excludes_non_runtime_project_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "payload"
    destination = tmp_path / "installed" / "local_backend"
    source.mkdir()
    for name in PAYLOAD_REQUIRED_ROOT_FILES | {"dynamic_renderer.py"}:
        (source / name).write_text("# fixture", encoding="utf-8")
    (source / "archive_config.json").write_text(
        "archive fixture",
        encoding="utf-8",
    )
    for name in (
        "test_fixture.py",
        "pytest.ini",
        "requirements-dev.txt",
        "requirements-dev-extra.txt",
    ):
        (source / name).write_text("fixture", encoding="utf-8")
    (source / ".evidence").mkdir()
    (source / ".evidence" / "capture.json").write_text(
        "{}",
        encoding="utf-8",
    )
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "fixture.pyc").write_bytes(b"pyc")
    (source / "control_center").mkdir()
    for name in (
        "__init__.py",
        "app.py",
        "auth.py",
        "memory_api.py",
        "private_world_api.py",
        "private_world_candidate_api.py",
        "private_world_candidate_backend.py",
        "private_world_candidate_ui.py",
        "runtime.py",
    ):
        (source / "control_center" / name).write_text(
            "# runtime fixture",
            encoding="utf-8",
        )
    for relative in PAYLOAD_REQUIRED_RELATIVE_FILES:
        required = source / Path(*relative.split("/"))
        required.parent.mkdir(parents=True, exist_ok=True)
        if not required.exists():
            required.write_text("# runtime fixture", encoding="utf-8")
    experimental = source / "runtime" / "packaging" / "experimental.py"
    experimental.parent.mkdir(parents=True)
    experimental.write_text("# excluded experiment", encoding="utf-8")

    copied = copy_project_payload(source, destination)

    assert "control_center/" in copied
    assert (destination / "control_center" / "runtime.py").is_file()
    assert (destination / "contracts" / "letter_status.py").is_file()
    assert (
        destination / "contracts" / "third_party_manifest.example.json"
    ).is_file()
    assert (
        destination / "contracts" / "third_party_manifest.schema.json"
    ).is_file()
    assert (destination / "runtime" / "original_client_media_http.py").is_file()
    assert (destination / "runtime" / "video_reply_settings.py").is_file()
    assert (destination / "runtime" / "imports" / "official_letters.py").is_file()
    assert (destination / "runtime" / "imports" / "historical_memory.py").is_file()
    assert not (destination / "runtime" / "packaging").exists()
    assert "dynamic_renderer.py" in copied
    assert (destination / "dynamic_renderer.py").is_file()
    assert "archive_config.json" in copied
    assert (destination / "archive_config.json").is_file()
    for name in PAYLOAD_REQUIRED_ROOT_FILES:
        assert name in copied
        assert (destination / name).is_file()
    for name in (
        "test_fixture.py",
        "pytest.ini",
        "requirements-dev.txt",
        "requirements-dev-extra.txt",
    ):
        assert name not in copied
        assert not (destination / name).exists()
    assert not (destination / ".evidence").exists()
    assert not (destination / "__pycache__").exists()


def test_copy_payload_includes_runtime_packages_used_by_product_imports(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).parents[2]
    destination = tmp_path / "installed" / "local_backend"

    copy_project_payload(repo_root, destination)

    for relative in (
        "runtime/memory/bounded_daemon_call.py",
        "runtime/memory/memory_port.py",
        "runtime/media/music_duration.py",
        "runtime/media/music_reply.py",
        "runtime/media/voice_direction.py",
        "runtime/persona/persona_loader.py",
        "runtime/private_world/ledger.py",
        "runtime/reply/reply_model_quality.py",
        "runtime/reply/reply_orchestrator.py",
        "runtime/reply/reply_quality_gate.py",
        "runtime/validation/memory_isolation_case01.py",
    ):
        assert (destination / relative).is_file(), relative


def test_copy_payload_from_git_tree_excludes_untracked_files(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).parents[2]
    source = _make_payload(repo_root, tmp_path / "payload")
    (source / "dynamic_renderer.py").write_text(
        "# tracked runtime fixture",
        encoding="utf-8",
    )
    (source / ".gitignore").write_text(
        "ignored_config.json\ncontrol_center/ignored_config.json\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "init"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )
    (source / "untracked_config.json").write_text(
        "untracked",
        encoding="utf-8",
    )
    (source / "ignored_config.json").write_text(
        "ignored",
        encoding="utf-8",
    )
    (source / "control_center" / "untracked_config.json").write_text(
        "untracked",
        encoding="utf-8",
    )
    (source / "control_center" / "ignored_config.json").write_text(
        "ignored",
        encoding="utf-8",
    )

    destination = tmp_path / "installed" / "local_backend"
    copied = copy_project_payload(source, destination)

    assert "dynamic_renderer.py" in copied
    assert not (destination / "untracked_config.json").exists()
    assert not (destination / "ignored_config.json").exists()
    assert not (
        destination / "control_center" / "untracked_config.json"
    ).exists()
    assert not (
        destination / "control_center" / "ignored_config.json"
    ).exists()


def test_copy_payload_from_linked_worktree_uses_tracked_files(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).parents[2]
    repository = _make_payload(repo_root, tmp_path / "repository")
    (repository / ".gitignore").write_text(
        "ignored_config.json\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "init"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=payload-test",
            "-c",
            "user.email=payload-test@example.invalid",
            "commit",
            "-m",
            "payload",
        ],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    source = tmp_path / "linked-worktree"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(source), "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    assert (source / ".git").is_file()
    (source / "untracked_config.json").write_text(
        "untracked",
        encoding="utf-8",
    )
    (source / "ignored_config.json").write_text(
        "ignored",
        encoding="utf-8",
    )

    destination = tmp_path / "installed" / "local_backend"
    copy_project_payload(source, destination)

    assert not (destination / "untracked_config.json").exists()
    assert not (destination / "ignored_config.json").exists()
    for name in PAYLOAD_REQUIRED_ROOT_FILES:
        assert (destination / name).is_file()


def test_copy_payload_git_query_failure_is_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).parents[2]
    source = _make_payload(repo_root, tmp_path / "payload")
    (source / ".git").mkdir()

    def fail_git_query(*args, **kwargs):
        raise OSError("host-specific git failure")

    monkeypatch.setattr(full_patch.subprocess, "run", fail_git_query)
    destination = tmp_path / "installed" / "local_backend"

    with pytest.raises(
        PatchInstallError,
        match="PATCH_PAYLOAD_GIT_QUERY_FAILED",
    ):
        copy_project_payload(source, destination)
    assert not destination.exists()


def test_copy_payload_git_query_timeout_is_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).parents[2]
    source = _make_payload(repo_root, tmp_path / "payload")
    (source / ".git").mkdir()
    observed: dict[str, object] = {}

    def timeout_git_query(command, **kwargs):
        observed["timeout"] = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    monkeypatch.setattr(full_patch.subprocess, "run", timeout_git_query)
    destination = tmp_path / "installed" / "local_backend"

    with pytest.raises(
        PatchInstallError,
        match="PATCH_PAYLOAD_GIT_QUERY_FAILED",
    ):
        copy_project_payload(source, destination)
    assert observed["timeout"] == 10
    assert not destination.exists()


def test_copy_payload_rejects_missing_original_client_runtime(
    tmp_path: Path,
) -> None:
    source = tmp_path / "payload"
    source.mkdir()
    for name in PAYLOAD_REQUIRED_ROOT_FILES - {"original_client_server.py"}:
        (source / name).write_text("# fixture", encoding="utf-8")

    with pytest.raises(PatchInstallError, match="PATCH_PAYLOAD_INCOMPLETE"):
        copy_project_payload(source, tmp_path / "destination")


def test_copy_payload_rejects_missing_control_center(tmp_path: Path) -> None:
    source = tmp_path / "payload"
    source.mkdir()
    for name in PAYLOAD_REQUIRED_ROOT_FILES:
        (source / name).write_text("# fixture", encoding="utf-8")

    with pytest.raises(PatchInstallError, match="PATCH_PAYLOAD_INCOMPLETE"):
        copy_project_payload(source, tmp_path / "destination")


def test_copy_payload_rejects_incomplete_control_center(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).parents[2]
    source = _make_payload(repo_root, tmp_path / "payload")
    (source / "control_center" / "app.py").unlink()

    with pytest.raises(PatchInstallError, match="PATCH_PAYLOAD_INCOMPLETE"):
        copy_project_payload(source, tmp_path / "destination")


@pytest.fixture
def fixture_inputs(
    tmp_path: Path,
) -> tuple[Path, Path, Path, str, str]:
    official, feapp_digest, webplayer_digest = _make_official(
        tmp_path / "official"
    )
    repo_root = Path(__file__).parents[2]
    payload = _make_payload(repo_root, tmp_path / "payload")
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        feapp_digest,
        webplayer_digest,
    )
    return (
        official,
        payload,
        manifest,
        feapp_digest,
        webplayer_digest,
    )


def test_install_isolated_copy_activates_original_client_surfaces(
    fixture_inputs,
    tmp_path: Path,
) -> None:
    official, payload, manifest, feapp_digest, webplayer_digest = fixture_inputs
    resources = official / CURRENT_TEST_CLIENT_VERSION / "resources"
    source_feapp = resources / "feapp.dat"
    source_webplayer = resources / "webplayer.dat"
    source_feapp_bytes = source_feapp.read_bytes()
    source_webplayer_bytes = source_webplayer.read_bytes()

    result = install_full_patch(
        official,
        tmp_path / "installed",
        payload,
        manifest,
    )
    installed = tmp_path / "installed"

    assert result["status"] == "INSTALLED"
    assert result["original_client_only"] is True
    assert result["companion_settings_embedded"] is True
    assert result["webplayer_local_media"] is True
    assert result["companion_settings_status"] == "PATCHED"
    assert result["webplayer_patch_status"] == "PATCHED"

    assert source_feapp.read_bytes() == source_feapp_bytes
    assert source_webplayer.read_bytes() == source_webplayer_bytes
    assert _sha256(source_feapp) == feapp_digest
    assert _sha256(source_webplayer) == webplayer_digest

    installed_resources = (
        installed / "app" / CURRENT_TEST_CLIENT_VERSION / "resources"
    )
    patched_feapp = installed_resources / "feapp.dat"
    patched_webplayer = installed_resources / "webplayer.dat"
    assert _sha256(patched_feapp) != feapp_digest
    assert _sha256(patched_webplayer) != webplayer_digest
    assert _sha256(Path(str(patched_feapp) + ".orig")) == feapp_digest
    assert _sha256(Path(str(patched_webplayer) + ".orig")) == webplayer_digest
    assert result["companion_backup_feapp_sha256"] == _sha256(
        Path(str(patched_feapp) + ".companion.orig")
    )

    with zipfile.ZipFile(patched_feapp) as archive:
        names = set(archive.namelist())
        index = archive.read("index.html").decode("utf-8")
        main = archive.read("assets/main-917d29fc.js").decode("utf-8")
    assert "assets/olivia-companion-settings.js" in names
    assert "data-olivia-companion-settings" in index
    assert "data-ui-version=\"p03.original-settings-manage.v9\"" in index
    assert (installed / "local_backend" / "original_client_setup_api.py").is_file()
    assert (installed / "local_backend" / "original_client_capability_api.py").is_file()
    assert (installed / "local_backend" / "original_client_update_api.py").is_file()
    assert "toyApiUrl" in main
    assert "await t.replace({name:ye.Collection})" in main

    with zipfile.ZipFile(patched_webplayer) as archive:
        names = set(archive.namelist())
        index = archive.read("index.html").decode("utf-8")
    assert "assets/olivia-local-media-bootstrap.js" in names
    assert "data-olivia-local-media-bootstrap" in index

    for name in PAYLOAD_REQUIRED_ROOT_FILES:
        assert (installed / "local_backend" / name).is_file()
    assert not (installed / "app" / "letter_pairs.json").exists()
    assert not (installed / "app" / "memory_store.json").exists()
    assert not (installed / "app" / "llm_config.json").exists()
    assert not (installed / "local_backend" / "letter_pairs.json").exists()
    assert not (installed / "local_backend" / "memory_store.json").exists()
    assert not (installed / "local_backend" / "llm_config.json").exists()
    assert (installed / "CONFIGURE.cmd").is_file()
    start_hidden = (installed / "START.vbs").read_text(encoding="utf-8")
    assert "START.cmd" in start_hidden
    assert ".Run" in start_hidden
    assert ", 0, False" in start_hidden
    start = (installed / "START.cmd").read_text(encoding="utf-8")
    assert "launcher\\version_launcher.py" in start
    assert "runtime\\python-3.12.10-embed-amd64\\python.exe" in start
    assert "set PYTHON_EXE=%ROOT%..\\runtime\\python-3.12.10-embed-amd64\\python.exe" in start
    uninstall = (installed / "UNINSTALL.cmd").read_text(encoding="utf-8")
    assert "launcher\\version_launcher.py" in uninstall
    assert "installer_main" not in uninstall
    for script in (
        installed / "START.cmd",
        installed / "CONFIGURE.cmd",
        installed / "UNINSTALL.cmd",
    ):
        value = script.read_text(encoding="utf-8").lower()
        assert "%localappdata%" not in value
        assert "%root%..\\runtime\\python-3.12.10-embed-amd64\\python.exe" in value
        assert "d:/" not in value
        assert "f:/" not in value
        assert "sk-" not in value


def test_install_is_idempotent_and_unknown_target_is_not_overwritten(
    fixture_inputs,
    tmp_path: Path,
) -> None:
    official, payload, manifest, _feapp, _webplayer = fixture_inputs
    target = tmp_path / "installed"
    first = install_full_patch(official, target, payload, manifest)
    assert first["status"] == "INSTALLED"
    (target / "local_backend" / "installer" / "start_local.py").write_text(
        "old launcher",
        encoding="utf-8",
    )
    (target / "local_backend" / "installer" / "uninstall_safety.py").write_text(
        "old uninstaller",
        encoding="utf-8",
    )
    (target / "START.cmd").write_text("old start", encoding="utf-8")
    for name in ("data", "logs", "third-party"):
        directory = target / name
        directory.mkdir(exist_ok=True)
        (directory / "preserve.txt").write_text(name, encoding="utf-8")

    repeated = install_full_patch(
        official,
        target,
        payload,
        manifest,
    )
    assert repeated["status"] == "ALREADY_INSTALLED"
    assert (target / "local_backend" / "installer" / "start_local.py").read_text(
        encoding="utf-8"
    ) != "old launcher"
    assert (target / "local_backend" / "installer" / "uninstall_safety.py").read_text(
        encoding="utf-8"
    ) != "old uninstaller"
    assert "version_launcher.py" in (target / "START.cmd").read_text(
        encoding="utf-8"
    )
    assert "runtime/mem0-site-packages" in repeated["owned_paths"]
    for name in ("data", "logs", "third-party"):
        assert (target / name / "preserve.txt").read_text(encoding="utf-8") == name

    unknown = tmp_path / "unknown"
    unknown.mkdir()
    (unknown / "user-file.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(PatchInstallError, match="INSTALL_ROOT_ALREADY_EXISTS"):
        install_full_patch(official, unknown, payload, manifest)
    assert (
        unknown / "user-file.txt"
    ).read_text(encoding="utf-8") == "keep"


def test_install_scripts_dispatch_through_the_stable_version_launcher(
    fixture_inputs,
    tmp_path: Path,
) -> None:
    official, payload, manifest, _feapp, _webplayer = fixture_inputs
    target = tmp_path / "installed"

    install_full_patch(official, target, payload, manifest)

    stable_launcher = target / "launcher" / "version_launcher.py"
    assert stable_launcher.read_bytes() == (
        payload / "installer" / "version_launcher.py"
    ).read_bytes()
    actions = {
        "START.cmd": "start",
        "CONFIGURE.cmd": "configure",
        "UNINSTALL.cmd": "uninstall",
    }
    for name, action in actions.items():
        script = (target / name).read_text(encoding="utf-8")
        assert "launcher\\version_launcher.py" in script
        assert f'--install-root "%ROOT%." {action}' in script
        assert "local_backend\\installer" not in script


def test_repeat_install_atomically_replaces_managed_payload_without_stale_files(
    fixture_inputs,
    tmp_path: Path,
) -> None:
    official, payload, manifest, _feapp, _webplayer = fixture_inputs
    target = tmp_path / "installed"
    install_full_patch(official, target, payload, manifest)
    stale = target / "local_backend" / "removed_module.py"
    stale.write_text("old managed module", encoding="utf-8")

    repeated = install_full_patch(official, target, payload, manifest)

    assert repeated["status"] == "ALREADY_INSTALLED"
    assert not stale.exists()


def test_repeat_install_rolls_back_the_active_payload_when_publish_fails(
    fixture_inputs,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    official, payload, manifest, _feapp, _webplayer = fixture_inputs
    target = tmp_path / "installed"
    install_full_patch(official, target, payload, manifest)
    launcher = target / "local_backend" / "installer" / "start_local.py"
    launcher.write_text("old launcher", encoding="utf-8")
    original_replace = full_patch.os.replace

    def fail_staged_start(source: str | Path, destination: str | Path) -> None:
        if Path(source).name == "START.cmd":
            raise OSError("synthetic publish failure")
        original_replace(source, destination)

    monkeypatch.setattr(full_patch.os, "replace", fail_staged_start)

    with pytest.raises(PatchInstallError, match="PATCH_PAYLOAD_REFRESH_FAILED"):
        install_full_patch(official, target, payload, manifest)

    assert launcher.read_text(encoding="utf-8") == "old launcher"


def test_incomplete_old_marker_is_not_treated_as_current_install(
    fixture_inputs,
    tmp_path: Path,
) -> None:
    official, payload, manifest, _feapp, _webplayer = fixture_inputs
    target = tmp_path / "installed"
    install_full_patch(official, target, payload, manifest)
    marker_path = target / ".olivia-full-patch.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["companion_settings_embedded"] = False
    marker_path.write_text(json.dumps(marker), encoding="utf-8")

    with pytest.raises(PatchInstallError, match="INSTALL_ROOT_ALREADY_EXISTS"):
        install_full_patch(official, target, payload, manifest)


@pytest.mark.parametrize("field", ["feapp_sha256", "webplayer_sha256"])
def test_install_rejects_bad_source_hash_before_target_write(
    fixture_inputs,
    tmp_path: Path,
    field: str,
) -> None:
    official, payload, manifest, _feapp, _webplayer = fixture_inputs
    bad = json.loads(manifest.read_text(encoding="utf-8"))
    bad[field] = "0" * 64
    manifest.write_text(json.dumps(bad), encoding="utf-8")
    target = tmp_path / "installed"
    with pytest.raises(PatchInstallError, match="UNSUPPORTED_OFFICIAL_VERSION"):
        install_full_patch(official, target, payload, manifest)
    assert not target.exists()


def test_install_rejects_official_source_overlap(
    fixture_inputs,
) -> None:
    official, payload, manifest, _feapp, _webplayer = fixture_inputs
    with pytest.raises(PatchInstallError, match="INSTALL_ROOT_OVERLAPS_OFFICIAL"):
        install_full_patch(official, official / "nested", payload, manifest)


def test_discovery_uses_appmanifest_without_fixed_drive(tmp_path: Path) -> None:
    steam = tmp_path / "Steam"
    official = steam / "steamapps" / "common" / "BSide Olivia Lin Test"
    _make_official(official)
    apps = steam / "steamapps"
    apps.mkdir(exist_ok=True)
    (apps / "appmanifest_4532590.acf").write_text(
        '"installdir" "BSide Olivia Lin Test"',
        encoding="utf-8",
    )
    assert discover_steam_install([steam]) == official.resolve()


def _make_windows_junction(link: Path, target: Path) -> Path:
    if os.name != "nt":
        pytest.skip("Windows junctions are only available on Windows")
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("Windows junctions are unavailable")
    assert getattr(link.lstat(), "st_file_attributes", 0) & 0x0400
    return link


def test_uninstall_is_dry_run_then_removes_only_owned_paths(
    fixture_inputs,
    tmp_path: Path,
) -> None:
    official, payload, manifest, _feapp, _webplayer = fixture_inputs
    target = tmp_path / "installed"
    install_full_patch(official, target, payload, manifest)
    (target / "data" / "letters.json").write_text(
        "user data",
        encoding="utf-8",
    )
    (target / "logs").mkdir()
    (target / "third-party").mkdir()
    assert uninstall_full_patch(target)["status"] == "DRY_RUN"
    assert (target / "app").is_dir()
    assert uninstall_full_patch(target, apply=True)["status"] == "UNINSTALLED"
    assert not (target / "app").exists()
    assert (
        target / "data" / "letters.json"
    ).read_text(encoding="utf-8") == "user data"
    assert (target / "third-party").is_dir()


def test_uninstall_ignores_marker_owned_paths_and_preserves_outside_sentinel(
    fixture_inputs,
    tmp_path: Path,
) -> None:
    official, payload, manifest, _feapp, _webplayer = fixture_inputs
    target = tmp_path / "installed"
    install_full_patch(official, target, payload, manifest)
    outside = tmp_path / "sentinel"
    outside.write_text("keep", encoding="utf-8")
    unknown = target / "user-file.txt"
    unknown.write_text("keep", encoding="utf-8")
    marker_path = target / ".olivia-full-patch.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["owned_paths"] = ["..\\sentinel", "../sentinel"]
    marker_path.write_text(json.dumps(marker), encoding="utf-8")

    result = uninstall_full_patch(target, apply=True)

    assert result["status"] == "UNINSTALLED"
    assert outside.read_text(encoding="utf-8") == "keep"
    assert unknown.read_text(encoding="utf-8") == "keep"


def test_uninstall_fails_closed_for_owned_symlink(
    fixture_inputs,
    tmp_path: Path,
) -> None:
    official, payload, manifest, _feapp, _webplayer = fixture_inputs
    target = tmp_path / "installed"
    install_full_patch(official, target, payload, manifest)
    outside = tmp_path / "outside"
    outside.mkdir()
    app = target / "app"
    shutil.rmtree(app)
    try:
        app.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")

    with pytest.raises(PatchInstallError, match="PATCH_MARKER_INVALID"):
        uninstall_full_patch(target, apply=True)

    assert outside.is_dir()


def test_uninstall_rejects_install_root_junction_before_external_delete(
    fixture_inputs,
    tmp_path: Path,
) -> None:
    official, payload, manifest, _feapp, _webplayer = fixture_inputs
    external = tmp_path / "external-install"
    install_full_patch(official, external, payload, manifest)
    sentinel = external / "data" / "junction-sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    marker_path = external / ".olivia-full-patch.json"
    marker_before = marker_path.read_bytes()
    junction = _make_windows_junction(tmp_path / "install-junction", external)

    with pytest.raises(PatchInstallError, match="PATCH_MARKER_INVALID"):
        uninstall_full_patch(junction, apply=True)

    assert (external / "app").is_dir()
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert marker_path.read_bytes() == marker_before


def test_standalone_uninstall_ignores_marker_owned_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "installed"
    root.mkdir()
    for name in (
        "app",
        "local_backend",
        "START.cmd",
        "CONFIGURE.cmd",
        "UNINSTALL.cmd",
    ):
        path = root / name
        if "." in name:
            path.write_text("managed", encoding="utf-8")
        else:
            path.mkdir()
    outside = tmp_path / "sentinel"
    outside.write_text("keep", encoding="utf-8")
    marker = {
        "schema_version": "olivia.full-patch.install.v2",
        "owned_root": str(root.resolve()),
        "owned_paths": ["..\\sentinel", "../sentinel"],
    }
    marker_path = root / ".olivia-full-patch.json"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    script = Path(__file__).parents[2] / "installer" / "uninstall.py"

    result = subprocess.run(
        [
            os.fspath(Path(os.sys.executable)),
            str(script),
            "--installation",
            str(root),
            "--apply",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert outside.read_text(encoding="utf-8") == "keep"
    assert not (root / "app").exists()


def test_standalone_uninstall_rejects_install_root_junction_before_external_delete(
    fixture_inputs,
    tmp_path: Path,
) -> None:
    official, payload, manifest, _feapp, _webplayer = fixture_inputs
    external = tmp_path / "external-install"
    install_full_patch(official, external, payload, manifest)
    sentinel = external / "data" / "junction-sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    marker_path = external / ".olivia-full-patch.json"
    marker_before = marker_path.read_bytes()
    junction = _make_windows_junction(tmp_path / "install-junction", external)
    script = Path(__file__).parents[2] / "installer" / "uninstall.py"

    result = subprocess.run(
        [
            os.fspath(Path(os.sys.executable)),
            str(script),
            "--installation",
            str(junction),
            "--apply",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2, result.stderr or result.stdout
    assert "PATCH_MARKER_INVALID" in result.stdout
    assert (external / "app").is_dir()
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert marker_path.read_bytes() == marker_before


def test_start_resolves_isolated_client_and_never_launcher(
    fixture_inputs,
    tmp_path: Path,
) -> None:
    official, payload, manifest, _feapp, _webplayer = fixture_inputs
    target = tmp_path / "installed"
    install_full_patch(official, target, payload, manifest)
    assert _client_executable(target) == (
        target / "app" / CURRENT_TEST_CLIENT_VERSION / "Olivia.exe"
    )
    start = (target / "START.cmd").read_text(encoding="utf-8")
    assert "launcher.exe" not in start.lower()
