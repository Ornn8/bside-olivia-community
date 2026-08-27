from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from installer.build_windows_setup import (
    SetupBuildError,
    prepare_setup_payload,
)


ROOT = Path(__file__).resolve().parents[2]


def _write_asset(root: Path, relative: str, content: bytes) -> dict[str, object]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {
        "path": relative.replace("\\", "/"),
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _offline_fixture(root: Path, requirements: bytes) -> None:
    runtime = _write_asset(root, "python.zip", b"runtime")
    runtime["source_url"] = "https://official.example/python.zip"
    pip = _write_asset(root, "pip.whl", b"pip")
    pip.update({"package": "pip", "version": "1"})
    wheel = _write_asset(root, "wheelhouse/core.whl", b"wheel")
    (root / "offline-core-assets.json").write_text(
        json.dumps(
            {
                "schema_version": "fixture.v1",
                "python_runtime": runtime,
                "pip_bootstrap": pip,
                "requirements_sha256": hashlib.sha256(requirements).hexdigest(),
                "wheels": [wheel],
            }
        ),
        encoding="utf-8",
    )


def test_prepare_setup_payload_copies_only_tracked_release_files_and_offline_assets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    offline = tmp_path / "offline"
    destination = tmp_path / "payload"
    (source / "installer").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "docs").mkdir()
    (source / "installer" / "Install.ps1").write_text("install", encoding="utf-8")
    requirements = b"locked requirements"
    (source / "installer" / "runtime-requirements.txt").write_bytes(requirements)
    (source / "LICENSE").write_text("license", encoding="utf-8")
    (source / "tracked.py").write_text("tracked", encoding="utf-8")
    (source / "untracked.py").write_text("untracked", encoding="utf-8")
    (source / "test_root.py").write_text("hidden", encoding="utf-8")
    (source / "requirements-ci.txt").write_text("hidden", encoding="utf-8")
    (source / "pyproject.toml").write_text("hidden", encoding="utf-8")
    (source / "installer" / "build_windows_setup.py").write_text(
        "hidden", encoding="utf-8"
    )
    (source / "tests" / "test_hidden.py").write_text("hidden", encoding="utf-8")
    (source / "docs" / "internal.md").write_text("hidden", encoding="utf-8")
    _offline_fixture(offline, requirements)
    monkeypatch.setattr(
        "installer.build_windows_setup._git_tracked_files",
        lambda _source: {
            "installer/Install.ps1",
            "installer/runtime-requirements.txt",
            "LICENSE",
            "tracked.py",
            "test_root.py",
            "requirements-ci.txt",
            "pyproject.toml",
            "installer/build_windows_setup.py",
            "tests/test_hidden.py",
            "docs/internal.md",
        },
    )
    monkeypatch.setattr(
        "installer.build_windows_setup._git_dirty_files", lambda _source: set()
    )

    prepare_setup_payload(source, offline, destination, validate_schema=False)

    assert (destination / "installer" / "Install.ps1").read_text() == "install"
    assert (destination / "LICENSE").is_file()
    assert (destination / "tracked.py").is_file()
    assert (destination / "offline" / "offline-core-assets.json").is_file()
    assert not (destination / "untracked.py").exists()
    assert not (destination / "tests").exists()
    assert not (destination / "docs").exists()
    assert not (destination / "test_root.py").exists()
    assert not (destination / "requirements-ci.txt").exists()
    assert not (destination / "pyproject.toml").exists()
    assert not (destination / "installer" / "build_windows_setup.py").exists()


def test_prepare_setup_payload_rejects_dirty_tracked_release_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    offline = tmp_path / "offline"
    (source / "installer").mkdir(parents=True)
    requirements = b"locked requirements"
    (source / "installer" / "runtime-requirements.txt").write_bytes(requirements)
    (source / "installer" / "Install.ps1").write_text("install", encoding="utf-8")
    (source / "tracked.py").write_text("dirty", encoding="utf-8")
    _offline_fixture(offline, requirements)
    monkeypatch.setattr(
        "installer.build_windows_setup._git_tracked_files",
        lambda _source: {
            "installer/Install.ps1",
            "installer/runtime-requirements.txt",
            "tracked.py",
        },
    )
    monkeypatch.setattr(
        "installer.build_windows_setup._git_dirty_files",
        lambda _source: {"tracked.py"},
    )

    with pytest.raises(SetupBuildError, match="SETUP_SOURCE_DIRTY"):
        prepare_setup_payload(source, offline, tmp_path / "payload", validate_schema=False)


