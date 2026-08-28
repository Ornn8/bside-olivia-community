from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

from installer import component_update
from installer import __main__ as installer_cli
from installer import version_launcher
from installer.component_update import (
    ComponentUpdateError,
    apply_component_update,
    rollback_component_update,
)
from installer.uninstall_safety import remove_owned_targets


_REQUIRED_COMPONENT_ENTRYPOINTS = {
    "installer/start_local.py": b"",
    "installer/configure.py": b"",
    "installer/uninstall.py": b"",
}


def _write_component_package(
    path: Path,
    *,
    version: str,
    files: dict[str, bytes],
    include_entrypoints: bool = True,
) -> str:
    package_files = dict(files)
    if include_entrypoints:
        for name, content in _REQUIRED_COMPONENT_ENTRYPOINTS.items():
            package_files.setdefault(name, content)
    manifest = {
        "schema_version": "olivia.component-package.v1",
        "component": "local_backend",
        "version": version,
        "files": [
            {
                "path": name,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for name, content in sorted(package_files.items())
        ],
    }
    manifest_bytes = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", manifest_bytes)
        for name, content in package_files.items():
            archive.writestr(f"payload/{name}", content)
    return hashlib.sha256(manifest_bytes).hexdigest()


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
    return link


def _write_install_marker(installation: Path) -> None:
    (installation / ".olivia-full-patch.json").write_text(
        json.dumps(
            {
                "schema_version": "olivia.full-patch.install.v2",
                "owned_root": str(installation.resolve()),
            }
        ),
        encoding="utf-8",
    )


def _managed_installation(tmp_path: Path) -> tuple[Path, Path]:
    installation = tmp_path / "installation"
    active = installation / "local_backend"
    active.mkdir(parents=True)
    (active / "old.py").write_text("old", encoding="utf-8")
    _write_install_marker(installation)
    return installation, active


def test_valid_local_backend_component_update_is_activated_atomically(
    tmp_path: Path,
) -> None:
    installation, active = _managed_installation(tmp_path)
    package = tmp_path / "local-backend.oliviapatch"
    manifest_sha256 = _write_component_package(
        package,
        version="1.1.0",
        files={"new.py": b"new"},
    )

    result = apply_component_update(
        installation,
        package,
        expected_manifest_sha256=manifest_sha256,
    )

    assert result == {
        "status": "APPLIED",
        "component": "local_backend",
        "version": "1.1.0",
    }
    versioned = (
        installation
        / "versions"
        / "local_backend"
        / f"1.1.0-{manifest_sha256}"
    )
    assert (active / "old.py").read_text(encoding="utf-8") == "old"
    assert not (active / "new.py").exists()
    assert (versioned / "new.py").read_bytes() == b"new"
    assert json.loads(
        (installation / ".olivia-update-state.json").read_text(encoding="utf-8")
    ) == {
        "schema_version": "olivia.update-state.v1",
        "active_components": {
            "local_backend": {
                "version": "1.1.0",
                "manifest_sha256": manifest_sha256,
                "payload_path": versioned.relative_to(installation).as_posix(),
            }
        },
        "previous_components": {
            "local_backend": {
                "version": "0.0.0+legacy",
                "manifest_sha256": "0" * 64,
                "payload_path": "local_backend",
            }
        },
    }


def test_component_update_refreshes_existing_shortcuts_from_the_active_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation, _active = _managed_installation(tmp_path)
    package = tmp_path / "local-backend.oliviapatch"
    manifest_sha256 = _write_component_package(
        package,
        version="1.1.0",
        files={
            "installer/Create-Shortcut.ps1": b"# fixture",
            "installer/assets/olivia.ico": b"icon",
        },
    )
    observed: list[tuple[Path, Path, bool]] = []

    def observe(root: Path, active_version: Path) -> None:
        observed.append(
            (
                root,
                active_version,
                (root / ".olivia-update-state.json").is_file(),
            )
        )

    monkeypatch.setattr(component_update, "_refresh_existing_shortcuts", observe)

    apply_component_update(
        installation,
        package,
        expected_manifest_sha256=manifest_sha256,
    )

    assert observed == [
        (
            installation,
            installation
            / "versions"
            / "local_backend"
            / f"1.1.0-{manifest_sha256}",
            True,
        )
    ]


def test_component_update_stays_applied_when_shortcut_refresh_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation, _active = _managed_installation(tmp_path)
    package = tmp_path / "local-backend.oliviapatch"
    manifest_sha256 = _write_component_package(
        package,
        version="1.1.0",
        files={"new.py": b"new"},
    )

    def fail_refresh(_root: Path, _active_version: Path) -> None:
        raise RuntimeError("synthetic optional shortcut failure")

    monkeypatch.setattr(component_update, "_refresh_existing_shortcuts", fail_refresh)

    result = apply_component_update(
        installation,
        package,
        expected_manifest_sha256=manifest_sha256,
    )

    assert result["status"] == "APPLIED"
    assert (installation / ".olivia-update-state.json").is_file()


@pytest.mark.parametrize("failure", ["probe", "timeout", "nonzero"])
def test_shortcut_refresh_ignores_probe_timeout_and_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    active_version = tmp_path / "active"
    script = active_version / "installer" / "Create-Shortcut.ps1"
    script.parent.mkdir(parents=True)
    script.write_text("# fixture", encoding="utf-8")

    if failure == "probe":
        original_is_file = Path.is_file

        def fail_script_probe(path: Path) -> bool:
            if path == script:
                raise OSError("synthetic probe failure")
            return original_is_file(path)

        monkeypatch.setattr(Path, "is_file", fail_script_probe)
    elif failure == "timeout":
        monkeypatch.setattr(
            component_update.subprocess,
            "run",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired("powershell.exe", 30)
            ),
        )
    else:
        monkeypatch.setattr(
            component_update.subprocess,
            "run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess([], 23),
        )

    component_update._refresh_existing_shortcuts(tmp_path, active_version)


