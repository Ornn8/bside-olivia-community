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
    start = install_root / "START.cmd"
    shortcut = tmp_path / "desktop" / "Olivia-local.lnk"
    client.parent.mkdir(parents=True)
    client.write_bytes(b"synthetic client")
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
                "[pscustomobject]@{target=$s.TargetPath;working=$s.WorkingDirectory;"
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
    assert Path(metadata["working"]) == install_root
    assert metadata["icon"] == f"{client},0"


def test_install_entrypoint_prioritizes_selected_payload() -> None:
    repo_root = Path(__file__).parents[2]
    script = (repo_root / "installer" / "Install.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert "sys.path.insert(0,sys.argv.pop(1))" in script
    assert 'runpy.run_module("installer",run_name="__main__")' in script


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


def _run_managed_python_path_helper(
    *,
    pth_path: Path,
    site_packages: Path,
    payload_root: Path,
    ownership_path: Path,
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
        "Update-ManagedPythonPath -PthPath $env:BSIDE_PTH_PATH "
        "-SitePackages $env:BSIDE_SITE_PACKAGES "
        "-PayloadRoot $env:BSIDE_PAYLOAD_ROOT "
        "-OwnershipPath $env:BSIDE_OWNERSHIP_PATH"
    )
    env = os.environ.copy()
    env.update(
        {
            "BSIDE_INSTALL_SCRIPT": str(repo_root / "installer" / "Install.ps1"),
            "BSIDE_PTH_PATH": str(pth_path),
            "BSIDE_SITE_PACKAGES": str(site_packages),
            "BSIDE_PAYLOAD_ROOT": str(payload_root),
            "BSIDE_OWNERSHIP_PATH": str(ownership_path),
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


def test_managed_runtime_pth_stays_portable_across_install_and_upgrade(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows PowerShell is only available on Windows")

    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    pth_path = runtime_root / "python312._pth"
    site_packages = runtime_root / "site-packages"
    first_payload = tmp_path / "first payload"
    ownership_path = runtime_root / ".bside-owned-payload-root"
    pth_path.write_text(
        "python312.zip\n.\n#import site\n",
        encoding="utf-8",
    )

    result = _run_managed_python_path_helper(
        pth_path=pth_path,
        site_packages=site_packages,
        payload_root=first_payload,
        ownership_path=ownership_path,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert pth_path.read_text(encoding="utf-8").splitlines() == [
        "python312.zip",
        ".",
        "#import site",
        "site-packages",
        "import site",
    ]
    assert ownership_path.read_text(encoding="utf-8").splitlines() == [
        str(first_payload)
    ]

    current_payload = tmp_path / "current payload"
    unrelated_root = tmp_path / "user python modules"
    pth_path.write_text(
        "\n".join(
            (
                "python312.zip",
                ".",
                str(current_payload),
                str(first_payload),
                str(unrelated_root),
                str(site_packages),
                "import site",
                "import site",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_managed_python_path_helper(
        pth_path=pth_path,
        site_packages=site_packages,
        payload_root=current_payload,
        ownership_path=ownership_path,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert pth_path.read_text(encoding="utf-8").splitlines() == [
        "python312.zip",
        ".",
        str(unrelated_root),
        "site-packages",
        "import site",
    ]
    assert ownership_path.read_text(encoding="utf-8").splitlines() == [
        str(current_payload)
    ]


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
    version_root = root / "0.0.9.615"
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
                "client_version": "0.0.9.615",
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
    assert not (destination / "runtime" / "packaging").exists()
    assert "dynamic_renderer.py" in copied
    assert (destination / "dynamic_renderer.py").is_file()
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
    resources = official / "0.0.9.615" / "resources"
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
        installed / "app" / "0.0.9.615" / "resources"
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
    assert "data-ui-version=\"p03.original-settings-manage.v1\"" in index
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
    start = (installed / "START.cmd").read_text(encoding="utf-8")
    assert "installer\\start_local.py" in start
    assert "runtime\\python-3.12.10-embed-amd64\\python.exe" in start
    uninstall = (installed / "UNINSTALL.cmd").read_text(encoding="utf-8")
    assert "installer\\uninstall.py" in uninstall
    assert "installer_main" not in uninstall
    for script in (installed / "START.cmd", installed / "UNINSTALL.cmd"):
        value = script.read_text(encoding="utf-8").lower()
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
    assert install_full_patch(
        official,
        target,
        payload,
        manifest,
    )["status"] == "ALREADY_INSTALLED"

    unknown = tmp_path / "unknown"
    unknown.mkdir()
    (unknown / "user-file.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(PatchInstallError, match="INSTALL_ROOT_ALREADY_EXISTS"):
        install_full_patch(official, unknown, payload, manifest)
    assert (
        unknown / "user-file.txt"
    ).read_text(encoding="utf-8") == "keep"


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


def test_start_resolves_isolated_client_and_never_launcher(
    fixture_inputs,
    tmp_path: Path,
) -> None:
    official, payload, manifest, _feapp, _webplayer = fixture_inputs
    target = tmp_path / "installed"
    install_full_patch(official, target, payload, manifest)
    assert _client_executable(target) == (
        target / "app" / "0.0.9.615" / "Olivia.exe"
    )
    start = (target / "START.cmd").read_text(encoding="utf-8")
    assert "launcher.exe" not in start.lower()
