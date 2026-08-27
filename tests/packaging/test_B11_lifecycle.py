"""B11 lifecycle composition and external-asset preservation contracts."""

from __future__ import annotations

from pathlib import Path
import hashlib
import os

import pytest

from runtime.packaging.b10b.errors import B10BError
from runtime.packaging.b10b.manager import B10BManager


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "B11 product with spaces"
    project.mkdir()
    (project / "local_server.py").write_text("# B02 contract\n", encoding="utf-8")
    (project / "http_contract.py").write_text("# B02 contract\n", encoding="utf-8")
    return project, tmp_path / "B11 data"


def _settings(tmp_path: Path) -> dict[str, object]:
    root = tmp_path / "external" / "LiveTalking"
    (root / "models").mkdir(parents=True)
    (root / "data" / "avatars" / "b11_olivia").mkdir(parents=True)
    (root / "evidence").mkdir(parents=True)
    source = tmp_path / "downloads" / "wav2lip256.pth"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"verified-managed-copy")
    managed = root / "models" / "wav2lip.pth"
    managed.write_bytes(source.read_bytes())
    return {
        "runtime_root": str(root),
        "python_executable": str(root / "venv" / "Scripts" / "python.exe"),
        "checkpoint_path": str(root / "models" / "wav2lip256.pth"),
        "checkpoint_sha256": "00" * 32,
        "checkpoint_url": "https://github.com/lipku/LiveTalking",
        "checkpoint_revision": "wav2lip256.pth",
        "checkpoint_license": "Wav2Lip upstream terms; see provenance",
        "avatar_payload": str(root / "data" / "avatars" / "b11_olivia"),
        "avatar_id": "b11_olivia",
        "original_reference": str(root / "original-reference.png"),
        "work_root": str(root / "evidence"),
        "upstream_revision": "a97f01ba366e55eeed94e88d6bae38ed77b3a1b9",
        "backend_name": "LiveTalking-Wav2Lip-official",
        "managed_external_copies": [
            {
                "source": str(source),
                "destination": str(managed),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "preserve_source": True,
            }
        ],
    }


def _directory_alias(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory reparse points are unavailable: {exc}")


def test_livetalking_install_enable_health_and_uninstall_are_idempotent_and_external_only(tmp_path: Path) -> None:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)

    installed = manager.install(["core/http", "visual-driver", "visual-livetalking"])
    assert installed["status"] == "INSTALLED"
    assert manager.install(["core/http", "visual-driver", "visual-livetalking"])["status"] == "NO_OP"

    manager.enable("core/http")
    manager.enable("visual-driver")
    manager.enable("visual-livetalking")
    before = manager.health()
    item = next(module for module in before["modules"] if module["id"] == "visual-livetalking")
    assert item["status"] == "UNAVAILABLE"
    assert item["provider_health"]["reason"] == "RUNTIME_NOT_CONFIGURED"

    manager.customize("visual-livetalking", _settings(tmp_path))
    configured = manager.health()
    configured_item = next(module for module in configured["modules"] if module["id"] == "visual-livetalking")
    assert configured_item["status"] == "UNAVAILABLE"
    assert configured_item["provider_health"]["external_assets_copied"] is False

    manager.disable("visual-livetalking")
    dry = manager.uninstall(["visual-livetalking"], dry_run=True)
    assert dry["status"] == "DRY_RUN"
    assert dry["external_assets_deleted"] is False
    assert dry["modules"][0]["managed_external_copies"][0]["status"] == "READY"

    removed = manager.uninstall(["visual-livetalking"], dry_run=False)
    assert removed["status"] == "UNINSTALLED"
    assert removed["external_assets_deleted"] is True
    assert removed["managed_external_copies_deleted"] == 1
    assert not (tmp_path / "external" / "LiveTalking" / "models" / "wav2lip.pth").exists()
    assert (tmp_path / "downloads" / "wav2lip256.pth").exists()
    assert (tmp_path / "external" / "LiveTalking").is_dir()
    assert not (data_root / "modules" / "visual_livetalking" / "marker.json").exists()
    assert not (data_root / "modules" / "visual_livetalking" / "config.json").exists()