def test_windows_patch_docs_define_shortcut_refresh_as_best_effort() -> None:
    documentation = (
        Path(__file__).parents[2] / "docs" / "WINDOWS_FULL_PATCH.md"
    ).read_text(encoding="utf-8")

    assert "桌面和开始菜单中仍然存在的快捷方式" in documentation
    assert "不会撤销已经完成的补丁激活" in documentation


def test_stable_launcher_resolves_the_atomically_selected_backend(
    tmp_path: Path,
) -> None:
    from installer.version_launcher import resolve_active_backend

    installation, legacy = _managed_installation(tmp_path)
    package = tmp_path / "local-backend.oliviapatch"
    manifest_sha256 = _write_component_package(
        package,
        version="1.1.0",
        files={"new.py": b"new"},
    )

    apply_component_update(
        installation,
        package,
        expected_manifest_sha256=manifest_sha256,
    )

    assert resolve_active_backend(installation) == (
        installation
        / "versions"
        / "local_backend"
        / f"1.1.0-{manifest_sha256}"
    )
    assert version_launcher.main(
        ["--install-root", str(installation), "start"]
    ) == 0
    (installation / ".olivia-update-state.json").unlink()
    assert resolve_active_backend(installation) == legacy


def test_component_package_requires_all_stable_launcher_entrypoints(
    tmp_path: Path,
) -> None:
    installation, legacy = _managed_installation(tmp_path)
    package = tmp_path / "incomplete.oliviapatch"
    manifest_sha256 = _write_component_package(
        package,
        version="1.1.0",
        files={"new.py": b"new"},
        include_entrypoints=False,
    )

    with pytest.raises(ComponentUpdateError, match="UPDATE_MANIFEST_INVALID"):
        apply_component_update(
            installation,
            package,
            expected_manifest_sha256=manifest_sha256,
        )

    assert (legacy / "old.py").read_text(encoding="utf-8") == "old"
    assert not (installation / ".olivia-update-state.json").exists()


