from __future__ import annotations

import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from installer import component_update
from installer import __main__ as installer_cli
from installer.component_update import ComponentUpdateError, apply_component_update
from installer.uninstall_safety import remove_owned_targets


def _write_component_package(
    path: Path,
    *,
    version: str,
    files: dict[str, bytes],
) -> str:
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
            for name, content in sorted(files.items())
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
        for name, content in files.items():
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
    assert not (active / "old.py").exists()
    assert (active / "new.py").read_bytes() == b"new"
    assert json.loads(
        (installation / ".olivia-update-state.json").read_text(encoding="utf-8")
    ) == {
        "schema_version": "olivia.update-state.v1",
        "active_components": {
            "local_backend": {
                "version": "1.1.0",
                "manifest_sha256": manifest_sha256,
            }
        },
    }


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


def test_component_update_restores_active_payload_when_publish_fails(
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

    def fail_new_payload(source: str | Path, destination: str | Path) -> None:
        if Path(source).name == "payload" and Path(destination) == active:
            raise OSError("synthetic publish failure")
        original_replace(source, destination)

    monkeypatch.setattr(component_update.os, "replace", fail_new_payload)

    with pytest.raises(ComponentUpdateError, match="UPDATE_ACTIVATION_FAILED"):
        apply_component_update(
            installation,
            package,
            expected_manifest_sha256=manifest_sha256,
        )

    assert (active / "old.py").read_text(encoding="utf-8") == "old"
    assert not (active / "new.py").exists()
    assert not (installation / ".olivia-update-state.json").exists()


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
