from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from installer.full_patch import (
    PatchInstallError,
    discover_steam_install,
    install_full_patch,
    uninstall_full_patch,
)
from installer.start_local import _client_command, _client_environment, _client_executable


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


def test_published_powershell_scripts_are_utf8_bom_safe() -> None:
    repo_root = Path(__file__).parents[2]
    for relative in (Path("installer") / "Install.ps1", Path("tools") / "Install-ThirdParty.ps1"):
        raw = (repo_root / relative).read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), f"{relative} must be UTF-8 BOM for Windows PowerShell 5.1"
        raw.decode("utf-8-sig")


def test_install_cmd_preserves_paths_with_spaces_for_windows_powershell(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("cmd.exe is only available on Windows")

    repo_root = Path(__file__).parents[2]
    package = tmp_path / "cmd package with spaces"
    package.mkdir()
    (package / "installer").mkdir()
    shutil.copy2(repo_root / "INSTALL.cmd", package / "INSTALL.cmd")
    (package / "installer" / "Install.ps1").write_text("# fixture", encoding="utf-8")
    capture = package / "captured-powershell-arguments.txt"
    (package / "powershell.cmd").write_text(
        '@echo off\n'
        f'>"{capture}" echo %*\n'
        'exit /b 0\n',
        encoding="ascii",
    )

    env = os.environ.copy()
    env["PATH"] = str(package) + os.pathsep + env.get("PATH", "")
    result = subprocess.run(
        [os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"), "/d", "/c", "call INSTALL.cmd"],
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
    assert "Install.ps1\" -PayloadRoot" in args


def test_start_local_matches_first_party_client_launch_contract(tmp_path: Path) -> None:
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


def _make_official(root: Path) -> tuple[Path, str]:
    version_root = root / "0.0.9.615"
    resources = version_root / "resources"
    resources.mkdir(parents=True)
    (root / "launcher.exe").write_bytes(b"official launcher fixture")
    (root / "letter_pairs.json").write_text("synthetic user letters", encoding="utf-8")
    (root / "memory_store.json").write_text("synthetic user memory", encoding="utf-8")
    (root / "llm_config.json").write_text('{"api_key":"synthetic"}', encoding="utf-8")
    (version_root / "Olivia.exe").write_bytes(b"official client fixture")
    javascript = (
        "prefix "
        "He=e=>new Promise((t,n)=>{try{"
        ',"query.response":no(a)}}),t(c)},onFailure:\'suffix\' '
        '!z.isNew||N?(await t.replace({name:ye.Home}),await h(z.uid.toString(),z.modelGatewayToken||"",!1))'
    )
    archive = resources / "feapp.dat"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("assets/main-917d29fc.js", javascript)
    return root, _sha256(archive)


def _write_manifest(path: Path, feapp_sha256: str) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": "olivia.full-patch.v2",
                "steam_app_id": "4532590",
                "client_version": "0.0.9.615",
                "feapp_sha256": feapp_sha256,
                "patch_mode": "isolated-copy",
                "live_status": "UNAVAILABLE_PAUSED",
                "media_status": "TEXT_ONLY_MAIN_BASELINE",
            }
        ),
        encoding="utf-8",
    )
    return path


def _make_payload(repo_root: Path, root: Path) -> Path:
    root.mkdir()
    shutil.copy2(repo_root / "local_server.py", root / "local_server.py")
    shutil.copy2(repo_root / "patch_feapp.py", root / "patch_feapp.py")
    shutil.copytree(repo_root / "installer", root / "installer")
    return root


def test_copy_payload_excludes_non_runtime_project_files(tmp_path: Path) -> None:
    from installer.full_patch import copy_project_payload

    source = tmp_path / "payload"
    destination = tmp_path / "installed" / "local_backend"
    source.mkdir()
    for name in ("local_server.py", "patch_feapp.py", "dynamic_renderer.py"):
        (source / name).write_text("# fixture", encoding="utf-8")
    for name in ("test_fixture.py", "pytest.ini", "requirements-dev.txt", "requirements-dev-extra.txt"):
        (source / name).write_text("fixture", encoding="utf-8")
    (source / ".evidence").mkdir()
    (source / ".evidence" / "capture.json").write_text("{}", encoding="utf-8")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "fixture.pyc").write_bytes(b"pyc")

    copied = copy_project_payload(source, destination)

    assert "dynamic_renderer.py" in copied
    assert (destination / "dynamic_renderer.py").is_file()
    for name in ("test_fixture.py", "pytest.ini", "requirements-dev.txt", "requirements-dev-extra.txt"):
        assert name not in copied
        assert not (destination / name).exists()
    assert not (destination / ".evidence").exists()
    assert not (destination / "__pycache__").exists()