def test_stable_launcher_runs_without_importing_the_legacy_backend_first(
    tmp_path: Path,
) -> None:
    installation, legacy = _managed_installation(tmp_path)
    launcher = installation / "launcher" / "version_launcher.py"
    launcher.parent.mkdir()
    shutil.copy2(Path(component_update.__file__).with_name("version_launcher.py"), launcher)
    entrypoint = legacy / "installer" / "start_local.py"
    entrypoint.parent.mkdir()
    entrypoint.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "record = Path(sys.argv[sys.argv.index('--record') + 1])\n"
        "record.write_text('\\n'.join(sys.argv), encoding='utf-8')\n",
        encoding="utf-8",
    )
    record = tmp_path / "launcher-args.txt"

    completed = subprocess.run(
        [
            os.fspath(Path(os.sys.executable)),
            os.fspath(launcher),
            "--install-root",
            os.fspath(installation),
            "start",
            "--record",
            os.fspath(record),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    arguments = record.read_text(encoding="utf-8").splitlines()
    assert arguments[0] == os.fspath(entrypoint)
    assert arguments[1:3] == ["--install-root", os.fspath(installation.resolve())]
    assert arguments[3:] == ["--record", os.fspath(record)]


def test_stable_launcher_cli_rejects_a_reparse_installation_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installation, _legacy = _managed_installation(tmp_path)
    alias = _make_windows_junction(tmp_path / "installation-alias", installation)

    exit_code = version_launcher.main(
        ["--install-root", str(alias), "start"]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "ERROR",
        "code": "UPDATE_INSTALLATION_INVALID",
    }


def test_stable_launcher_rejects_an_invalid_previous_version_descriptor(
    tmp_path: Path,
) -> None:
    installation, _legacy = _managed_installation(tmp_path)
    digest = "a" * 64
    versioned = installation / "versions" / "local_backend" / f"1.1.0-{digest}"
    versioned.mkdir(parents=True)
    (installation / ".olivia-update-state.json").write_text(
        json.dumps(
            {
                "schema_version": "olivia.update-state.v1",
                "active_components": {
                    "local_backend": {
                        "version": "1.1.0",
                        "manifest_sha256": digest,
                        "payload_path": f"versions/local_backend/1.1.0-{digest}",
                    }
                },
                "previous_components": {"local_backend": {"path": "elsewhere"}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        version_launcher.VersionLauncherError,
        match="UPDATE_STATE_INVALID",
    ):
        version_launcher.resolve_active_backend(installation)


def test_next_component_update_preserves_the_previous_version_for_rollback(
    tmp_path: Path,
) -> None:
    installation, legacy = _managed_installation(tmp_path)
    first_package = tmp_path / "first.oliviapatch"
    first_sha256 = _write_component_package(
        first_package,
        version="1.1.0",
        files={"first.py": b"first"},
    )
    second_package = tmp_path / "second.oliviapatch"
    second_sha256 = _write_component_package(
        second_package,
        version="1.2.0",
        files={"second.py": b"second"},
    )
    apply_component_update(
        installation,
        first_package,
        expected_manifest_sha256=first_sha256,
    )

    apply_component_update(
        installation,
        second_package,
        expected_manifest_sha256=second_sha256,
    )

    state = json.loads(
        (installation / ".olivia-update-state.json").read_text(encoding="utf-8")
    )
    assert state["active_components"]["local_backend"] == {
        "version": "1.2.0",
        "manifest_sha256": second_sha256,
        "payload_path": f"versions/local_backend/1.2.0-{second_sha256}",
    }
    assert state["previous_components"]["local_backend"] == {
        "version": "1.1.0",
        "manifest_sha256": first_sha256,
        "payload_path": f"versions/local_backend/1.1.0-{first_sha256}",
    }
    assert (legacy / "old.py").read_text(encoding="utf-8") == "old"


def test_first_component_update_can_roll_back_to_the_legacy_baseline(
    tmp_path: Path,
) -> None:
    from installer.version_launcher import resolve_active_backend

    installation, legacy = _managed_installation(tmp_path)
    package = tmp_path / "first.oliviapatch"
    manifest_sha256 = _write_component_package(
        package,
        version="1.1.0",
        files={"new.py": b"new"},
    )
    apply_component_update(
        installation,
        package,
        expected_manifest_sha256=manifest_sha256,
    )

    result = rollback_component_update(installation)

    assert result == {
        "status": "ROLLED_BACK",
        "component": "local_backend",
        "version": "0.0.0+legacy",
    }
    assert resolve_active_backend(installation) == legacy


def test_reapplying_the_active_descriptor_preserves_the_real_previous_pointer(
    tmp_path: Path,
) -> None:
    installation, _legacy = _managed_installation(tmp_path)
    packages: list[tuple[Path, str]] = []
    for version in ("1.1.0", "1.2.0"):
        package = tmp_path / f"{version}.oliviapatch"
        digest = _write_component_package(
            package,
            version=version,
            files={f"{version}.py": version.encode("utf-8")},
        )
        packages.append((package, digest))
        apply_component_update(
            installation,
            package,
            expected_manifest_sha256=digest,
        )
    state_path = installation / ".olivia-update-state.json"
    before = json.loads(state_path.read_text(encoding="utf-8"))

    apply_component_update(
        installation,
        packages[-1][0],
        expected_manifest_sha256=packages[-1][1],
    )

    assert json.loads(state_path.read_text(encoding="utf-8")) == before


def test_existing_state_requires_an_active_local_backend(
    tmp_path: Path,
) -> None:
    installation, _legacy = _managed_installation(tmp_path)
    state_path = installation / ".olivia-update-state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "olivia.update-state.v1",
                "active_components": {},
                "previous_components": {},
            }
        ),
        encoding="utf-8",
    )
    package = tmp_path / "update.oliviapatch"
    manifest_sha256 = _write_component_package(
        package,
        version="1.1.0",
        files={"new.py": b"new"},
    )

    with pytest.raises(ComponentUpdateError, match="UPDATE_STATE_INVALID"):
        apply_component_update(
            installation,
            package,
            expected_manifest_sha256=manifest_sha256,
        )


def test_rollback_rejects_a_reparse_previous_version_without_changing_state(
    tmp_path: Path,
) -> None:
    from installer.version_launcher import resolve_active_backend

    installation, _legacy = _managed_installation(tmp_path)
    applied: list[tuple[str, str]] = []
    for version in ("1.1.0", "1.2.0"):
        package = tmp_path / f"{version}.oliviapatch"
        digest = _write_component_package(
            package,
            version=version,
            files={f"{version}.py": version.encode("utf-8")},
        )
        apply_component_update(
            installation,
            package,
            expected_manifest_sha256=digest,
        )
        applied.append((version, digest))
    previous_version, previous_digest = applied[0]
    previous_path = (
        installation
        / "versions"
        / "local_backend"
        / f"{previous_version}-{previous_digest}"
    )
    shutil.rmtree(previous_path)
    external = tmp_path / "external"
    external.mkdir()
    _make_windows_junction(previous_path, external)
    state_path = installation / ".olivia-update-state.json"
    state_before = state_path.read_bytes()

    with pytest.raises(ComponentUpdateError, match="UPDATE_STATE_INVALID"):
        rollback_component_update(installation)

    assert state_path.read_bytes() == state_before
    assert resolve_active_backend(installation).name == (
        f"{applied[1][0]}-{applied[1][1]}"
    )


def test_component_update_rolls_back_by_atomically_swapping_the_version_pointer(
    tmp_path: Path,
) -> None:
    from installer.version_launcher import resolve_active_backend

    installation, _legacy = _managed_installation(tmp_path)
    first_package = tmp_path / "first.oliviapatch"
    first_sha256 = _write_component_package(
        first_package,
        version="1.1.0",
        files={"first.py": b"first"},
    )
    second_package = tmp_path / "second.oliviapatch"
    second_sha256 = _write_component_package(
        second_package,
        version="1.2.0",
        files={"second.py": b"second"},
    )
    apply_component_update(
        installation,
        first_package,
        expected_manifest_sha256=first_sha256,
    )
    apply_component_update(
        installation,
        second_package,
        expected_manifest_sha256=second_sha256,
    )

    result = rollback_component_update(installation)

    assert result == {
        "status": "ROLLED_BACK",
        "component": "local_backend",
        "version": "1.1.0",
    }
    assert resolve_active_backend(installation) == (
        installation
        / "versions"
        / "local_backend"
        / f"1.1.0-{first_sha256}"
    )
    state = json.loads(
        (installation / ".olivia-update-state.json").read_text(encoding="utf-8")
    )
    assert state["previous_components"]["local_backend"] == {
        "version": "1.2.0",
        "manifest_sha256": second_sha256,
        "payload_path": f"versions/local_backend/1.2.0-{second_sha256}",
    }


def test_rollback_update_is_available_through_the_public_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installation, _legacy = _managed_installation(tmp_path)
    for version in ("1.1.0", "1.2.0"):
        package = tmp_path / f"{version}.oliviapatch"
        manifest_sha256 = _write_component_package(
            package,
            version=version,
            files={f"{version}.py": version.encode("utf-8")},
        )
        apply_component_update(
            installation,
            package,
            expected_manifest_sha256=manifest_sha256,
        )

    exit_code = installer_cli.main(
        ["rollback-update", "--installation", str(installation)]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "ROLLED_BACK",
        "component": "local_backend",
        "version": "1.1.0",
    }


def test_component_update_contract_examples_match_their_public_schemas() -> None:
    from jsonschema import Draft202012Validator

    contract_root = Path(__file__).parents[2] / "contracts"
    pairs = (
        ("component_update_package.schema.json", "component_update_package.example.json"),
        ("component_update_state.schema.json", "component_update_state.example.json"),
    )
    for schema_name, example_name in pairs:
        schema = json.loads((contract_root / schema_name).read_text(encoding="utf-8"))
        example = json.loads((contract_root / example_name).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        assert not list(Draft202012Validator(schema).iter_errors(example))


def test_component_update_rejects_payload_path_escape_before_activation(
    tmp_path: Path,
) -> None:
    installation, active = _managed_installation(tmp_path)
    package = tmp_path / "unsafe.oliviapatch"
    manifest_sha256 = _write_component_package(
        package,
        version="1.1.0",
        files={"../escape.py": b"escape"},
    )

    with pytest.raises(ComponentUpdateError, match="UPDATE_MANIFEST_INVALID"):
        apply_component_update(
            installation,
            package,
            expected_manifest_sha256=manifest_sha256,
        )

    assert (active / "old.py").read_text(encoding="utf-8") == "old"
    assert not (installation / "escape.py").exists()
    assert not (installation / ".olivia-update-state.json").exists()
    assert not list(installation.glob(".olivia-update-staging-*"))


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "same.py.",
        "same.py ",
        "module.py:shadow",
        "NUL",
        "con.txt",
        "folder/PRN.dat",
    ],
)
def test_component_update_rejects_windows_aliases_before_activation(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    installation, active = _managed_installation(tmp_path)
    package = tmp_path / "unsafe-windows-name.oliviapatch"
    manifest_sha256 = _write_component_package(
        package,
        version="1.1.0",
        files={unsafe_name: b"unsafe"},
    )

    with pytest.raises(ComponentUpdateError, match="UPDATE_MANIFEST_INVALID"):
        apply_component_update(
            installation,
            package,
            expected_manifest_sha256=manifest_sha256,
        )

    assert (active / "old.py").read_text(encoding="utf-8") == "old"
    assert not (installation / ".olivia-update-state.json").exists()


def test_component_update_rejects_non_regular_zip_members(
    tmp_path: Path,
) -> None:
    installation, active = _managed_installation(tmp_path)
    package = tmp_path / "symlink-member.oliviapatch"
    content = b"target.py"
    manifest = {
        "schema_version": "olivia.component-package.v1",
        "component": "local_backend",
        "version": "1.1.0",
        "files": [
            {
                "path": "link.py",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
    }
    manifest_bytes = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    link = zipfile.ZipInfo("payload/link.py")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("manifest.json", manifest_bytes)
        archive.writestr(link, content)

    with pytest.raises(ComponentUpdateError, match="UPDATE_PACKAGE_INVALID"):
        apply_component_update(
            installation,
            package,
            expected_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        )

    assert (active / "old.py").read_text(encoding="utf-8") == "old"
    assert not (installation / ".olivia-update-state.json").exists()


def test_component_update_reenumerates_the_staged_tree_before_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation, active = _managed_installation(tmp_path)
    package = tmp_path / "local-backend.oliviapatch"
    manifest_sha256 = _write_component_package(
        package,
        version="1.1.0",
        files={"new.py": b"new"},
    )
    original_write_bytes = Path.write_bytes

    def inject_extra_file(path: Path, content: bytes) -> int:
        written = original_write_bytes(path, content)
        if path.name == "new.py":
            with (path.parent / "unexpected.py").open("wb") as stream:
                stream.write(b"unexpected")
        return written

    monkeypatch.setattr(Path, "write_bytes", inject_extra_file)

    with pytest.raises(ComponentUpdateError, match="UPDATE_STAGED_TREE_MISMATCH"):
        apply_component_update(
            installation,
            package,
            expected_manifest_sha256=manifest_sha256,
        )

    assert (active / "old.py").read_text(encoding="utf-8") == "old"
    assert not (active / "new.py").exists()
    assert not (active / "unexpected.py").exists()


def test_component_update_keeps_the_old_pointer_when_pointer_publish_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation, active = _managed_installation(tmp_path)
    package = tmp_path / "local-backend.oliviapatch"
    manifest_sha256 = _write_component_package(
        package,
        version="1.1.0",
        files={"new.py": b"new"},
    )
    original_replace = component_update.os.replace

    state_path = installation / ".olivia-update-state.json"

    def fail_new_pointer(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == state_path:
            raise OSError("synthetic publish failure")
        original_replace(source, destination)

    monkeypatch.setattr(component_update.os, "replace", fail_new_pointer)

    with pytest.raises(ComponentUpdateError, match="UPDATE_ACTIVATION_FAILED"):
        apply_component_update(
            installation,
            package,
            expected_manifest_sha256=manifest_sha256,
        )

    assert (active / "old.py").read_text(encoding="utf-8") == "old"
    assert not (active / "new.py").exists()
    assert not state_path.exists()

    monkeypatch.setattr(component_update.os, "replace", original_replace)
    assert apply_component_update(
        installation,
        package,
        expected_manifest_sha256=manifest_sha256,
    ) == {
        "status": "APPLIED",
        "component": "local_backend",
        "version": "1.1.0",
    }


def test_component_update_rejects_untrusted_manifest_before_activation(
    tmp_path: Path,
) -> None:
    installation, active = _managed_installation(tmp_path)
    package = tmp_path / "local-backend.oliviapatch"
    _write_component_package(
        package,
        version="1.1.0",
        files={"new.py": b"new"},
    )

    with pytest.raises(
        ComponentUpdateError,
        match="UPDATE_MANIFEST_DIGEST_MISMATCH",
    ):
        apply_component_update(
            installation,
            package,
            expected_manifest_sha256="0" * 64,
        )

    assert (active / "old.py").read_text(encoding="utf-8") == "old"
    assert not (active / "new.py").exists()
    assert not (installation / ".olivia-update-state.json").exists()


def test_installer_cli_applies_a_verified_local_component_package(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installation, _active = _managed_installation(tmp_path)
    package = tmp_path / "local-backend.oliviapatch"
    manifest_sha256 = _write_component_package(
        package,
        version="1.1.0",
        files={"new.py": b"new"},
    )

    exit_code = installer_cli.main(
        [
            "apply-update",
            "--installation",
            str(installation),
            "--package",
            str(package),
            "--manifest-sha256",
            manifest_sha256,
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "APPLIED",
        "component": "local_backend",
        "version": "1.1.0",
    }


def test_uninstall_removes_update_state_and_abandoned_update_staging(
    tmp_path: Path,
) -> None:
    installation = tmp_path / "installation"
    installation.mkdir()
    state = installation / ".olivia-update-state.json"
    state.write_text("{}", encoding="utf-8")
    abandoned = installation / "runtime" / "update-staging" / "abandoned"
    abandoned.mkdir(parents=True)
    (abandoned / "partial.bin").write_bytes(b"partial")
    preserved = installation / "data" / "keep.txt"
    preserved.parent.mkdir()
    preserved.write_text("keep", encoding="utf-8")

    remove_owned_targets(installation)

    assert not state.exists()
    assert not (installation / "runtime" / "update-staging").exists()
    assert preserved.read_text(encoding="utf-8") == "keep"


def test_component_update_rejects_reparse_component_target(
    tmp_path: Path,
) -> None:
    installation = tmp_path / "installation"
    installation.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "old.py").write_text("external", encoding="utf-8")
    active = _make_windows_junction(installation / "local_backend", external)
    _write_install_marker(installation)
    package = tmp_path / "local-backend.oliviapatch"
    manifest_sha256 = _write_component_package(
        package,
        version="1.1.0",
        files={"new.py": b"new"},
    )

    with pytest.raises(ComponentUpdateError, match="UPDATE_INSTALLATION_INVALID"):
        apply_component_update(
            installation,
            package,
            expected_manifest_sha256=manifest_sha256,
        )

    assert (external / "old.py").read_text(encoding="utf-8") == "external"
    assert not (external / "new.py").exists()