def test_prepare_setup_payload_rejects_tampered_offline_assets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    offline = tmp_path / "offline"
    (source / "installer").mkdir(parents=True)
    requirements = b"locked requirements"
    (source / "installer" / "runtime-requirements.txt").write_bytes(requirements)
    (source / "installer" / "Install.ps1").write_text("install", encoding="utf-8")
    _offline_fixture(offline, requirements)
    (offline / "wheelhouse" / "core.whl").write_bytes(b"tampered")
    monkeypatch.setattr(
        "installer.build_windows_setup._git_tracked_files",
        lambda _source: {
            "installer/Install.ps1",
            "installer/runtime-requirements.txt",
        },
    )
    monkeypatch.setattr(
        "installer.build_windows_setup._git_dirty_files", lambda _source: set()
    )

    with pytest.raises(SetupBuildError, match="SETUP_OFFLINE_ASSET_HASH_MISMATCH"):
        prepare_setup_payload(source, offline, tmp_path / "payload", validate_schema=False)


def test_prepare_setup_payload_rejects_reparse_parent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    offline = tmp_path / "offline"
    (source / "installer").mkdir(parents=True)
    requirements = b"locked requirements"
    (source / "installer" / "runtime-requirements.txt").write_bytes(requirements)
    (source / "installer" / "Install.ps1").write_text("install", encoding="utf-8")
    _offline_fixture(offline, requirements)
    monkeypatch.setattr(
        "installer.build_windows_setup._git_tracked_files",
        lambda _source: {
            "installer/Install.ps1",
            "installer/runtime-requirements.txt",
        },
    )
    monkeypatch.setattr(
        "installer.build_windows_setup._git_dirty_files", lambda _source: set()
    )
    monkeypatch.setattr(
        "installer.build_windows_setup._is_reparse_point",
        lambda path: path.name == "wheelhouse",
    )

    with pytest.raises(SetupBuildError, match="SETUP_PATH_REPARSE_POINT"):
        prepare_setup_payload(source, offline, tmp_path / "payload", validate_schema=False)


def test_inno_wrapper_is_current_user_offline_and_delegates_to_install_ps1() -> None:
    script = (ROOT / "installer" / "windows_setup.iss").read_text(encoding="utf-8")

    assert "PrivilegesRequired=lowest" in script
    assert "CreateAppDir=no" in script
    assert "Uninstallable=no" in script
    assert "LicenseFile=" in script
    assert "Install.ps1" in script
    assert "-NonInteractive" in script
    assert "GetInstallRoot" in script
    assert "ExecAndLogOutput" in script
    assert "StableInstallCode" in script
    assert "function PrepareToInstall" in script
    assert "dontcopy noencryption" in script
    assert "OfficialDirPage: TInputQueryWizardPage" in script
    assert "BrowseForFolder" in script
    assert "{param:InstallRoot|" in script
    assert "{param:OfficialRoot|" in script
    assert "API" not in script
    assert "Hugging Face" not in script


def test_install_ps1_supports_noninteractive_setup_without_optional_downloads() -> None:
    script = (ROOT / "installer" / "Install.ps1").read_text(encoding="utf-8-sig")

    assert "[switch]$NonInteractive" in script
    assert "if (-not $selectedOfficial -and -not $NonInteractive)" in script
    assert "Invoke-WebRequest" not in script
    assert "provision_mem0_embedding.py" not in script


def test_github_build_publishes_setup_and_checksum_for_merged_main() -> None:
    workflow = (ROOT / ".github" / "workflows" / "windows-setup.yml").read_text(
        encoding="utf-8"
    )

    assert "branches: [main]" in workflow
    assert "build_offline_core_assets.py" in workflow
    assert "build_windows_setup.py" in workflow
    assert "pip install --require-hashes" in workflow
    assert "installer/setup-build-requirements.txt" in workflow
    assert "choco install innosetup --version=6.7.1" in workflow
    assert "Get-AuthenticodeSignature" in workflow
    assert "Pyrsys B.V." in workflow
    assert "is-6_7_1/Files/Languages/Unofficial/ChineseSimplified.isl" in workflow
    assert "7d544b9bb1d142cfa11f2e5d3cc8abe2e55f8e066c5124e3772675aa236e1278" in workflow
    assert "Olivia-Setup-x64.exe.sha256" in workflow
    assert "actions/upload-artifact@v4" in workflow


def test_setup_build_requirements_are_exact_and_hash_locked() -> None:
    requirements = (
        ROOT / "installer" / "setup-build-requirements.txt"
    ).read_text(encoding="utf-8")
    entries = [line for line in requirements.splitlines() if line and not line.startswith("#")]

    assert any(line.startswith("jsonschema==") for line in entries)
    assert all(" --hash=sha256:" in line for line in entries)


def test_third_party_notices_cover_setup_compiler_and_chinese_messages() -> None:
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

    assert "Inno Setup" in notices
    assert "ChineseSimplified.isl" in notices
    assert "is-6_7_1" in notices
    assert "7d544b9bb1d142cfa11f2e5d3cc8abe2e55f8e066c5124e3772675aa236e1278" in notices
