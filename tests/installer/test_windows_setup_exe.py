from __future__ import annotations

import hashlib
import json
from pathlib import Path
import wave

import pytest

from installer.build_windows_setup import (
    BUILD_CONTROL_FILES,
    SetupBuildError,
    _git_tracked_files,
    _is_release_file,
    build_windows_setup,
    main as build_setup_main,
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


def _write_voice_reference(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as target:
        target.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        target.writeframes(b"\x00\x00" * 160)


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


def _voice_setup_fixture(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    source, offline = tmp_path / "source", tmp_path / "offline"
    installer = source / "installer"
    installer.mkdir(parents=True)
    (installer / "Install.ps1").write_text("install", encoding="utf-8")
    requirements = b"locked requirements"
    (installer / "runtime-requirements.txt").write_bytes(requirements)
    reference = tmp_path / "distributor" / "olivia-reference.wav"
    _write_voice_reference(reference)
    _offline_fixture(offline, requirements)
    monkeypatch.setattr(
        "installer.build_windows_setup._git_tracked_files",
        lambda _source: {
            "installer/Install.ps1",
            "installer/runtime-requirements.txt",
            *BUILD_CONTROL_FILES,
        },
    )
    monkeypatch.setattr("installer.build_windows_setup._git_dirty_files", lambda _: set())
    return source, offline, reference


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
    (source / "installer" / "assets").mkdir()
    (source / "installer" / "assets" / "olivia.ico").write_bytes(b"icon")
    requirements = b"locked requirements"
    (source / "installer" / "runtime-requirements.txt").write_bytes(requirements)
    (source / "LICENSE").write_text("license", encoding="utf-8")
    (source / "local_server.py").write_text("tracked", encoding="utf-8")
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
            "installer/assets/olivia.ico",
            "installer/runtime-requirements.txt",
            *BUILD_CONTROL_FILES,
            "LICENSE",
            "local_server.py",
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
    assert (destination / "installer" / "assets" / "olivia.ico").read_bytes() == b"icon"
    assert (destination / "LICENSE").is_file()
    assert (destination / "local_server.py").is_file()
    assert (destination / "offline" / "offline-core-assets.json").is_file()
    assert not (destination / "untracked.py").exists()
    assert not (destination / "tests").exists()
    assert not (destination / "docs").exists()
    assert not (destination / "test_root.py").exists()
    assert not (destination / "requirements-ci.txt").exists()
    assert not (destination / "pyproject.toml").exists()
    assert not (destination / "installer" / "build_windows_setup.py").exists()


def test_prepare_setup_payload_injects_hash_locked_voice_reference(tmp_path: Path, monkeypatch) -> None:
    source, offline, reference = _voice_setup_fixture(tmp_path, monkeypatch)
    destination = tmp_path / "payload"

    prepare_setup_payload(
        source,
        offline,
        destination,
        distribution="private",
        voice_reference=reference,
        validate_schema=False,
    )

    installed_reference = destination / "offline" / "voice" / "olivia-reference.wav"
    manifest = json.loads((destination / "offline/offline-core-assets.json").read_text())
    assert installed_reference.read_bytes() == reference.read_bytes()
    assert manifest["distribution"] == "private"
    assert manifest["voice_reference"] == {
        "path": "voice/olivia-reference.wav",
        "size_bytes": reference.stat().st_size,
        "sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
        "wave": {"channels": 1, "sample_width_bytes": 2, "sample_rate_hz": 16000,
                 "frame_count": 160, "compression_type": "NONE"},
    }
    input_manifest_path = offline / "offline-core-assets.json"
    input_manifest = json.loads(input_manifest_path.read_text())
    input_manifest.update(distribution="private", voice_reference=manifest["voice_reference"])
    input_manifest_path.write_text(json.dumps(input_manifest), encoding="utf-8")
    prebundled = offline / "voice" / "olivia-reference.wav"
    prebundled.parent.mkdir()
    prebundled.write_bytes(reference.read_bytes())
    with pytest.raises(SetupBuildError, match="SETUP_INPUT_VOICE_REFERENCE_FORBIDDEN"):
        prepare_setup_payload(source, offline, tmp_path / "prebundled", validate_schema=False)
    del input_manifest["distribution"], input_manifest["voice_reference"]
    input_manifest_path.write_text(json.dumps(input_manifest), encoding="utf-8")
    with pytest.raises(SetupBuildError, match="SETUP_OFFLINE_ASSET_SET_MISMATCH"):
        prepare_setup_payload(source, offline, tmp_path / "orphan", validate_schema=False)
    with pytest.raises(SetupBuildError, match="SETUP_VOICE_REFERENCE_PRIVATE_ONLY"):
        prepare_setup_payload(
            source, offline, tmp_path / "public", voice_reference=reference, validate_schema=False
        )
    with pytest.raises(SetupBuildError, match="SETUP_PRIVATE_VOICE_REFERENCE_REQUIRED"):
        prepare_setup_payload(
            source, offline, tmp_path / "private", distribution="private", validate_schema=False
        )


def test_setup_build_cli_forwards_distributor_voice_reference(tmp_path: Path, monkeypatch) -> None:
    reference = tmp_path / "olivia-reference.wav"
    reference.write_bytes(b"RIFF-voice")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "installer.build_windows_setup.build_windows_setup",
        lambda *args, **kwargs: captured.update(kwargs) or {"status": "OK"},
    )

    result = build_setup_main([
        "--offline", str(tmp_path / "offline"), "--output", str(tmp_path / "output"),
        "--version", "0.1.test", "--distribution", "private", "--voice-reference", str(reference),
    ])

    assert result == 0
    assert captured["distribution"] == "private"
    assert captured["voice_reference"] == reference


def test_failed_private_setup_compile_removes_partial_final_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source, offline, reference = _voice_setup_fixture(tmp_path, monkeypatch)
    output = tmp_path / "dist-private"
    compiler = tmp_path / "ISCC.exe"
    compiler.write_bytes(b"compiler")
    contracts = source / "contracts"
    contracts.mkdir()
    (contracts / "offline_core_assets.schema.json").write_text(
        json.dumps({"type": "object"}),
        encoding="utf-8",
    )

    def fail_compile(_command, *, check, timeout):
        assert check is False
        assert timeout == 900
        (output / "Olivia-Setup-x64.exe").write_bytes(b"partial private setup")
        return type("Result", (), {"returncode": 1})()

    monkeypatch.setattr("installer.build_windows_setup.subprocess.run", fail_compile)

    with pytest.raises(SetupBuildError, match="SETUP_COMPILE_FAILED"):
        build_windows_setup(
            source,
            offline,
            output,
            version="0.1.test",
            iscc=compiler,
            distribution="private",
            voice_reference=reference,
        )

    assert not (output / "Olivia-Setup-x64.exe").exists()
    assert not (output / "Olivia-Setup-x64.exe.sha256").exists()


def test_windows_setup_docs_separate_public_and_private_voice_artifacts() -> None:
    documentation = (ROOT / "docs" / "WINDOWS_FULL_PATCH.md").read_text(
        encoding="utf-8"
    )

    assert "公开安装器不包含参考音频" in documentation
    assert "--distribution private" in documentation
    assert "--voice-reference" in documentation
    assert "dist-private" in documentation
    assert "文件名仍为 `Olivia-Setup-x64.exe`" in documentation
    assert "除私有模式显式传入的 WAV 外" in documentation
    assert "GitHub Actions 只生成公开安装器 artifact" in documentation


def test_prepare_setup_payload_rejects_truncated_voice_reference(tmp_path: Path, monkeypatch) -> None:
    source, offline, reference = _voice_setup_fixture(tmp_path, monkeypatch)
    reference.write_bytes(reference.read_bytes()[:-1])

    with pytest.raises(SetupBuildError, match="SETUP_VOICE_REFERENCE_TRUNCATED"):
        prepare_setup_payload(
            source,
            offline,
            tmp_path / "payload",
            distribution="private",
            voice_reference=reference,
            validate_schema=False,
        )


@pytest.mark.parametrize(
    "relative",
    [
        "linli_character/reference.wav",
        "runtime/reference.flac",
        "runtime/reference.mp3",
        "runtime/reference.mp4",
    ],
)
def test_public_setup_rejects_tracked_audio_and_video_payloads(
    tmp_path: Path,
    monkeypatch,
    relative: str,
) -> None:
    source, offline, _reference = _voice_setup_fixture(tmp_path, monkeypatch)
    media = source.joinpath(*relative.split("/"))
    media.parent.mkdir()
    media.write_bytes(b"private media")
    monkeypatch.setattr(
        "installer.build_windows_setup._git_tracked_files",
        lambda _source: {
            "installer/Install.ps1",
            "installer/runtime-requirements.txt",
            *BUILD_CONTROL_FILES,
            relative,
        },
    )

    with pytest.raises(SetupBuildError, match="SETUP_PUBLIC_MEDIA_FORBIDDEN"):
        prepare_setup_payload(
            source,
            offline,
            tmp_path / "payload",
            validate_schema=False,
        )


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
    (source / "local_server.py").write_text("dirty", encoding="utf-8")
    _offline_fixture(offline, requirements)
    monkeypatch.setattr(
        "installer.build_windows_setup._git_tracked_files",
        lambda _source: {
            "installer/Install.ps1",
            "installer/runtime-requirements.txt",
            *BUILD_CONTROL_FILES,
            "local_server.py",
        },
    )
    monkeypatch.setattr(
        "installer.build_windows_setup._git_dirty_files",
        lambda _source: {"local_server.py"},
    )

    with pytest.raises(SetupBuildError, match="SETUP_SOURCE_DIRTY"):
        prepare_setup_payload(source, offline, tmp_path / "payload", validate_schema=False)


def test_setup_payload_uses_positive_runtime_allowlist() -> None:
    assert _is_release_file("local_server.py")
    assert _is_release_file("original_client_update_api.py")
    assert _is_release_file("control_center/static/index.html")
    assert _is_release_file("installer/full_patch.py")
    assert _is_release_file("installer/component_package.py")
    assert _is_release_file("installer/seed-vc-overlap-frames.patch")
    assert _is_release_file("installer/start_hidden.vbs.txt")
    assert _is_release_file("installer/assets/olivia.ico")
    assert _is_release_file("tools/livetalking_worker.py")

    assert not _is_release_file(".gitignore")
    assert not _is_release_file("baseline_hardening_scan.py")
    assert not _is_release_file("installer/build_offline_core_assets.py")
    assert not _is_release_file("tools/verify_b11_scope.py")
    assert not _is_release_file("tools/live_e2e_acceptance.py")


def test_real_head_setup_payload_excludes_build_audit_test_and_scm_files() -> None:
    selected = {
        relative
        for relative in _git_tracked_files(ROOT)
        if _is_release_file(relative)
    }

    assert {
        "installer/Install.ps1",
        "installer/assets/olivia.ico",
        "installer/full_patch.py",
        "installer/component_package.py",
        "installer/seed-vc-overlap-frames.patch",
        "local_server.py",
        "control_center/static/index.html",
    }.issubset(selected)
    assert not any(
        relative.startswith((".", "docs/", "tests/"))
        or relative.startswith("tools/verify_")
        or "/audit_" in relative
        or "acceptance" in relative
        or relative.startswith("installer/build_")
        for relative in selected
    )


def test_prepare_setup_payload_rejects_dirty_setup_build_control_file(
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
            *BUILD_CONTROL_FILES,
        },
    )
    monkeypatch.setattr(
        "installer.build_windows_setup._git_dirty_files",
        lambda _source: {"installer/windows_setup.iss"},
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
            *BUILD_CONTROL_FILES,
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
    assert "SetupIconFile={#PayloadRoot}\\installer\\assets\\olivia.ico" in script
    assert "Install.ps1" in script
    assert "-NonInteractive" in script
    assert "GetInstallRoot" in script
    assert "Exec(" in script
    assert "ExecAndLogOutput" not in script
    assert "StableInstallCode" in script
    assert "SetupResultPath" in script
    assert "OLIVIA_SETUP_ERROR=" in script
    assert "OFFICIAL_INSTALL_AMBIGUOUS" in script
    assert "上一步" in script
    assert ".diagnostic.json" in script
    assert "Olivia installer diagnostic:" in script
    assert "function PrepareToInstall" in script
    assert "dontcopy noencryption" in script
    assert "OfficialDirPage: TInputQueryWizardPage" in script
    assert "BrowseForFolder" in script
    assert "{param:InstallRoot|" in script
    assert "{localappdata}\\BSideOliviaLocal\\install}" not in script
    assert "产品目录" in script
    assert "{param:OfficialRoot|" in script
    assert "API" not in script
    assert "Hugging Face" not in script


def test_inno_wrapper_creates_launch_shortcuts_and_offers_immediate_start() -> None:
    script = (ROOT / "installer" / "windows_setup.iss").read_text(encoding="utf-8")

    assert '[Tasks]' in script
    assert 'Name: "desktopicon"' in script
    assert '[Icons]' in script
    assert '{userprograms}\\Olivia 本地版' in script
    assert '{userdesktop}\\Olivia 本地版' in script
    assert '[Run]' in script
    assert 'Description: "立即启动 Olivia"' in script
    assert '{sys}\\wscript.exe' in script
    assert '\\install\\START.vbs' in script
    assert '\\install\\START.cmd' not in script
    hidden_launch = (
        'Filename: "{sys}\\wscript.exe"; Parameters: "//B //Nologo '
        '""{code:GetInstallRoot}\\install\\START.vbs"""'
    )
    assert script.count(hidden_launch) == 3
    assert script.count('WorkingDir: "{code:GetInstallRoot}\\install"') == 3
    assert "' -SkipShortcut'" in script
    assert 'IconFilename: "{code:GetInstallRoot}\\install\\local_backend\\installer\\assets\\olivia.ico"' in script


def test_install_ps1_supports_noninteractive_setup_without_optional_downloads() -> None:
    script = (ROOT / "installer" / "Install.ps1").read_text(encoding="utf-8-sig")

    assert "[switch]$NonInteractive" in script
    assert "[string]$SetupResultPath" in script
    assert "OLIVIA_SETUP_ERROR=" in script
    assert "SETUP_INSTALL_FAILED" in script
    assert "if (-not $selectedOfficial -and -not $NonInteractive)" in script
    assert "Invoke-WebRequest" not in script
    assert "provision_mem0_embedding.py" not in script


def test_windows_installer_documents_ambiguous_source_diagnostic_contract() -> None:
    documentation = (ROOT / "docs" / "WINDOWS_FULL_PATCH.md").read_text(
        encoding="utf-8"
    )

    assert "OFFICIAL_INSTALL_AMBIGUOUS" in documentation
    assert "olivia.setup-source-diagnostic.v1" in documentation
    assert "selected_official_id" in documentation
    assert "observed_feapp_sha256" in documentation
    assert "observed_webplayer_sha256" in documentation


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
    assert "windows_setup_smoke.ps1" in workflow
    assert "actions/upload-artifact@v4" in workflow


def test_setup_failure_smoke_copies_the_valid_repository_icon() -> None:
    smoke = (ROOT / "tests" / "installer" / "windows_setup_smoke.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert "installer\\assets\\olivia.ico" in smoke
    assert "Copy-Item -LiteralPath $sourceIcon -Destination $fixtureIcon" in smoke


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


def test_runtime_third_party_notices_cover_every_locked_offline_wheel() -> None:
    requirements = (ROOT / "installer" / "runtime-requirements.txt").read_text(
        encoding="utf-8"
    )
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    packages = {
        line.split("==", maxsplit=1)[0]
        for line in requirements.splitlines()
        if line and not line.startswith("#")
    }

    assert packages
    assert all(f"`{package}`" in notices for package in packages)
    assert "Python 3.12.10 embeddable package" in notices
    assert "`pip` 25.2" in notices


def test_first_release_notes_cover_user_facing_release_boundaries() -> None:
    notes = (ROOT / "docs" / "releases" / "v0.1.0.md").read_text(
        encoding="utf-8"
    )

    for heading in ("已验证能力", "需要用户准备", "已知限制", "升级与回滚"):
        assert f"## {heading}" in notes
    assert "Olivia-Setup-x64.exe" in notes
    assert "未进行 Authenticode" in notes
    assert "未知发布者" in notes
    assert "SHA-256" in notes


def test_readme_release_status_matches_deferred_optional_model_install() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Mem0 一键新装仍是发布阻断" not in readme
    assert "可选模型在登录后的初始设置中按需安装" in readme
    assert "当前状态：发布候选" not in readme
    assert "发布边界" in readme