@pytest.fixture
def fixture_inputs(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    official, digest = _make_official(tmp_path / "official")
    repo_root = Path(__file__).parents[2]
    payload = _make_payload(repo_root, tmp_path / "payload")
    manifest = _write_manifest(tmp_path / "manifest.json", digest)
    return official, payload, manifest, digest


def test_install_isolated_copy_patches_only_staged_client(fixture_inputs, tmp_path: Path):
    official, payload, manifest, digest = fixture_inputs
    source_feapp = official / "0.0.9.615" / "resources" / "feapp.dat"
    source_bytes = source_feapp.read_bytes()
    result = install_full_patch(official, tmp_path / "installed", payload, manifest)
    installed = tmp_path / "installed"

    assert result["status"] == "INSTALLED"
    assert source_feapp.read_bytes() == source_bytes
    assert _sha256(source_feapp) == digest
    patched = installed / "app" / "0.0.9.615" / "resources" / "feapp.dat"
    assert _sha256(patched) != digest
    assert _sha256(Path(str(patched) + ".orig")) == digest
    assert (installed / "local_backend" / "patch_feapp.py").is_file()
    assert not (installed / "app" / "letter_pairs.json").exists()
    assert not (installed / "app" / "memory_store.json").exists()
    assert not (installed / "app" / "llm_config.json").exists()
    assert not (installed / "local_backend" / "letter_pairs.json").exists()
    assert not (installed / "local_backend" / "memory_store.json").exists()
    assert not (installed / "local_backend" / "llm_config.json").exists()
    assert (installed / "CONFIGURE.cmd").is_file()
    assert "installer\\start_local.py" in (installed / "START.cmd").read_text(encoding="utf-8")
    assert "runtime\\python-3.12.10-embed-amd64\\python.exe" in (installed / "START.cmd").read_text(encoding="utf-8")
    uninstall = (installed / "UNINSTALL.cmd").read_text(encoding="utf-8")
    assert "installer\\uninstall.py" in uninstall
    assert "installer_main" not in uninstall
    for script in (installed / "START.cmd", installed / "UNINSTALL.cmd"):
        text = script.read_text(encoding="utf-8").lower()
        assert "d:/" not in text and "f:/" not in text and "sk-" not in text


def test_install_is_idempotent_and_unknown_target_is_not_overwritten(fixture_inputs, tmp_path: Path):
    official, payload, manifest, _ = fixture_inputs
    target = tmp_path / "installed"
    first = install_full_patch(official, target, payload, manifest)
    assert first["status"] == "INSTALLED"
    assert install_full_patch(official, target, payload, manifest)["status"] == "ALREADY_INSTALLED"

    unknown = tmp_path / "unknown"
    unknown.mkdir()
    (unknown / "user-file.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(PatchInstallError, match="INSTALL_ROOT_ALREADY_EXISTS"):
        install_full_patch(official, unknown, payload, manifest)
    assert (unknown / "user-file.txt").read_text(encoding="utf-8") == "keep"


def test_install_rejects_bad_source_hash_before_target_write(fixture_inputs, tmp_path: Path):
    official, payload, manifest, _ = fixture_inputs
    bad = json.loads(manifest.read_text(encoding="utf-8"))
    bad["feapp_sha256"] = "0" * 64
    manifest.write_text(json.dumps(bad), encoding="utf-8")
    target = tmp_path / "installed"
    with pytest.raises(PatchInstallError, match="UNSUPPORTED_OFFICIAL_VERSION"):
        install_full_patch(official, target, payload, manifest)
    assert not target.exists()


def test_install_rejects_official_source_overlap(fixture_inputs, tmp_path: Path):
    official, payload, manifest, _ = fixture_inputs
    with pytest.raises(PatchInstallError, match="INSTALL_ROOT_OVERLAPS_OFFICIAL"):
        install_full_patch(official, official / "nested", payload, manifest)


def test_discovery_uses_appmanifest_without_fixed_drive(tmp_path: Path):
    steam = tmp_path / "Steam"
    official = steam / "steamapps" / "common" / "BSide Olivia Lin Test"
    _make_official(official)
    apps = steam / "steamapps"
    apps.mkdir(exist_ok=True)
    (apps / "appmanifest_4532590.acf").write_text('"installdir" "BSide Olivia Lin Test"', encoding="utf-8")
    assert discover_steam_install([steam]) == official.resolve()


def test_uninstall_is_dry_run_then_removes_only_owned_paths(fixture_inputs, tmp_path: Path):
    official, payload, manifest, _ = fixture_inputs
    target = tmp_path / "installed"
    install_full_patch(official, target, payload, manifest)
    (target / "data" / "letters.json").write_text("user data", encoding="utf-8")
    (target / "logs").mkdir()
    (target / "third-party").mkdir()
    assert uninstall_full_patch(target)["status"] == "DRY_RUN"
    assert (target / "app").is_dir()
    assert uninstall_full_patch(target, apply=True)["status"] == "UNINSTALLED"
    assert not (target / "app").exists()
    assert (target / "data" / "letters.json").read_text(encoding="utf-8") == "user data"
    assert (target / "third-party").is_dir()


def test_start_resolves_isolated_client_and_never_launcher(fixture_inputs, tmp_path: Path):
    official, payload, manifest, _ = fixture_inputs
    target = tmp_path / "installed"
    install_full_patch(official, target, payload, manifest)
    assert _client_executable(target) == target / "app" / "0.0.9.615" / "Olivia.exe"
    start = (target / "START.cmd").read_text(encoding="utf-8")
    assert "launcher.exe" not in start.lower()