@pytest.mark.parametrize("alias_suffix", [".", " "])
def test_livetalking_rejects_managed_copy_destination_alias_of_preserved_source(
    tmp_path: Path, alias_suffix: str
) -> None:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    manager.install(["core/http", "visual-driver", "visual-livetalking"])

    settings = _settings(tmp_path)
    source = tmp_path / "downloads" / "notepad.exe"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"preserved source")
    settings["managed_external_copies"] = [
        {
            "source": str(source),
            "destination": f"{source}{alias_suffix}",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "preserve_source": True,
        }
    ]

    with pytest.raises(B10BError) as exc_info:
        manager.customize("visual-livetalking", settings)

    assert exc_info.value.code == "CONFIG_INVALID"
    assert source.is_file()
    assert not (data_root / "modules" / "visual_livetalking" / "config.json").exists()


def test_livetalking_uninstall_refuses_physical_alias_of_preserved_source(tmp_path: Path) -> None:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    manager.install(["core/http", "visual-driver", "visual-livetalking"])

    settings = _settings(tmp_path)
    source = tmp_path / "downloads" / "preserved.pth"
    destination = tmp_path / "external" / "LiveTalking" / "models" / "preserved.pth"
    source.write_bytes(b"preserved source")
    destination.write_bytes(source.read_bytes())
    settings["managed_external_copies"] = [
        {
            "source": str(source),
            "destination": str(destination),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "preserve_source": True,
        }
    ]
    manager.customize("visual-livetalking", settings)
    manager.disable("visual-livetalking")

    destination.unlink()
    os.link(source, destination)
    assert os.path.samefile(source, destination)

    with pytest.raises(B10BError) as exc_info:
        manager.uninstall(["visual-livetalking"], dry_run=False)

    assert exc_info.value.code == "CONFIG_INVALID"
    assert source.read_bytes() == b"preserved source"
    assert destination.exists()


def test_livetalking_uninstall_refuses_reparse_alias_of_preserved_source(tmp_path: Path) -> None:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    manager.install(["core/http", "visual-driver", "visual-livetalking"])

    settings = _settings(tmp_path)
    source = tmp_path / "downloads" / "preserved.pth"
    destination = tmp_path / "external" / "LiveTalking" / "models" / "preserved.pth"
    source.write_bytes(b"preserved source")
    destination.write_bytes(source.read_bytes())
    settings["managed_external_copies"] = [
        {
            "source": str(source),
            "destination": str(destination),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "preserve_source": True,
        }
    ]
    manager.customize("visual-livetalking", settings)
    manager.disable("visual-livetalking")

    destination.unlink()
    (destination.parent / "wav2lip.pth").unlink()
    destination.parent.rmdir()
    _directory_alias(source.parent, destination.parent)
    assert os.path.samefile(source, destination)

    with pytest.raises(B10BError) as exc_info:
        manager.uninstall(["visual-livetalking"], dry_run=False)

    assert exc_info.value.code == "CONFIG_INVALID"
    assert source.read_bytes() == b"preserved source"
    assert destination.exists()


def test_livetalking_uninstall_refuses_destination_alias_of_any_preserved_source(tmp_path: Path) -> None:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    manager.install(["core/http", "visual-driver", "visual-livetalking"])

    settings = _settings(tmp_path)
    source_a = tmp_path / "downloads" / "preserved-a.pth"
    source_b = tmp_path / "downloads" / "preserved-b.pth"
    destination_a = tmp_path / "external" / "LiveTalking" / "models" / "copy-a.pth"
    destination_b = tmp_path / "external" / "LiveTalking" / "models" / "copy-b.pth"
    source_a.write_bytes(b"same verified source")
    source_b.write_bytes(b"same verified source")
    destination_a.write_bytes(source_a.read_bytes())
    destination_b.write_bytes(source_b.read_bytes())
    digest = hashlib.sha256(source_a.read_bytes()).hexdigest()
    settings["managed_external_copies"] = [
        {
            "source": str(source_a),
            "destination": str(destination_a),
            "sha256": digest,
            "preserve_source": True,
        },
        {
            "source": str(source_b),
            "destination": str(destination_b),
            "sha256": digest,
            "preserve_source": True,
        },
    ]
    manager.customize("visual-livetalking", settings)
    manager.disable("visual-livetalking")

    destination_a.unlink()
    os.link(source_b, destination_a)
    assert os.path.samefile(source_b, destination_a)

    with pytest.raises(B10BError) as exc_info:
        manager.uninstall(["visual-livetalking"], dry_run=False)

    assert exc_info.value.code == "CONFIG_INVALID"
    assert source_b.read_bytes() == b"same verified source"
    assert destination_a.exists()
