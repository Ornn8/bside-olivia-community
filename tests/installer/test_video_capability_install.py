from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
import json
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import wave
import zipfile

import pytest
from jsonschema import Draft202012Validator

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import video_capability_install
from video_capability_install import (
    apply_runtime_text_patch,
    _extract_zip_safely,
    _portable_python_runtime,
    load_video_manifest,
    load_video_runtime_environment,
    write_runtime_root_manifest,
    VideoCapabilityError,
)
from video_capability_install import (
    VideoBundle,
    VideoCapabilityInstaller,
    VideoFile,
    VideoFileInstall,
    VideoManifest,
    VideoRuntimeArtifact,
)
import original_client_video_capability_api as video_capability_api
import original_client_server
from original_client_video_capability_api import mount_original_client_video_capability_api
from runtime.media.music_reply import video_reply_dependency_status, video_reply_source_url


def test_repository_bom_freezes_accepted_latentsync_15_256_profile() -> None:
    manifest_path = Path("installer/video-capability-manifest.json")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = load_video_manifest(manifest_path)
    ordinary = manifest.bundles[0]
    provenance = payload["provenance"]["latentsync_model"]

    assert provenance == {
        "repo": "ByteDance/LatentSync-1.5",
        "revision": "32a20d29aead0498e3e885e90dbbe8027da1b61b",
        "unet_sha256": "6440b49a7ccceff56cdc001f5f17605216337f5bbd66fa360139768926e23f51",
        "tiny_sha256": "65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9",
        "buffalo_l_sha256": "80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f",
        "config_path": "configs/unet/stage2_efficient.yaml",
        "config_resolution": 256,
        "config_sha256": "72e263b0adb072935a08d5cc3ce304ce5c471fab9f7e233b7304e5902bba24ce",
        "license": "OpenRAIL++",
    }
    unet = next(item for item in ordinary.files if item.identifier == "latentsync-unet")
    assert unet.size_bytes == 5_072_348_184
    assert unet.sha256 == provenance["unet_sha256"]
    assert unet.sources == {
        "domestic": "https://modelscope.cn/models/chenmingyu/latentsync/resolve/582973d4f016e94f8c08a85ee111814f8c623828/latentsync_unet.pt",
        "official": "https://huggingface.co/ByteDance/LatentSync-1.5/resolve/32a20d29aead0498e3e885e90dbbe8027da1b61b/latentsync_unet.pt",
    }


def test_repository_bom_replaces_cosyvoice_with_fixed_breeze_and_license_boundaries() -> None:
    manifest_path = Path("installer/video-capability-manifest.json")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = load_video_manifest(manifest_path)
    ordinary, music = manifest.bundles
    assert ordinary.label == "说话与口型基础组件"
    assert music.label == "视频回信（说话 + 60 秒音乐）"
    breeze_files = [
        item for item in ordinary.files if item.identifier.startswith("breeze-")
    ]
    baseline_files = [item for item in breeze_files if not item.identifier.startswith("breeze-dependency-")]
    assert len(baseline_files) == 18
    assert sum(item.size_bytes for item in baseline_files) == 5_787_613_526
    assert ordinary.dependencies == ("breeze_tts2", "latentsync", "ffmpeg")
    assert not any(item.identifier.startswith("cosy-") for item in ordinary.files)
    assert all(
        item.license == "BreezeBlue-Research-and-Non-Commercial-1.0"
        for item in baseline_files
        if item.identifier
        not in {
            "breeze-python-runtime",
            "breeze-runtime-comfy-kitchen-wheel",
            "breeze-runtime-whisper-wheel",
            "breeze-runtime-code",
            "breeze-quality-gate-whisper-base",
        }
    )
    assert next(
        item for item in breeze_files if item.identifier == "breeze-int8-hybrid"
    ).sha256 == "e9f80ab9976caa3b5905e3d87e71d053aad8fae6b0e75caecbca9efdcf1cb2c8"
    quality_gate = next(
        item
        for item in ordinary.files
        if item.identifier == "breeze-quality-gate-whisper-base"
    )
    assert quality_gate.size_bytes == 145_262_807
    assert quality_gate.sha256 == (
        "ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e"
    )
    assert payload["provenance"]["breeze_model"] == {
        "repo": "drbaph/Breeze-TTS-2-comfyui",
        "revision": "a6bfd9d0d4e8b3b61c3a5ed3ecf55468c8ab88e4",
        "official_base_repo": "BreezeBlue/Breeze-TTS-2",
        "official_base_revision": "c1c8ca18b70b30822735633991d9ebf4898e47d4",
        "variant": "int8_hybrid",
        "size_bytes": 5580779297,
        "license": "BreezeBlue-Research-and-Non-Commercial-1.0",
    }
    assert music.license_review_required is False
    assert "official_video_assets" not in ordinary.dependencies
    assert music.dependencies == ("ordinary_video", "minimax_music3", "roformer")
    ffmpeg = next(item for item in ordinary.files if item.identifier == "ffmpeg")
    assert ffmpeg.size_bytes == 111_253_802
    assert ffmpeg.sha256 == (
        "fec81ae03971d9dd4be3ebe02e263bd2ec1d789483f931bdba5f5715e65da2e9"
    )
    assert ffmpeg.sources == {
        "official": "https://github.com/GyanD/codexffmpeg/releases/download/9.0.1/ffmpeg-9.0.1-essentials_build.zip"
    }
    assert video_reply_source_url("ffmpeg", "official") == ffmpeg.sources["official"]
    assert payload["provenance"]["ffmpeg"]["source"] == (
        "https://github.com/GyanD/codexffmpeg/releases/tag/9.0.1"
    )
    assert ordinary.runtime_environment == {
        "OLIVIA_BREEZE_TTS_ROOT": "breeze/runtime",
        "OLIVIA_BREEZE_TTS_PYTHON": "breeze/python/python.exe",
        "OLIVIA_BREEZE_TTS_MODEL_ROOT": "breeze/model",
        "OLIVIA_BREEZE_TTS_MODEL_LICENSE": "breeze/model/LICENSE",
        "OLIVIA_TTS_QUALITY_GATE_CACHE_ROOT": "quality/whisper",
        "OLIVIA_FFMPEG_EXE": "ffmpeg/runtime/bin/ffmpeg.exe",
        "OLIVIA_LATENTSYNC_PYTHON": "latentsync/runtime/python/python.exe",
        "OLIVIA_LATENTSYNC_ROOT": "latentsync/runtime",
        "OLIVIA_OFFICIAL_REPLY_REFERENCE": "scenes/official-reply-reference-000-043s-v1.mp4",
        "OLIVIA_ORDINARY_ACTION_BASE": "scenes/official-reply-action-base-v1.mp4",
        "OLIVIA_MUSIC_PERFORMANCE_BASE": "scenes/official-performance-lipsync-safe-2950f-v1.mp4",
    }
    assert ordinary.runtime_artifacts == (
        VideoRuntimeArtifact(
            "latentsync-runtime",
            ("latentsync-runtime-part-01", "latentsync-runtime-part-02"),
            3_345_491_860,
            "0410df7ad4e383214c532bb6f9f1e3dc779b79087a5b40b1500fe61b85ea3dcb",
            "latentsync",
            1,
        ),
    )
    assert {
        patch.identifier: (patch.target_path, patch.sha256)
        for patch in ordinary.runtime_patches
    } == {
        "latentsync-windows-memmap": (
            "latentsync/runtime/latentsync/pipelines/lipsync_pipeline.py",
            "a627cc639bd400c00466f683517afaf7adbac8b42088f128bd42e04d52b8e5b1",
        ),
        "latentsync-windows-mp4-writer": (
            "latentsync/runtime/latentsync/utils/util.py",
            "bbeea2d143e756c0ba419b5299cd4b62731e619b155d3e308aa66920868ca516",
        ),
    }
    music_file_ids = {item.identifier for item in music.files}
    assert "seed-vc-code" not in music_file_ids
    assert "demucs-htdemucs6s" not in music_file_ids
    assert {"roformer-code", "roformer-checkpoint"} <= music_file_ids
    assert music.runtime_environment == {
        "OLIVIA_MINIMAX_COMFY_PYTHON": "minimax/runtime/python/python.exe",
        "OLIVIA_MINIMAX_COMFY_ROOT": "minimax/runtime",
        "OLIVIA_MINIMAX_WORKER": "minimax/runtime/tools/minimax_music3_worker.py",
        "OLIVIA_ROFORMER_PYTHON": "roformer/runtime/python/python.exe",
        "OLIVIA_ROFORMER_MODEL_PATH": "roformer/models/MelBandRoformer.ckpt",
        "OLIVIA_ROFORMER_CONFIG_PATH": "roformer/runtime/src/mel_band_roformer/configs/config_vocals_mel_band_roformer.yaml",
    }
    assert music.runtime_artifacts == (
        VideoRuntimeArtifact(
            "minimax-runtime",
            ("minimax-runtime-part-01", "minimax-runtime-part-02"),
            2_714_245_618,
            "59cc7dd47086ac0ec5a27adeebd9098c050a3fb0a21fb3aba50978a0ba562a04",
            "minimax",
            1,
        ),
        VideoRuntimeArtifact(
            "roformer-runtime",
            ("roformer-runtime-part-01", "roformer-runtime-part-02"),
            2_889_933_971,
            "205562052ec4232ad37f162c2b832c4130abe506065b7f2f77f8739bf8650adb",
            "roformer",
            1,
        ),
    )
    provenance = payload["provenance"]
    latentsync_unet = next(item for item in ordinary.files if item.identifier == "latentsync-unet")
    assert latentsync_unet.size_bytes == 5072348184
    assert latentsync_unet.sha256 == provenance["latentsync_model"]["unet_sha256"]
    assert latentsync_unet.license == "OpenRAIL++"
    latentsync_tiny = next(item for item in ordinary.files if item.identifier == "latentsync-tiny")
    assert latentsync_tiny.relative_path == "latentsync/runtime/checkpoints/whisper/tiny.pt"
    assert latentsync_tiny.license == "OpenRAIL++"
    assert provenance["roformer"]["license"] == "MIT + CC-BY-NC-SA-4.0 checkpoint"
    assert provenance["roformer"]["config_sha256"] == "5e380dfa5d5757ac4c2b7f6ef607b93d5058ecff805e7b05ed730a47b90d103c"
    minimax_files = [
        item
        for item in music.files
        if item.identifier.startswith("minimax-")
        and not item.identifier.startswith("minimax-runtime-part-")
    ]
    assert minimax_files
    assert all(
        "/resolve/fbc3502b5d2ca0049348ee28b632f270b35e193a/"
        in item.sources["domestic"]
        for item in minimax_files
    )

    schema = json.loads(Path("contracts/video_capability_manifest.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(payload)


@pytest.mark.parametrize(
    ("hardware", "reason"),
    (
        (
            {
                "status": "UNAVAILABLE",
                "vendor": "unknown",
                "minimum_vram_mib": 10240,
                "detected_vram_mib": None,
                "reason_code": "BREEZE_TTS_NVIDIA_GPU_REQUIRED",
            },
            "BREEZE_TTS_NVIDIA_GPU_REQUIRED",
        ),
        (
            {
                "status": "UNAVAILABLE",
                "vendor": "NVIDIA",
                "minimum_vram_mib": 10240,
                "detected_vram_mib": 8192,
                "reason_code": "BREEZE_TTS_10GB_VRAM_REQUIRED",
            },
            "BREEZE_TTS_10GB_VRAM_REQUIRED",
        ),
    ),
)
def test_breeze_hardware_gate_blocks_before_any_download_and_is_retryable(
    tmp_path: Path,
    hardware: dict[str, object],
    reason: str,
) -> None:
    opened: list[str] = []
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=load_video_manifest(Path("installer/video-capability-manifest.json")),
        opener=lambda *_args, **_kwargs: opened.append("network") or pytest.fail(
            "hardware rejection must happen before download"
        ),
        hardware_probe=lambda: hardware,
    )

    status = installer.status()
    assert status["hardware"] == hardware
    assert status["bundles"][0]["state"] == "prerequisites_required"
    assert status["bundles"][0]["reason_code"] == reason
    with pytest.raises(VideoCapabilityError, match=reason):
        installer.start(bundle_id="ordinary_video")
    assert opened == []


def test_breeze_hardware_gate_retries_after_gpu_becomes_eligible(tmp_path: Path) -> None:
    hardware = {
        "status": "UNAVAILABLE",
        "vendor": "NVIDIA",
        "minimum_vram_mib": 10240,
        "detected_vram_mib": 8192,
        "reason_code": "BREEZE_TTS_10GB_VRAM_REQUIRED",
    }
    manifest = VideoManifest(
        version="fixture",
        bundles=(
            VideoBundle(
                "ordinary_video",
                "ordinary",
                "FIXED",
                True,
                ("breeze_tts2",),
                (),
            ),
        ),
    )
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=manifest,
        hardware_probe=lambda: hardware,
    )

    with pytest.raises(VideoCapabilityError, match="BREEZE_TTS_10GB_VRAM_REQUIRED"):
        installer.start(bundle_id="ordinary_video")
    hardware.update(
        status="READY",
        detected_vram_mib=12288,
        reason_code=None,
    )

    assert installer.retry(bundle_id="ordinary_video") == "APPLIED"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if installer.status()["bundles"][0]["state"] == "ready":
            break
        time.sleep(0.01)
    assert installer.status()["bundles"][0]["state"] == "ready"


def test_video_uninstall_removes_only_the_managed_capability_tree(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    manifest = VideoManifest(
        version="fixture",
        bundles=(
            VideoBundle("ordinary_video", "ordinary", "FIXED", False, (), ()),
            VideoBundle("music_video", "music", "FIXED", False, (), ()),
        ),
    )
    installer = VideoCapabilityInstaller(data_root=data_root, manifest=manifest)
    for relative in (
        "ordinary_video/model.bin",
        "music_video/model.bin",
        "runtime/python.exe",
        "shared/reference.wav",
        "generated/tts.json",
        ".downloads/partial.bin",
        ".staging/partial.bin",
    ):
        target = installer.install_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"managed")
    (installer.install_root / "runtime-environment.json").write_text(
        "{}", encoding="utf-8"
    )
    media = data_root / "media" / "reply.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"keep")

    assert installer.status()["can_uninstall"] is True
    assert installer.uninstall() == "APPLIED"

    assert installer.install_root.is_dir()
    assert [item.name for item in installer.install_root.iterdir()] == ["shared"]
    assert (installer.install_root / "shared/reference.wav").read_bytes() == b"managed"
    assert media.read_bytes() == b"keep"
    status = installer.status()
    assert status["can_uninstall"] is False
    assert {item["state"] for item in status["bundles"]} == {"missing"}
    assert installer.uninstall() == "NOOP"


def test_breeze_hardware_status_rechecks_gpu_zero_instead_of_using_cached_result(
    tmp_path: Path,
) -> None:
    samples = iter(
        (
            {
                "status": "READY",
                "vendor": "NVIDIA",
                "minimum_vram_mib": 10240,
                "detected_vram_mib": 10240,
                "reason_code": None,
            },
            {
                "status": "UNAVAILABLE",
                "vendor": "NVIDIA",
                "minimum_vram_mib": 10240,
                "detected_vram_mib": 8192,
                "reason_code": "BREEZE_TTS_10GB_VRAM_REQUIRED",
            },
        )
    )
    manifest = VideoManifest(
        version="fixture",
        bundles=(
            VideoBundle(
                "ordinary_video",
                "ordinary",
                "FIXED",
                True,
                ("breeze_tts2",),
                (),
            ),
        ),
    )
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=manifest,
        hardware_probe=lambda: next(samples),
    )

    status = installer.status()

    assert status["hardware"]["detected_vram_mib"] == 8192
    assert status["bundles"][0]["reason_code"] == "BREEZE_TTS_10GB_VRAM_REQUIRED"


def test_video_manifest_contains_every_hash_locked_breeze_wheel() -> None:
    import re
    from packaging.utils import canonicalize_name, parse_wheel_filename
    manifest = load_video_manifest(Path("installer/video-capability-manifest.json"))
    wheels = {}
    for item in manifest.bundles[0].files:
        if item.relative_path.startswith("breeze/wheels/"):
            name, version, _, _ = parse_wheel_filename(Path(item.relative_path).name)
            wheels[name] = (str(version), item.sha256)
    requirements = Path("installer/breeze-runtime-requirements.txt").read_text()
    blocks = requirements.replace("\\\n", " ").splitlines()
    locked = {}
    for line in blocks:
        match = re.match(r"([\w-]+)==(\S+)", line)
        if match:
            name, version = match.groups()
            locked[canonicalize_name(name)] = version
            assert canonicalize_name(name) in wheels, name
            actual_version, sha = wheels[canonicalize_name(name)]
            assert actual_version == version
            assert f"--hash=sha256:{sha}" in line, name
    assert set(locked) == set(wheels)


def test_empty_capability_root_bootstraps_a_verified_breeze_runtime(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    archive = artifacts / "runtime" / "python-fixture.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as payload:
        payload.writestr("python.exe", b"synthetic-python")
        payload.writestr("python312.zip", b"synthetic-stdlib")
        payload.writestr("python312._pth", "python312.zip\n.\n")
    runtime_file = artifacts / "breeze" / "runtime" / "__init__.py"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text("# synthetic Breeze runtime\n", encoding="utf-8")
    files = (
        VideoFile(
            "breeze-python-runtime",
            "runtime/python-fixture.zip",
            archive.stat().st_size,
            hashlib.sha256(archive.read_bytes()).hexdigest(),
            "PSF-2.0",
            {"official": "https://example.invalid/python.zip"},
            install=VideoFileInstall("zip", "breeze/python", 0),
        ),
        VideoFile(
            "breeze-runtime-code",
            "breeze/runtime/__init__.py",
            runtime_file.stat().st_size,
            hashlib.sha256(runtime_file.read_bytes()).hexdigest(),
            "Apache-2.0",
            {"official": "https://example.invalid/runtime.py"},
        ),
    )
    runner_calls: list[tuple[Path, Path, Path]] = []

    def install_packages(python: Path, site_packages: Path, requirements: Path) -> None:
        status = installer.status()["bundles"][0]
        assert status["state"] == "verifying"
        assert status["current_file"] == "安装本地运行依赖（无需联网）"
        runner_calls.append((python, site_packages, requirements))
        (site_packages / "torch").mkdir()
        (site_packages / "torch" / "__init__.py").write_text(
            "__version__ = '2.9.1+cu128'\n", encoding="utf-8"
        )

    data_root = (tmp_path / "empty-data").resolve()
    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=VideoManifest(
            "fixture",
            (
                VideoBundle(
                    "ordinary_video",
                    "ordinary",
                    "FIXED",
                    True,
                    ("breeze_tts2",),
                    files,
                    runtime_environment={
                        "OLIVIA_BREEZE_TTS_PYTHON": "breeze/python/python.exe",
                        "OLIVIA_BREEZE_TTS_ROOT": "breeze/runtime",
                    },
                ),
            ),
        ),
        artifact_roots=(artifacts.resolve(),),
        readiness_probe=lambda _environment: {"ordinary_missing_dependencies": []},
        hardware_probe=lambda: {
            "status": "READY",
            "vendor": "NVIDIA",
            "minimum_vram_mib": 10240,
            "detected_vram_mib": 10240,
            "reason_code": None,
        },
        runtime_package_runner=install_packages,
        runtime_package_verifier=lambda python, runtime: (
            python.is_file()
            and (runtime / "__init__.py").is_file()
            and (python.parent / "Lib" / "site-packages" / "torch" / "__init__.py").is_file()
        ),
    )

    assert installer.start(bundle_id="ordinary_video", source_mode="official") == "APPLIED"
    state = _wait(installer, 0, "ready", "failed")
    assert state == "ready", installer.status()

    installed = installer.install_root / "ordinary_video"
    assert len(runner_calls) == 1
    assert runner_calls[0][0].name == "python.exe"
    assert runner_calls[0][1].as_posix().endswith("breeze/python/Lib/site-packages")
    assert "Lib/site-packages" in (
        installed / "breeze" / "python" / "python312._pth"
    ).read_text(encoding="utf-8")
    marker = json.loads(
        (installed / ".olivia-breeze-runtime.json").read_text(encoding="utf-8")
    )
    assert marker["requirements_sha256"] == (
        "1d1f2aafb3eeda0d882335354fb89ca572dbc0cb5438766020db818780c08ddc"
    )
    assert load_video_runtime_environment(data_root)["OLIVIA_BREEZE_TTS_PYTHON"] == str(
        installed / "breeze" / "python" / "python.exe"
    )


def test_breeze_runtime_bootstrap_is_hash_locked_and_wheel_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / "breeze" / "python" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fixture")
    site_packages = python.parent / "Lib" / "site-packages"
    requirements = Path("installer/breeze-runtime-requirements.txt").resolve()
    observed: list[list[str]] = []

    def run(command, **_kwargs):
        observed.append(list(command))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video_capability_install.subprocess, "run", run)

    VideoCapabilityInstaller._install_breeze_runtime_packages(
        python, site_packages, requirements
    )

    command = observed[0]
    assert "--require-hashes" in command
    assert "--no-deps" in command
    assert "--only-binary=:all:" in command
    assert "--find-links" in command
    assert str(tmp_path / "breeze" / "wheels") in command
    assert "--no-index" in command
    assert "--extra-index-url" not in command
    assert "--no-binary" not in command
    assert "--no-build-isolation" not in command


def test_runtime_artifact_parts_are_verified_reassembled_and_removed_after_install(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    archive = tmp_path / "latentsync.zip"
    with zipfile.ZipFile(archive, "w") as payload:
        payload.writestr("python/python.exe", b"portable-latentsync-python")
    archive_bytes = archive.read_bytes()
    split = len(archive_bytes) // 2
    parts = (archive_bytes[:split], archive_bytes[split:])
    files: list[VideoFile] = []
    for index, content in enumerate(parts, start=1):
        relative = f"runtime/latentsync.zip.part{index:02d}"
        target = artifacts / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        files.append(
            VideoFile(
                f"latentsync-runtime-part-{index:02d}",
                relative,
                len(content),
                hashlib.sha256(content).hexdigest(),
                "runtime dependency licenses",
                {"official": f"https://example.invalid/{target.name}"},
            )
        )
    runtime_artifact = VideoRuntimeArtifact(
        "latentsync-runtime",
        tuple(item.identifier for item in files),
        len(archive_bytes),
        hashlib.sha256(archive_bytes).hexdigest(),
        "latentsync/runtime",
        0,
    )
    manifest = VideoManifest(
        "fixture",
        (
            VideoBundle(
                "ordinary_video",
                "ordinary",
                "FIXED",
                False,
                ("latentsync",),
                tuple(files),
                runtime_environment={
                    "OLIVIA_LATENTSYNC_PYTHON": "latentsync/runtime/python/python.exe"
                },
                runtime_artifacts=(runtime_artifact,),
            ),
        ),
    )
    data_root = (tmp_path / "data").resolve()
    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        artifact_roots=(artifacts.resolve(),),
    )

    assert installer.start(bundle_id="ordinary_video") == "APPLIED"
    assert _wait(installer, 0, "ready", "failed") == "ready"

    installed = installer.install_root / "ordinary_video"
    assert (installed / "latentsync/runtime/python/python.exe").read_bytes() == (
        b"portable-latentsync-python"
    )
    assert not any((installed / item.relative_path).exists() for item in files)
    assert not any(
        (installer._download_root(manifest.bundles[0]) / item.relative_path).exists()
        for item in files
    )
    assert (
        installed / ".runtime-artifacts" / "latentsync-runtime.json"
    ).is_file()
    restarted = VideoCapabilityInstaller(data_root=data_root, manifest=manifest)
    assert restarted.status()["bundles"][0]["state"] == "ready"


def test_runtime_text_patch_is_hash_checked_and_fails_on_source_drift(
    tmp_path: Path,
) -> None:
    target = tmp_path / "runtime.py"
    target.write_text("before\n", encoding="utf-8")
    patch = tmp_path / "runtime.patch.json"
    patch.write_text(
        json.dumps(
            {
                "schema_version": "olivia.runtime-text-patch.v1",
                "target": "runtime.py",
                "replacements": [{"before": "before", "after": "after"}],
            }
        ),
        encoding="utf-8",
    )
    sha256 = hashlib.sha256(patch.read_bytes()).hexdigest()

    apply_runtime_text_patch(
        bundle_root=tmp_path,
        patch_path=patch,
        target_path="runtime.py",
        expected_sha256=sha256,
        patch_id="fixture",
    )

    assert target.read_text(encoding="utf-8") == "after\n"
    assert (tmp_path / ".patches" / "fixture.json").is_file()
    target.write_text("drifted\n", encoding="utf-8")
    with pytest.raises(VideoCapabilityError, match="VIDEO_RUNTIME_PATCH_SOURCE_MISMATCH"):
        apply_runtime_text_patch(
            bundle_root=tmp_path,
            patch_path=patch,
            target_path="runtime.py",
            expected_sha256=sha256,
            patch_id="fixture",
        )


def test_latentsync_windows_mp4_writer_patch_uses_opencv(tmp_path: Path) -> None:
    patch = Path("installer/latentsync-windows-mp4-writer.patch.json")
    payload = json.loads(patch.read_text(encoding="utf-8"))
    target = tmp_path / payload["target"]
    target.parent.mkdir(parents=True)
    target.write_text(payload["replacements"][0]["before"] + "\n", encoding="utf-8")

    apply_runtime_text_patch(
        bundle_root=tmp_path,
        patch_path=patch,
        target_path=payload["target"],
        expected_sha256=hashlib.sha256(patch.read_bytes()).hexdigest(),
        patch_id="latentsync-windows-mp4-writer",
    )

    patched = target.read_text(encoding="utf-8")
    assert "write_video_cv2(video_output_path, video_frames, fps)" in patched
    assert "imageio.get_writer" not in patched


@pytest.mark.parametrize("unsafe", ["CON/file.bin", "asset:stream", "name./file.bin"])
def test_manifest_rejects_windows_unsafe_paths(tmp_path: Path, unsafe: str) -> None:
    payload = json.loads(Path("installer/video-capability-manifest.json").read_text(encoding="utf-8"))
    payload["bundles"][0]["files"][0]["path"] = unsafe
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(VideoCapabilityError, match="VIDEO_MANIFEST_PATH_INVALID"):
        load_video_manifest(path)


def _wait(installer: VideoCapabilityInstaller, index: int, *states: str) -> str:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = installer.status()["bundles"][index]["state"]
        if state in states:
            return state
        time.sleep(0.01)
    return installer.status()["bundles"][index]["state"]


def _runtime_archive(tmp_path: Path) -> Path:
    runtime_root = tmp_path / "runtime-source"
    environment = {
        "OLIVIA_BREEZE_TTS_PYTHON": "breeze/python/python.exe",
        "OLIVIA_LATENTSYNC_PYTHON": "latentsync/python/python.exe",
        "OLIVIA_MINIMAX_COMFY_PYTHON": "minimax/python/python.exe",
        "OLIVIA_ROFORMER_PYTHON": "roformer/python/python.exe",
    }
    for relative in environment.values():
        target = runtime_root / relative
        if Path(relative).suffix:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fixture")
        else:
            target.mkdir(parents=True, exist_ok=True)
            (target / "fixture.txt").write_bytes(b"fixture")
    write_runtime_root_manifest(
        runtime_root.resolve(), version="fixture-runtime", environment=environment
    )
    archive = tmp_path / "Olivia-video-runtime-fixture.zip"
    with zipfile.ZipFile(archive, "w") as payload:
        for path in runtime_root.rglob("*"):
            if path.is_file():
                payload.write(path, path.relative_to(runtime_root).as_posix())
    return archive.resolve()


def _runtime_ready_manifest() -> VideoManifest:
    return VideoManifest(
        "1.0", (
            VideoBundle("ordinary_video", "video", "FIXED", False, (), (), runtime_environment={
                "OLIVIA_TTS_CONFIG": "config/tts.json", "OLIVIA_LATENTSYNC_ROOT": "latentsync/runtime"}),
            VideoBundle("music_video", "music", "FIXED", False, (), (), runtime_environment={
                "OLIVIA_MINIMAX_COMFY_ROOT": "minimax/runtime",
                "OLIVIA_MINIMAX_WORKER": "minimax/runtime/tools/minimax_music3_worker.py"}),
        ),
    )


def _prepare_runtime_dependencies(data_root: Path, manifest: VideoManifest) -> None:
    install_root = _mark_bundle_payloads_ready(data_root, manifest)
    for relative in ("ordinary_video/cosyvoice/runtime", "ordinary_video/cosyvoice/model",
                     "ordinary_video/latentsync/runtime", "music_video/minimax/runtime"):
        (install_root / relative).mkdir(parents=True, exist_ok=True)
    config = install_root / "ordinary_video/config/tts.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('{"settings":{}}', encoding="utf-8")
    reference = install_root / "shared/linli-reference.wav"
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_bytes(b"managed-voice")


def _mark_bundle_payloads_ready(data_root: Path, manifest: VideoManifest) -> Path:
    install_root = data_root / "capabilities" / "video"
    for bundle in manifest.bundles:
        root = install_root / bundle.identifier
        root.mkdir(parents=True, exist_ok=True)
        (root / ".ready.json").write_text(
            json.dumps({"schema_version": "olivia.video-bundle.v1", "bundle": bundle.identifier, "version": manifest.version}),
            encoding="utf-8",
        )
    return install_root


def test_runtime_archive_is_extracted_verified_and_activated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _runtime_archive(tmp_path)
    monkeypatch.setattr(video_capability_install, "_runtime_environment_is_portable", lambda *_: True)
    manifest = _runtime_ready_manifest()
    data_root = (tmp_path / "data").resolve()
    _prepare_runtime_dependencies(data_root, manifest)
    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=lambda _environment: {
            "ordinary_missing_dependencies": [],
            "music_ready": True,
        },
    )

    assert installer.import_runtime_archive(runtime_archive=archive) == "APPLIED"

    status = installer.status()
    assert status["runtime_import"]["state"] == "ready"
    profile = json.loads(
        (installer.install_root / "runtime-environment.json").read_text(encoding="utf-8")
    )
    assert Path(profile["runtime_root"]) == (installer.install_root / "runtime").resolve()
    assert Path(profile["environment"]["OLIVIA_TTS_CONFIG"]).is_file()
    observed: list[dict[str, str]] = []
    monkeypatch.setattr(
        VideoCapabilityInstaller,
        "import_runtime_archive",
        lambda *_args, **_kwargs: pytest.fail("installed runtime must not reimport"),
    )
    restarted = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=lambda _environment: pytest.fail("installed runtime must not reprobe"),
        runtime_archives=(archive,),
        runtime_environment_applier=lambda environment: observed.append(dict(environment)),
    )
    assert restarted.status()["runtime_import"]["state"] == "ready"
    assert len(observed) == 1
    archive.unlink()
    restarted_without_archive = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=lambda _environment: pytest.fail("installed runtime must not reprobe"),
    )
    assert restarted_without_archive.status()["runtime_import"]["state"] == "ready"


def test_runtime_archive_import_retries_a_transient_archive_open_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _runtime_archive(tmp_path)
    manifest = _runtime_ready_manifest()
    data_root = (tmp_path / "data").resolve()
    _prepare_runtime_dependencies(data_root, manifest)
    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=lambda _environment: {
            "ordinary_missing_dependencies": [],
            "music_ready": True,
        },
    )
    real_zip_file = zipfile.ZipFile
    archive_opens = 0
    observed_delays: list[float] = []

    def transient_zip_file(*args: object, **kwargs: object) -> zipfile.ZipFile:
        nonlocal archive_opens
        archive_opens += 1
        if archive_opens == 1:
            raise PermissionError("archive is temporarily held by another process")
        return real_zip_file(*args, **kwargs)

    monkeypatch.setattr(video_capability_install.zipfile, "ZipFile", transient_zip_file)
    monkeypatch.setattr(
        video_capability_install,
        "time",
        SimpleNamespace(sleep=observed_delays.append),
        raising=False,
    )
    monkeypatch.setattr(
        video_capability_install,
        "_runtime_environment_is_portable",
        lambda *_: True,
    )

    assert installer.import_runtime_archive(runtime_archive=archive) == "APPLIED"
    assert archive_opens >= 2
    assert observed_delays == [0.1]


def test_runtime_archive_import_does_not_retry_bad_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "corrupt.zip"
    archive.write_bytes(b"not a zip")
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=_runtime_ready_manifest(),
    )
    archive_opens = 0

    def corrupt_zip_file(*_args: object, **_kwargs: object) -> zipfile.ZipFile:
        nonlocal archive_opens
        archive_opens += 1
        raise zipfile.BadZipFile

    monkeypatch.setattr(video_capability_install.zipfile, "ZipFile", corrupt_zip_file)
    monkeypatch.setattr(
        video_capability_install,
        "time",
        SimpleNamespace(
            sleep=lambda _delay: pytest.fail("corrupt archives must not be retried")
        ),
        raising=False,
    )

    with pytest.raises(VideoCapabilityError, match="^VIDEO_RUNTIME_ARCHIVE_INVALID$"):
        installer.import_runtime_archive(runtime_archive=archive)
    assert archive_opens == 1


def test_runtime_archive_import_bounds_transient_open_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "locked.zip"
    archive.write_bytes(b"present")
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=_runtime_ready_manifest(),
    )
    archive_opens = 0
    observed_delays: list[float] = []

    def locked_zip_file(*_args: object, **_kwargs: object) -> zipfile.ZipFile:
        nonlocal archive_opens
        archive_opens += 1
        raise PermissionError("archive remains locked")

    monkeypatch.setattr(video_capability_install.zipfile, "ZipFile", locked_zip_file)
    monkeypatch.setattr(
        video_capability_install,
        "time",
        SimpleNamespace(sleep=observed_delays.append),
        raising=False,
    )

    with pytest.raises(VideoCapabilityError, match="^VIDEO_RUNTIME_ARCHIVE_INVALID$"):
        installer.import_runtime_archive(runtime_archive=archive)
    assert archive_opens == 8
    assert observed_delays == [0.1, 0.2, 0.4, 0.8, 1.6, 2.0, 2.0]


def test_runtime_archive_import_publishes_extract_verify_and_test_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _runtime_archive(tmp_path)
    manifest = _runtime_ready_manifest()
    data_root = (tmp_path / "data").resolve()
    _prepare_runtime_dependencies(data_root, manifest)
    observed: list[tuple[str, int, int]] = []
    monkeypatch.setattr(
        video_capability_install,
        "_runtime_environment_is_portable",
        lambda *_: True,
    )
    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=lambda _environment: {
            "ordinary_missing_dependencies": [],
            "music_ready": True,
        },
        runtime_progress=lambda state, current, total: observed.append(
            (state, current, total)
        ),
    )

    assert installer.import_runtime_archive(runtime_archive=archive) == "APPLIED"
    assert any(state == "extracting" and total > 0 for state, _, total in observed)
    assert any(
        state == "checking" and total > 0 and current == total
        for state, current, total in observed
    )
    assert any(state == "testing" for state, _, _ in observed)
    assert observed[-1][0] == "ready"


def test_runtime_archive_resumes_interrupted_extraction_from_bound_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _runtime_archive(tmp_path)
    monkeypatch.setattr(video_capability_install, "_runtime_environment_is_portable", lambda *_: True)
    manifest = _runtime_ready_manifest()
    data_root = (tmp_path / "data").resolve()
    _prepare_runtime_dependencies(data_root, manifest)
    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=lambda _environment: {"ordinary_missing_dependencies": [], "music_ready": True},
    )
    real_extract = video_capability_install._extract_runtime_zip_safely
    resume_values: list[bool] = []

    def interrupted_extract(
        archive_path: Path,
        destination: Path,
        *,
        resume: bool = False,
        next_member_index: int = 0,
        checkpoint_progress: object = None,
        progress: object = None,
    ) -> None:
        resume_values.append(resume)
        destination.mkdir(parents=True)
        with zipfile.ZipFile(archive_path) as payload:
            member = next(item for item in payload.infolist() if not item.is_dir())
            target = destination / member.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload.read(member))
        raise RuntimeError("simulated extraction termination")

    monkeypatch.setattr(video_capability_install, "_extract_runtime_zip_safely", interrupted_extract)
    with pytest.raises(RuntimeError, match="simulated extraction termination"):
        installer.import_runtime_archive(runtime_archive=archive)

    cache = data_root / "capabilities" / ".video-runtime-import-cache"
    checkpoint = cache / ".runtime-import-checkpoint.json"
    assert checkpoint.is_file()
    checkpoint_payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    candidate = cache / checkpoint_payload["candidate"]
    assert candidate.is_dir()
    first_member = next(item for item in zipfile.ZipFile(archive).infolist() if not item.is_dir())
    preserved = candidate / first_member.filename
    assert preserved.is_file()
    def resumed_extract(
        archive_path: Path,
        destination: Path,
        *,
        resume: bool = False,
        next_member_index: int = 0,
        checkpoint_progress: object = None,
        progress: object = None,
    ) -> None:
        resume_values.append(resume)
        real_extract(archive_path, destination, resume=resume, progress=progress)

    monkeypatch.setattr(video_capability_install, "_extract_runtime_zip_safely", resumed_extract)
    assert installer.import_runtime_archive(runtime_archive=archive) == "APPLIED"
    assert resume_values == [False, True]
    assert not checkpoint.exists()


def test_runtime_host_unavailable_keeps_verified_artifact_and_degrades_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _runtime_archive(tmp_path)
    monkeypatch.setattr(video_capability_install, "_runtime_environment_is_portable", lambda *_: True)
    manifest = _runtime_ready_manifest()
    data_root = (tmp_path / "data").resolve()
    _prepare_runtime_dependencies(data_root, manifest)
    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=lambda _environment: {
            "ordinary_missing_dependencies": ["loader"],
            "music_ready": False,
            "dependencies": [
                {"id": "breeze_tts2", "state": "missing"},
                {"id": "minimax_music3", "state": "missing"},
            ],
        },
    )

    assert installer.import_runtime_archive(runtime_archive=archive) == "APPLIED"

    status = installer.status()
    assert status["status"] == "UNAVAILABLE"
    assert status["runtime_import"]["state"] == "ready"
    assert status["runtime_import"]["reason_code"] == "VIDEO_RUNTIME_HOST_UNAVAILABLE"
    assert [item["state"] for item in status["bundles"]] == [
        "prerequisites_required",
        "prerequisites_required",
    ]
    assert all(
        item["reason_code"] == "VIDEO_RUNTIME_HOST_UNAVAILABLE"
        for item in status["bundles"]
    )
    profile = json.loads(
        (installer.install_root / "runtime-environment.json").read_text(encoding="utf-8")
    )
    assert profile["host_status"] == {
        "status": "UNAVAILABLE",
        "reason_code": "VIDEO_RUNTIME_HOST_UNAVAILABLE",
    }


def test_runtime_archive_retries_verified_candidate_without_reextract_or_rehash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _runtime_archive(tmp_path)
    monkeypatch.setattr(video_capability_install, "_runtime_environment_is_portable", lambda *_: True)
    manifest = _runtime_ready_manifest()
    data_root = (tmp_path / "data").resolve()
    _prepare_runtime_dependencies(data_root, manifest)
    attempts = 0
    verify_values: list[bool] = []
    real_load = video_capability_install._load_runtime_root_manifest

    def counted_load(*args: object, **kwargs: object) -> dict[str, str]:
        verify_values.append(kwargs["verify_files"])
        return real_load(*args, **kwargs)

    monkeypatch.setattr(video_capability_install, "_load_runtime_root_manifest", counted_load)

    def readiness(_environment: dict[str, str]) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {
                "ordinary_missing_dependencies": ["ffmpeg"],
                "music_ready": False,
                "dependencies": [{"id": "ffmpeg", "state": "missing"}],
            }
        return {"ordinary_missing_dependencies": [], "music_ready": True}

    installer = VideoCapabilityInstaller(
        data_root=data_root, manifest=manifest, readiness_probe=readiness
    )
    with pytest.raises(VideoCapabilityError, match="VIDEO_RUNTIME_PROBE_FAILED"):
        installer.import_runtime_archive(runtime_archive=archive)
    resume_values: list[bool] = []
    real_extract = video_capability_install._extract_runtime_zip_safely

    def counted_extract(*args: object, **kwargs: object) -> None:
        resume_values.append(bool(kwargs.get("resume", False)))
        real_extract(*args, **kwargs)

    monkeypatch.setattr(video_capability_install, "_extract_runtime_zip_safely", counted_extract)
    assert installer.import_runtime_archive(runtime_archive=archive) == "APPLIED"
    assert verify_values == [True, True, True, True]
    assert resume_values == [True]


def test_runtime_archive_ignores_checkpoint_for_another_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _runtime_archive(tmp_path)
    monkeypatch.setattr(video_capability_install, "_runtime_environment_is_portable", lambda *_: True)
    manifest = _runtime_ready_manifest()
    data_root = (tmp_path / "data").resolve()
    _prepare_runtime_dependencies(data_root, manifest)
    attempts = 0

    def readiness(_environment: dict[str, str]) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {
                "ordinary_missing_dependencies": ["ffmpeg"],
                "music_ready": False,
                "dependencies": [{"id": "ffmpeg", "state": "missing"}],
            }
        return {"ordinary_missing_dependencies": [], "music_ready": True}

    installer = VideoCapabilityInstaller(
        data_root=data_root, manifest=manifest, readiness_probe=readiness
    )
    with pytest.raises(VideoCapabilityError, match="VIDEO_RUNTIME_PROBE_FAILED"):
        installer.import_runtime_archive(runtime_archive=archive)
    checkpoint = data_root / "capabilities" / ".video-runtime-import-cache" / ".runtime-import-checkpoint.json"
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    old_candidate = checkpoint.parent / payload["candidate"]
    payload["archive_identity"] = "foreign-runtime-fingerprint"
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    resume_values: list[bool] = []
    real_extract = video_capability_install._extract_runtime_zip_safely

    def counted_extract(*args: object, **kwargs: object) -> None:
        resume_values.append(bool(kwargs.get("resume", False)))
        real_extract(*args, **kwargs)

    monkeypatch.setattr(video_capability_install, "_extract_runtime_zip_safely", counted_extract)
    assert installer.import_runtime_archive(runtime_archive=archive) == "APPLIED"
    assert resume_values == [False]
    assert not old_candidate.exists()


def test_runtime_archive_copy_with_new_inode_and_mtime_resumes_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _runtime_archive(tmp_path)
    copied = (tmp_path / "copied-runtime.zip").resolve()
    shutil.copyfile(archive, copied)
    os.utime(copied, ns=(1_000_000_000, 1_000_000_000))
    monkeypatch.setattr(video_capability_install, "_runtime_environment_is_portable", lambda *_: True)
    manifest = _runtime_ready_manifest()
    data_root = (tmp_path / "data").resolve()
    _prepare_runtime_dependencies(data_root, manifest)
    attempts = 0

    def readiness(_environment: dict[str, str]) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {
                "ordinary_missing_dependencies": ["ffmpeg"],
                "music_ready": False,
                "dependencies": [{"id": "ffmpeg", "state": "missing"}],
            }
        return {"ordinary_missing_dependencies": [], "music_ready": True}

    installer = VideoCapabilityInstaller(data_root=data_root, manifest=manifest, readiness_probe=readiness)
    with pytest.raises(VideoCapabilityError, match="VIDEO_RUNTIME_PROBE_FAILED"):
        installer.import_runtime_archive(runtime_archive=archive)
    resume_values: list[bool] = []
    real_extract = video_capability_install._extract_runtime_zip_safely

    def counted_extract(*args: object, **kwargs: object) -> None:
        resume_values.append(bool(kwargs.get("resume", False)))
        real_extract(*args, **kwargs)

    monkeypatch.setattr(video_capability_install, "_extract_runtime_zip_safely", counted_extract)
    assert installer.import_runtime_archive(runtime_archive=copied) == "APPLIED"
    assert resume_values == [True]


def test_runtime_archive_system_exit_keeps_cache_after_video_root_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _runtime_archive(tmp_path)
    monkeypatch.setattr(video_capability_install, "_runtime_environment_is_portable", lambda *_: True)
    manifest = _runtime_ready_manifest()
    data_root = (tmp_path / "data").resolve()
    _prepare_runtime_dependencies(data_root, manifest)
    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=lambda _environment: (_ for _ in ()).throw(SystemExit(7)),
    )
    with pytest.raises(SystemExit):
        installer.import_runtime_archive(runtime_archive=archive)
    cache = data_root / "capabilities" / ".video-runtime-import-cache"
    checkpoint = cache / ".runtime-import-checkpoint.json"
    assert checkpoint.is_file()
    shutil.rmtree(installer.install_root)
    _prepare_runtime_dependencies(data_root, manifest)
    resume_values: list[bool] = []
    real_extract = video_capability_install._extract_runtime_zip_safely

    def counted_extract(*args: object, **kwargs: object) -> None:
        resume_values.append(bool(kwargs.get("resume", False)))
        real_extract(*args, **kwargs)

    monkeypatch.setattr(video_capability_install, "_extract_runtime_zip_safely", counted_extract)
    restarted = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=lambda _environment: {
            "ordinary_missing_dependencies": [], "music_ready": True
        },
    )
    assert restarted.import_runtime_archive(runtime_archive=archive) == "APPLIED"
    assert resume_values == [True]


def test_runtime_archive_same_size_candidate_tamper_fails_full_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _runtime_archive(tmp_path)
    monkeypatch.setattr(video_capability_install, "_runtime_environment_is_portable", lambda *_: True)
    manifest = _runtime_ready_manifest()
    data_root = (tmp_path / "data").resolve()
    _prepare_runtime_dependencies(data_root, manifest)
    attempts = 0

    def readiness(_environment: dict[str, str]) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {
                "ordinary_missing_dependencies": ["ffmpeg"],
                "music_ready": False,
                "dependencies": [{"id": "ffmpeg", "state": "missing"}],
            }
        return {"ordinary_missing_dependencies": [], "music_ready": True}

    installer = VideoCapabilityInstaller(data_root=data_root, manifest=manifest, readiness_probe=readiness)
    with pytest.raises(VideoCapabilityError, match="VIDEO_RUNTIME_PROBE_FAILED"):
        installer.import_runtime_archive(runtime_archive=archive)
    cache = data_root / "capabilities" / ".video-runtime-import-cache"
    payload = json.loads((cache / ".runtime-import-checkpoint.json").read_text(encoding="utf-8"))
    candidate = cache / payload["candidate"]
    tampered = next(path for path in candidate.rglob("*") if path.is_file() and path.name != "runtime-manifest.json")
    tampered.write_bytes(b"tamper!")
    with pytest.raises(VideoCapabilityError, match="VIDEO_RUNTIME_ROOT_INVALID"):
        installer.import_runtime_archive(runtime_archive=archive)
    assert not (cache / ".runtime-import-checkpoint.json").exists()


def test_runtime_hard_failure_restores_previous_runtime_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _runtime_archive(tmp_path)
    monkeypatch.setattr(video_capability_install, "_runtime_environment_is_portable", lambda *_: True)
    manifest = _runtime_ready_manifest()
    data_root = (tmp_path / "data").resolve()
    _prepare_runtime_dependencies(data_root, manifest)
    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=lambda _environment: {
            "ordinary_missing_dependencies": [],
            "music_ready": True,
        },
    )
    previous = installer.install_root / "runtime"
    previous.mkdir()
    (previous / "old.txt").write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(
        installer,
        "_install_managed_minimax_worker",
        lambda: (_ for _ in ()).throw(
            VideoCapabilityError("VIDEO_RUNTIME_WORKER_UNAVAILABLE")
        ),
    )

    with pytest.raises(VideoCapabilityError, match="VIDEO_RUNTIME_WORKER_UNAVAILABLE"):
        installer.import_runtime_archive(runtime_archive=archive)

    assert (previous / "old.txt").read_text(encoding="utf-8") == "preserve"
    assert not list(installer.install_root.glob(".runtime.backup"))


def test_successful_runtime_upgrade_removes_directory_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _runtime_archive(tmp_path)
    monkeypatch.setattr(video_capability_install, "_runtime_environment_is_portable", lambda *_: True)
    manifest = _runtime_ready_manifest()
    data_root = (tmp_path / "data").resolve()
    _prepare_runtime_dependencies(data_root, manifest)
    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=lambda _environment: {
            "ordinary_missing_dependencies": [],
            "music_ready": True,
        },
    )
    previous = installer.install_root / "runtime"
    previous.mkdir()
    (previous / "old.txt").write_text("replace", encoding="utf-8")

    assert installer.import_runtime_archive(runtime_archive=archive) == "APPLIED"
    assert not (installer.install_root / ".runtime.backup").exists()
    assert not (installer.install_root / "runtime" / "old.txt").exists()
    assert (installer.install_root / "runtime" / "runtime-manifest.json").is_file()


def test_structurally_nonportable_runtime_hard_fails_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _runtime_archive(tmp_path)
    monkeypatch.setattr(video_capability_install, "_runtime_environment_is_portable", lambda *_: False)
    manifest = _runtime_ready_manifest()
    data_root = (tmp_path / "data").resolve()
    _prepare_runtime_dependencies(data_root, manifest)
    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=lambda _environment: {
            "ordinary_missing_dependencies": [],
            "music_ready": True,
        },
    )

    with pytest.raises(VideoCapabilityError, match="VIDEO_RUNTIME_NOT_PORTABLE"):
        installer.import_runtime_archive(runtime_archive=archive)
    assert not (installer.install_root / "runtime").exists()


@pytest.mark.parametrize("failure", ["timeout", "loader"])
def test_runtime_host_start_failure_soft_degrades_without_reprobe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    archive = _runtime_archive(tmp_path)
    manifest = _runtime_ready_manifest()
    data_root = (tmp_path / "data").resolve()
    _prepare_runtime_dependencies(data_root, manifest)

    def failed_process(*_args: object, **_kwargs: object) -> object:
        if failure == "timeout":
            raise subprocess.TimeoutExpired("portable-python", 20)
        return subprocess.CompletedProcess([], 0xC0000135)

    monkeypatch.setattr(video_capability_install.subprocess, "run", failed_process)
    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=lambda _environment: pytest.fail("host failure must not reprobe"),
    )

    assert installer.import_runtime_archive(runtime_archive=archive) == "APPLIED"
    status = installer.status()
    assert status["status"] == "UNAVAILABLE"
    assert status["runtime_import"]["state"] == "ready"
    assert status["runtime_import"]["reason_code"] == "VIDEO_RUNTIME_HOST_UNAVAILABLE"
    assert all(item["reason_code"] == "VIDEO_RUNTIME_HOST_UNAVAILABLE" for item in status["bundles"])
    schema = json.loads(Path("contracts/video_capability_status.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(status)


def test_static_dependency_probe_failure_remains_hard_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _runtime_archive(tmp_path)
    monkeypatch.setattr(video_capability_install, "_runtime_environment_is_portable", lambda *_: True)
    manifest = _runtime_ready_manifest()
    data_root = (tmp_path / "data").resolve()
    _prepare_runtime_dependencies(data_root, manifest)
    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=lambda _environment: {
            "ordinary_missing_dependencies": ["ffmpeg"],
            "music_ready": False,
            "dependencies": [{"id": "ffmpeg", "state": "missing"}],
        },
    )

    with pytest.raises(VideoCapabilityError, match="VIDEO_RUNTIME_PROBE_FAILED"):
        installer.import_runtime_archive(runtime_archive=archive)
    assert not (installer.install_root / "runtime").exists()


def test_restart_repairs_legacy_managed_worker_pair_before_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _runtime_archive(tmp_path)
    monkeypatch.setattr(
        video_capability_install, "_runtime_environment_is_portable", lambda *_: True
    )
    manifest = _runtime_ready_manifest()
    data_root = (tmp_path / "data").resolve()
    _prepare_runtime_dependencies(data_root, manifest)
    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=lambda _environment: {
            "ordinary_missing_dependencies": [],
            "music_ready": True,
        },
    )
    assert installer.import_runtime_archive(runtime_archive=archive) == "APPLIED"
    worker = Path(load_video_runtime_environment(data_root)["OLIVIA_MINIMAX_WORKER"])
    profile = worker.with_name("minimax_profile.py")
    profile.unlink()
    observed: list[dict[str, str]] = []
    music_root = installer.install_root / "music_video"
    original_walk = os.walk

    def bounded_walk(top, *args, **kwargs):
        assert Path(top) != music_root, "startup must not scan the complete music runtime"
        return original_walk(top, *args, **kwargs)

    monkeypatch.setattr(video_capability_install.os, "walk", bounded_walk)

    restarted = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=lambda _environment: pytest.fail(
            "installed runtime must not reprobe"
        ),
        runtime_environment_applier=lambda environment: observed.append(
            dict(environment)
        ),
    )

    source_directory = Path(video_capability_install.__file__).resolve().parent / "tools"
    assert restarted.status()["runtime_import"]["state"] == "ready"
    assert profile.read_bytes() == (source_directory / profile.name).read_bytes()
    assert worker.read_bytes() == (source_directory / worker.name).read_bytes()
    assert len(observed) == 1


def test_restart_fails_closed_when_legacy_managed_worker_pair_cannot_be_repaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _runtime_archive(tmp_path)
    monkeypatch.setattr(
        video_capability_install, "_runtime_environment_is_portable", lambda *_: True
    )
    manifest = _runtime_ready_manifest()
    data_root = (tmp_path / "data").resolve()
    _prepare_runtime_dependencies(data_root, manifest)
    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=lambda _environment: {
            "ordinary_missing_dependencies": [],
            "music_ready": True,
        },
    )
    assert installer.import_runtime_archive(runtime_archive=archive) == "APPLIED"
    worker = Path(load_video_runtime_environment(data_root)["OLIVIA_MINIMAX_WORKER"])
    profile = worker.with_name("minimax_profile.py")
    profile.unlink()
    real_copy = video_capability_install.shutil.copy2

    def copy(source: object, target: object) -> object:
        if Path(source).name == "minimax_profile.py":
            raise PermissionError("profile unavailable")
        return real_copy(source, target)

    monkeypatch.setattr(video_capability_install.shutil, "copy2", copy)
    observed: list[dict[str, str]] = []

    restarted = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=lambda _environment: pytest.fail(
            "failed legacy repair must not reprobe"
        ),
        runtime_environment_applier=lambda environment: observed.append(
            dict(environment)
        ),
    )

    assert restarted.status()["runtime_import"] == {
        "state": "failed",
        "checked_bytes": 0,
        "total_bytes": 0,
        "reason_code": "VIDEO_RUNTIME_WORKER_UNAVAILABLE",
    }
    assert observed == []
    assert not profile.exists()
    assert worker.is_file()


def test_runtime_archive_rejects_managed_component_and_voice_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = (tmp_path / "runtime").resolve()
    environment = {
        "OLIVIA_COSYVOICE_PYTHON": str(runtime_root / "cosyvoice/python/python.exe"),
        "OLIVIA_LATENTSYNC_PYTHON": str(runtime_root / "latentsync/python/python.exe"),
        "OLIVIA_MINIMAX_COMFY_PYTHON": str(runtime_root / "minimax/python/python.exe"),
        "OLIVIA_ROFORMER_PYTHON": str(runtime_root / "roformer/python/python.exe"),
        "OLIVIA_REPLY_VOICE_REFERENCE": str(runtime_root / "voice.wav"),
    }
    monkeypatch.setattr(video_capability_install, "_portable_python_runtime", lambda *_: True)
    assert not video_capability_install._runtime_environment_is_portable(
        environment, runtime_root
    )


@pytest.mark.parametrize(("failure", "error"), (("partial_apply", "VIDEO_RUNTIME_ENVIRONMENT_ACTIVATION_FAILED"), ("profile_publish", "VIDEO_RUNTIME_ENVIRONMENT_WRITE_FAILED")))
def test_runtime_activation_failure_restores_process_environment_and_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, error: str) -> None:
    manifest = _runtime_ready_manifest()
    data_root = (tmp_path / failure / "data").resolve()
    _prepare_runtime_dependencies(data_root, manifest)
    previous_environment = {"OLIVIA_COSYVOICE_PYTHON": "old-python", "KEEP": "yes"}
    monkeypatch.setattr(video_capability_install.os, "environ", previous_environment)
    def apply(environment: Mapping[str, str]) -> None:
        previous_environment.update(environment)
        if failure == "partial_apply":
            raise RuntimeError("partial application")
    installer = VideoCapabilityInstaller(
        data_root=data_root, manifest=manifest, runtime_environment_applier=apply,
        readiness_probe=lambda _environment: {"ordinary_missing_dependencies": [], "music_ready": True})
    profile = installer.install_root / "runtime-environment.json"
    previous_profile = b'{"schema_version":"olivia.video-runtime-environment.v1","environment":{}}'
    profile.write_bytes(previous_profile)
    real_replace = video_capability_install.os.replace
    def replace(source: object, target: object) -> None:
        if failure == "profile_publish" and Path(target) == profile and Path(source).suffix == ".tmp":
            raise OSError("profile publication failed")
        real_replace(source, target)
    monkeypatch.setattr(video_capability_install.os, "replace", replace)
    monkeypatch.setattr(video_capability_install, "_runtime_environment_is_portable", lambda *_: True)
    with pytest.raises(VideoCapabilityError, match=error):
        installer.import_runtime_archive(runtime_archive=_runtime_archive(tmp_path / failure))
    assert previous_environment == {"OLIVIA_COSYVOICE_PYTHON": "old-python", "KEEP": "yes"}
    assert profile.read_bytes() == previous_profile
    assert installer.status()["status"] == "UNAVAILABLE"


def test_production_manifest_persists_managed_worker_and_finishes_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = load_video_manifest(Path("installer/video-capability-manifest.json"))
    data_root = (tmp_path / "data").resolve()
    install_root = _mark_bundle_payloads_ready(data_root, manifest)
    for relative in (
        "ordinary_video/breeze/runtime/nodes.py",
        "ordinary_video/breeze/model/LICENSE", "ordinary_video/ffmpeg/runtime/bin/ffmpeg.exe",
        "ordinary_video/scenes/official-reply-action-base-v1.mp4",
        "ordinary_video/scenes/official-performance-lipsync-safe-2950f-v1.mp4",
        "ordinary_video/scenes/official-reply-reference-000-043s-v1.mp4",
        "music_video/roformer/models/MelBandRoformer.ckpt",
        "music_video/roformer/runtime/src/mel_band_roformer/configs/config_vocals_mel_band_roformer.yaml",
        "shared/linli-reference.wav"):
        target = install_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fixture")
    for relative in ("ordinary_video/breeze/model/drbaph_Breeze-TTS-2-comfyui", "ordinary_video/quality/whisper", "ordinary_video/latentsync/runtime",
                     "music_video/minimax/runtime"):
        (install_root / relative).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        video_capability_install,
        "resolve_managed_voice_reference",
        lambda _data_root: (install_root / "shared/linli-reference.wav").resolve(),
    )
    monkeypatch.setenv("OLIVIA_TTS_REFERENCE_TEXT", "合成测试参考音频的精确转写。")
    monkeypatch.setattr(video_capability_install, "_ready_marker_matches", lambda *_: True)
    monkeypatch.setattr(video_capability_install, "_size_matches", lambda *_: True)
    monkeypatch.setattr(
        VideoCapabilityInstaller, "_runtime_artifacts_ready", lambda *_: True
    )
    monkeypatch.setattr(video_capability_install, "_runtime_environment_is_portable", lambda *_: True)

    def readiness(environment: Mapping[str, str]) -> dict[str, object]:
        settings = json.loads(Path(environment["OLIVIA_TTS_CONFIG"]).read_text(encoding="utf-8"))
        values = settings["settings"]
        ready = (
            values["provider"] == "breeze_tts2"
            and values["reference_text"] == "合成测试参考音频的精确转写。"
            and values["license_id"] == "BreezeBlue-Research-and-Non-Commercial-1.0"
            and Path(values["runtime_root"])
            == (install_root / "ordinary_video/breeze/runtime").resolve()
            and Path(values["model_dir"])
            == (install_root / "ordinary_video/breeze/model").resolve()
            and values["provider_options"]["model_variant"] == "int8_hybrid"
            and values["provider_options"]["device"] == "cuda"
            and values["provider_options"]["attention"] == "eager"
            and values["provider_options"]["decode_mode"] == "eager"
        )
        return {"ordinary_missing_dependencies": [] if ready else ["breeze_tts2"], "music_ready": ready}

    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=readiness,
        hardware_probe=lambda: {
            "status": "READY",
            "vendor": "NVIDIA",
            "minimum_vram_mib": 10240,
            "detected_vram_mib": 10240,
            "reason_code": None,
        },
    )
    assert installer.import_runtime_archive(runtime_archive=_runtime_archive(tmp_path)) == "APPLIED"
    worker = Path(load_video_runtime_environment(data_root)["OLIVIA_MINIMAX_WORKER"])
    assert worker.is_file() and worker.is_relative_to(install_root)
    profile = worker.with_name("minimax_profile.py")
    assert profile.is_file() and profile.is_relative_to(install_root)
    isolated = subprocess.run(
        [sys.executable, "-I", "-B", str(worker), "--help"],
        cwd=tmp_path,
        env={},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert isolated.returncode == 0, isolated.stderr
    assert installer.status()["status"] == "READY", installer.status()


def test_split_production_bundles_reach_real_readiness_without_legacy_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = load_video_manifest(Path("installer/video-capability-manifest.json"))
    data_root = (tmp_path / "empty-data").resolve()
    assert not data_root.exists()
    provider_cache = data_root / "provider-cache"

    def readiness(environment: Mapping[str, str]) -> Mapping[str, object]:
        values = dict(environment)
        values["OLIVIA_PROVIDER_CACHE_ROOT"] = str(provider_cache)
        return video_reply_dependency_status(
            values,
            performance_video_path=Path(values["OLIVIA_MUSIC_PERFORMANCE_BASE"]),
            probe_runtime=False,
        )

    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=readiness,
        hardware_probe=lambda: {
            "status": "READY",
            "vendor": "NVIDIA",
            "minimum_vram_mib": 10240,
            "detected_vram_mib": 10240,
            "reason_code": None,
        },
    )
    install_root = installer.install_root
    ordinary = install_root / ".staging/ordinary-fixture"
    music = install_root / ".staging/music-fixture"
    for root, bundle_id in (
        (ordinary, "ordinary_video"),
        (music, "music_video"),
    ):
        root.mkdir(parents=True, exist_ok=True)
        (root / ".ready.json").write_text(
            json.dumps(
                {
                    "schema_version": "olivia.video-bundle.v1",
                    "bundle": bundle_id,
                    "version": manifest.version,
                }
            ),
            encoding="utf-8",
        )
    directories = (
        ordinary / "breeze/runtime",
        ordinary / "breeze/model/drbaph_Breeze-TTS-2-comfyui/audio_tokenizer",
        ordinary / "quality/whisper",
        ordinary / "latentsync/runtime/configs/unet",
        ordinary / "latentsync/runtime/checkpoints",
        ordinary / "latentsync/runtime/scripts",
        music / "minimax/runtime/comfy_extras",
        music / "minimax/runtime/models/diffusion_models",
        music / "minimax/runtime/models/text_encoders",
        music / "minimax/runtime/models/vae",
        music / "roformer/runtime/src/mel_band_roformer/configs",
        music / "roformer/models",
    )
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    files = (
        ordinary / "breeze/python/python.exe",
        ordinary / "breeze/runtime/__init__.py",
        ordinary / "breeze/runtime/loader.py",
        ordinary / "breeze/runtime/nodes.py",
        ordinary / "breeze/runtime/int8.py",
        ordinary / "breeze/runtime/LICENSE",
        ordinary / "breeze/model/drbaph_Breeze-TTS-2-comfyui/config.json",
        ordinary / "breeze/model/drbaph_Breeze-TTS-2-comfyui/tokenizer.json",
        ordinary / "breeze/model/drbaph_Breeze-TTS-2-comfyui/Breeze-TTS-2-int8-hybrid.safetensors",
        ordinary / "breeze/model/drbaph_Breeze-TTS-2-comfyui/audio_tokenizer/config.json",
        ordinary / "breeze/model/drbaph_Breeze-TTS-2-comfyui/audio_tokenizer/model.safetensors",
        ordinary / "ffmpeg/runtime/bin/ffmpeg.exe",
        ordinary / "latentsync/runtime/python/python.exe",
        ordinary / "latentsync/runtime/scripts/inference.py",
        ordinary / "latentsync/runtime/configs/unet/stage2_efficient.yaml",
        ordinary / "latentsync/runtime/checkpoints/latentsync_unet.pt",
        ordinary / "scenes/official-reply-action-base-v1.mp4",
        ordinary / "scenes/official-performance-lipsync-safe-2950f-v1.mp4",
        ordinary / "scenes/official-reply-reference-000-043s-v1.mp4",
        music / "minimax/runtime/python/python.exe",
        music / "minimax/runtime/main.py",
        music / "minimax/runtime/comfy_extras/nodes_minimax_music.py",
        music / "minimax/runtime/models/diffusion_models/minimax_music3_dit_int8_convrot.safetensors",
        music / "minimax/runtime/models/text_encoders/minimax_music3_text_encoder_pruned_int8_convrot.safetensors",
        music / "minimax/runtime/models/vae/minimax_music3_dav.safetensors",
        music / "roformer/runtime/python/python.exe",
        music / "roformer/runtime/src/mel_band_roformer/configs/config_vocals_mel_band_roformer.yaml",
        music / "roformer/models/MelBandRoformer.ckpt",
    )
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    (ordinary / "breeze/model/LICENSE").write_text(
        "BREEZEBLUE RESEARCH AND NON-COMMERCIAL LICENSE AGREEMENT\nVersion 1.0\n",
        encoding="utf-8",
    )
    reference = install_root / "shared/linli-reference.wav"
    reference.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(reference), "wb") as target:
        target.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        target.writeframes(b"\0\0" * 1_600)
    transcript = reference.with_suffix(".txt")
    transcript.write_text("synthetic exact transcript\n", encoding="utf-8")
    reference.with_suffix(".json").write_text(
        json.dumps(
            {
                "schema_version": "olivia.managed-voice-reference.v2",
                "path": reference.name,
                "size_bytes": reference.stat().st_size,
                "sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
                "transcript": {
                    "path": transcript.name,
                    "size_bytes": transcript.stat().st_size,
                    "sha256": hashlib.sha256(transcript.read_bytes()).hexdigest(),
                },
                "wave": {
                    "channels": 1,
                    "sample_width_bytes": 2,
                    "sample_rate_hz": 16_000,
                    "frame_count": 1_600,
                    "compression_type": "NONE",
                },
            }
        ),
        encoding="utf-8",
    )
    reference_sha256 = hashlib.sha256(reference.read_bytes()).hexdigest()
    real_voice_resolver = video_capability_install.resolve_managed_voice_reference
    monkeypatch.setattr(
        video_capability_install,
        "resolve_managed_voice_reference",
        lambda root: real_voice_resolver(root, expected_sha256=reference_sha256),
    )
    monkeypatch.setattr(video_capability_install, "_ready_marker_matches", lambda *_: True)
    monkeypatch.setattr(video_capability_install, "_size_matches", lambda *_: True)
    monkeypatch.setattr(VideoCapabilityInstaller, "_runtime_artifacts_ready", lambda *_: True)
    monkeypatch.setattr(VideoCapabilityInstaller, "_breeze_runtime_marker_ready", lambda *_: True)

    installer._promote_directory(
        ordinary,
        install_root / "ordinary_video",
        refresh_environment=True,
    )
    installer._promote_directory(
        music,
        install_root / "music_video",
        refresh_environment=True,
    )
    status = installer.status()

    assert status["status"] == "READY", status
    assert status["runtime_import"]["state"] == "ready"
    assert not list(tmp_path.rglob("Olivia-video-runtime-*.zip"))
    environment = load_video_runtime_environment(data_root)
    config = Path(environment["OLIVIA_TTS_CONFIG"])
    assert config.is_file() and config.is_relative_to(install_root)
    worker = Path(environment["OLIVIA_MINIMAX_WORKER"])
    assert worker.is_file() and worker.is_relative_to(install_root)

    previous_config = config.read_bytes()
    reference.write_bytes(reference.read_bytes() + b"tampered")
    with pytest.raises(
        VideoCapabilityError, match="^VIDEO_RUNTIME_TTS_CONFIG_UNAVAILABLE$"
    ):
        installer._write_runtime_environment()
    assert config.read_bytes() == previous_config


def _managed_tts_config_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[VideoCapabilityInstaller, dict[str, str]]:
    installer = object.__new__(VideoCapabilityInstaller)
    installer.data_root = (tmp_path / "data").resolve()
    installer.install_root = installer.data_root / "capabilities/video"
    installer.install_root.mkdir(parents=True)
    installer._requires_breeze_hardware = True

    directories = {
        "OLIVIA_BREEZE_TTS_ROOT": installer.install_root / "ordinary/breeze/runtime",
        "OLIVIA_BREEZE_TTS_MODEL_ROOT": installer.install_root / "ordinary/breeze/model",
    }
    files = {
        "OLIVIA_BREEZE_TTS_MODEL_LICENSE": installer.install_root / "ordinary/breeze/model/LICENSE",
        "OLIVIA_BREEZE_TTS_PYTHON": installer.install_root / "ordinary/breeze/python/python.exe",
        "OLIVIA_REPLY_VOICE_REFERENCE": installer.install_root / "shared/linli-reference.wav",
    }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)
    for path in files.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")

    reference = files["OLIVIA_REPLY_VOICE_REFERENCE"]
    with wave.open(str(reference), "wb") as target:
        target.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        target.writeframes(b"\0\0")
    transcript = reference.with_suffix(".txt")
    transcript.write_text("synthetic exact transcript\n", encoding="utf-8")
    reference_sha256 = hashlib.sha256(reference.read_bytes()).hexdigest()
    reference.with_suffix(".json").write_text(
        json.dumps(
            {
                "schema_version": "olivia.managed-voice-reference.v2",
                "path": reference.name,
                "size_bytes": reference.stat().st_size,
                "sha256": reference_sha256,
                "transcript": {
                    "path": transcript.name,
                    "size_bytes": transcript.stat().st_size,
                    "sha256": hashlib.sha256(transcript.read_bytes()).hexdigest(),
                },
                "wave": {
                    "channels": 1,
                    "sample_width_bytes": 2,
                    "sample_rate_hz": 16_000,
                    "frame_count": 1,
                    "compression_type": "NONE",
                },
            }
        ),
        encoding="utf-8",
    )
    real_resolver = video_capability_install.resolve_managed_voice_reference
    monkeypatch.setattr(
        video_capability_install,
        "resolve_managed_voice_reference",
        lambda root: real_resolver(root, expected_sha256=reference_sha256),
    )
    return installer, {
        **{key: str(value) for key, value in directories.items()},
        **{key: str(value) for key, value in files.items()},
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point contract")
def test_managed_tts_config_rejects_generated_directory_junction_without_external_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer, environment = _managed_tts_config_fixture(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    generated = installer.install_root / "generated"
    linked = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(generated), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if linked.returncode != 0:
        pytest.skip("directory junction unavailable")

    with pytest.raises(
        VideoCapabilityError,
        match="^VIDEO_RUNTIME_TTS_CONFIG_UNAVAILABLE$",
    ):
        installer._generate_managed_tts_config(environment)

    assert not (outside / "tts_local.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point contract")
@pytest.mark.parametrize("leaf", ("tts_local.json", "tts_local.json.fixed.tmp"))
def test_managed_tts_config_rejects_target_and_temp_reparse_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, leaf: str
) -> None:
    installer, environment = _managed_tts_config_fixture(tmp_path, monkeypatch)
    generated = installer.install_root / "generated"
    generated.mkdir()
    outside = tmp_path / "outside-leaf"
    outside.mkdir()
    monkeypatch.setattr(
        video_capability_install.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="fixed"),
    )
    linked = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(generated / leaf), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if linked.returncode != 0:
        pytest.skip("leaf junction unavailable")

    with pytest.raises(
        VideoCapabilityError, match="^VIDEO_RUNTIME_TTS_CONFIG_UNAVAILABLE$"
    ):
        installer._generate_managed_tts_config(environment)

    assert not list(outside.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point contract")
def test_managed_tts_config_rechecks_generated_directory_after_temp_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer, environment = _managed_tts_config_fixture(tmp_path, monkeypatch)
    generated = installer.install_root / "generated"
    outside = tmp_path / "outside-after-write"
    outside.mkdir()
    outside_temp = outside / "tts_local.json.fixed.tmp"
    outside_temp.write_bytes(b"do-not-touch")
    parked = installer.install_root / "generated-parked"
    original_write_text = Path.write_text
    monkeypatch.setattr(
        video_capability_install.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="fixed"),
    )

    def write_then_swap(path: Path, *args: object, **kwargs: object) -> int:
        written = original_write_text(path, *args, **kwargs)
        if path.parent == generated and path.name.endswith(".tmp"):
            generated.rename(parked)
            linked = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(generated), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if linked.returncode != 0:
                pytest.skip("directory junction unavailable")
        return written

    monkeypatch.setattr(Path, "write_text", write_then_swap)

    with pytest.raises(
        VideoCapabilityError, match="^VIDEO_RUNTIME_TTS_CONFIG_UNAVAILABLE$"
    ):
        installer._generate_managed_tts_config(environment)

    assert not (outside / "tts_local.json").exists()
    assert outside_temp.read_bytes() == b"do-not-touch"


def _managed_worker_import_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[VideoCapabilityInstaller, Path, str, Path]:
    manifest = _runtime_ready_manifest()
    data_root = (tmp_path / "data").resolve()
    _prepare_runtime_dependencies(data_root, manifest)
    runtime_root = (tmp_path / "runtime-source").resolve()
    _runtime_archive(tmp_path)
    manifest_sha256 = hashlib.sha256(
        (runtime_root / "runtime-manifest.json").read_bytes()
    ).hexdigest()
    monkeypatch.setattr(
        video_capability_install, "_runtime_environment_is_portable", lambda *_: True
    )
    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=lambda _environment: {
            "ordinary_missing_dependencies": [],
            "music_ready": True,
        },
    )
    worker_directory = (
        data_root
        / "capabilities"
        / "video"
        / "music_video"
        / "minimax"
        / "runtime"
        / "tools"
    )
    return installer, runtime_root, manifest_sha256, worker_directory


def test_runtime_import_rejects_linked_managed_worker_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer, runtime_root, manifest_sha256, worker_directory = (
        _managed_worker_import_fixture(tmp_path, monkeypatch)
    )
    outside = tmp_path / "outside-worker"
    outside.mkdir()
    worker_directory.parent.mkdir(parents=True, exist_ok=True)
    try:
        worker_directory.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating directory links is unavailable")
    with pytest.raises(VideoCapabilityError, match="VIDEO_RUNTIME_WORKER_UNAVAILABLE"):
        installer.import_runtime_root(
            runtime_root=runtime_root, manifest_sha256=manifest_sha256
        )
    assert list(outside.iterdir()) == []


def test_unchanged_managed_workers_do_not_rescan_or_rewrite_music_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer, _, _, directory = _managed_worker_import_fixture(tmp_path, monkeypatch)
    installer._install_managed_minimax_worker()
    targets = [directory / "minimax_profile.py", directory / "minimax_music3_worker.py"]
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in targets]
    scans = []
    original = video_capability_install._reject_reparse_tree
    def scan(path):
        scans.append(path)
        return original(path)
    monkeypatch.setattr(video_capability_install, "_reject_reparse_tree", scan)
    installer._install_managed_minimax_worker()
    assert scans == []
    assert [(p.read_bytes(), p.stat().st_mtime_ns) for p in targets] == before
    targets[0].write_bytes(b"outdated worker")
    installer._install_managed_minimax_worker()
    assert scans == []
    assert targets[0].read_bytes() == before[0][0]


def test_runtime_import_reports_stable_worker_error_when_worker_directory_cannot_be_prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer, runtime_root, manifest_sha256, worker_directory = (
        _managed_worker_import_fixture(tmp_path, monkeypatch)
    )
    worker_directory = worker_directory.resolve()
    real_mkdir = Path.mkdir

    def mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path.resolve() == worker_directory:
            raise PermissionError("worker directory unavailable")
        real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)

    with pytest.raises(
        VideoCapabilityError, match="^VIDEO_RUNTIME_WORKER_UNAVAILABLE$"
    ):
        installer.import_runtime_root(
            runtime_root=runtime_root, manifest_sha256=manifest_sha256
        )

    assert installer.status()["runtime_import"]["reason_code"] == (
        "VIDEO_RUNTIME_WORKER_UNAVAILABLE"
    )


def test_runtime_import_restores_both_managed_worker_files_after_publish_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer, runtime_root, manifest_sha256, worker_directory = (
        _managed_worker_import_fixture(tmp_path, monkeypatch)
    )
    worker_directory.mkdir(parents=True)
    profile = worker_directory / "minimax_profile.py"
    worker = worker_directory / "minimax_music3_worker.py"
    profile.write_bytes(b"old-profile")
    worker.write_bytes(b"old-worker")
    real_replace = video_capability_install.os.replace

    def replace(source: object, target: object) -> None:
        if Path(target) == worker and Path(source).suffix == ".tmp":
            raise PermissionError("worker publication failed")
        real_replace(source, target)

    real_unlink = Path.unlink
    denied_profile_delete = False

    def unlink(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal denied_profile_delete
        if path == profile and not denied_profile_delete:
            denied_profile_delete = True
            raise PermissionError("profile still open")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(video_capability_install.os, "replace", replace)
    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(
        VideoCapabilityError, match="^VIDEO_RUNTIME_WORKER_UNAVAILABLE$"
    ):
        installer.import_runtime_root(
            runtime_root=runtime_root, manifest_sha256=manifest_sha256
        )

    assert profile.read_bytes() == b"old-profile"
    assert worker.read_bytes() == b"old-worker"
    assert not tuple(worker_directory.glob("*.tmp"))
    assert not tuple(worker_directory.glob("*.bak"))


def test_first_runtime_import_removes_partial_managed_worker_after_publish_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer, runtime_root, manifest_sha256, worker_directory = (
        _managed_worker_import_fixture(tmp_path, monkeypatch)
    )
    profile = worker_directory / "minimax_profile.py"
    worker = worker_directory / "minimax_music3_worker.py"
    real_replace = video_capability_install.os.replace

    def replace(source: object, target: object) -> None:
        if Path(target) == worker and Path(source).suffix == ".tmp":
            raise PermissionError("worker publication failed")
        real_replace(source, target)

    real_unlink = Path.unlink
    denied_profile_delete = False

    def unlink(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal denied_profile_delete
        if path == profile and not denied_profile_delete:
            denied_profile_delete = True
            raise PermissionError("profile still open")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(video_capability_install.os, "replace", replace)
    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(
        VideoCapabilityError, match="^VIDEO_RUNTIME_WORKER_UNAVAILABLE$"
    ):
        installer.import_runtime_root(
            runtime_root=runtime_root, manifest_sha256=manifest_sha256
        )

    assert not profile.exists()
    assert not worker.exists()
    assert not tuple(worker_directory.glob("*.tmp"))
    assert not tuple(worker_directory.glob("*.bak"))


def test_repeated_runtime_import_keeps_managed_worker_pair_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer, runtime_root, manifest_sha256, worker_directory = (
        _managed_worker_import_fixture(tmp_path, monkeypatch)
    )

    assert installer.import_runtime_root(
        runtime_root=runtime_root, manifest_sha256=manifest_sha256
    ) == "APPLIED"
    assert installer.import_runtime_root(
        runtime_root=runtime_root, manifest_sha256=manifest_sha256
    ) == "APPLIED"

    source_directory = Path(video_capability_install.__file__).resolve().parent / "tools"
    for name in ("minimax_profile.py", "minimax_music3_worker.py"):
        assert (worker_directory / name).read_bytes() == (
            source_directory / name
        ).read_bytes()
    assert not tuple(worker_directory.glob("*.tmp"))
    assert not tuple(worker_directory.glob("*.bak"))


def test_runtime_archive_failure_keeps_the_specific_step_reason(tmp_path: Path) -> None:
    archive = (tmp_path / "Olivia-video-runtime-broken.zip").resolve()
    with zipfile.ZipFile(archive, "w") as payload:
        payload.writestr("runtime-manifest.json", "{}")
    configured_downloads = (tmp_path / "downloads").resolve()
    configured_downloads.mkdir()
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest("1.0", ()),
        readiness_probe=lambda _environment: {},
        runtime_archive_roots=(configured_downloads,),
    )

    with pytest.raises(VideoCapabilityError, match="VIDEO_RUNTIME_ROOT_INVALID"):
        installer.import_runtime_archive(runtime_archive=archive)

    assert installer.status()["runtime_import"] == {
        "state": "failed",
        "checked_bytes": 0,
        "total_bytes": 0,
        "reason_code": "VIDEO_RUNTIME_ROOT_INVALID",
    }


def test_runtime_archive_background_start_returns_before_extraction_and_deduplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = (tmp_path / "Olivia-video-runtime-slow.zip").resolve()
    with zipfile.ZipFile(archive, "w") as payload:
        payload.writestr("runtime-manifest.json", "{}")
    started = threading.Event()
    release = threading.Event()

    def slow_extract(
        _archive: Path,
        staging: Path,
        *,
        next_member_index=0,
        checkpoint_progress=None,
        progress,
    ) -> None:
        staging.mkdir(parents=True)
        progress(1, 2)
        started.set()
        assert release.wait(2)
        (staging / "runtime-manifest.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        video_capability_install,
        "_extract_runtime_zip_safely",
        slow_extract,
    )
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest("1.0", ()),
        readiness_probe=lambda _environment: {},
    )

    try:
        assert (
            installer.start_runtime_archive_import(runtime_archive=archive)
            == "APPLIED"
        )
        assert started.wait(1)
        assert installer.status()["runtime_import"] == {
            "state": "extracting",
            "checked_bytes": 1,
            "total_bytes": 2,
        }
        assert (
            installer.start_runtime_archive_import(runtime_archive=archive) == "NOOP"
        )
    finally:
        release.set()

    deadline = time.monotonic() + 2
    while (
        time.monotonic() < deadline
        and installer.status()["runtime_import"]["state"] != "failed"
    ):
        time.sleep(0.01)
    failed = installer.status()["runtime_import"]
    assert failed["state"] == "failed"
    assert failed["reason_code"] == "VIDEO_RUNTIME_ROOT_INVALID"


def test_configured_installer_activates_runtime_for_current_server_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}
    process_environment: dict[str, str] = {}

    class Installer:
        def __init__(self, **kwargs) -> None:
            observed.update(kwargs)

    monkeypatch.setattr(original_client_server, "VideoCapabilityInstaller", Installer)
    monkeypatch.setattr(
        original_client_server,
        "load_video_manifest",
        lambda _path: VideoManifest("1.0", ()),
    )
    monkeypatch.setattr(original_client_server.os, "environ", process_environment)

    install_root = (tmp_path / "install").resolve()
    assert original_client_server._configured_video_capability_installer(
        {"OLIVIA_INSTALL_ROOT": str(install_root)}, (tmp_path / "data").resolve()
    ) is not None
    observed["runtime_environment_applier"]({"OLIVIA_LATENTSYNC_PYTHON": "runtime"})

    assert process_environment["OLIVIA_LATENTSYNC_PYTHON"] == "runtime"
    assert install_root / "downloads" in observed["runtime_archive_roots"]


def test_complete_download_automatically_prepares_new_runtime_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_root = (tmp_path / "downloads").resolve()
    archive_root.mkdir()
    data_root = (tmp_path / "data").resolve()
    manifest = _runtime_ready_manifest()
    _prepare_runtime_dependencies(data_root, manifest)
    monkeypatch.setattr(video_capability_install, "_runtime_environment_is_portable", lambda *_: True)

    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=lambda _environment: {
            "ordinary_missing_dependencies": [],
            "music_ready": True,
        },
        runtime_archive_roots=(archive_root,),
    )

    assert installer.status()["runtime_import"]["state"] == "required"
    _runtime_archive(archive_root)

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and installer.status()["runtime_import"]["state"] != "ready":
        time.sleep(0.01)

    assert installer.status()["runtime_import"]["state"] == "ready"
    assert [item["state"] for item in installer.status()["bundles"]] == ["ready", "ready"]


def test_failed_discovered_runtimes_retry_only_after_archive_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive_root = (tmp_path / "downloads").resolve()
    archive_root.mkdir()
    replacement = _runtime_archive(tmp_path / "replacement")
    manifest = _runtime_ready_manifest()
    data_root = (tmp_path / "data").resolve()
    _prepare_runtime_dependencies(data_root, manifest)
    monkeypatch.setattr(video_capability_install, "_runtime_environment_is_portable", lambda *_: True)
    installer = VideoCapabilityInstaller(
        data_root=data_root, manifest=manifest, readiness_probe=lambda _environment: {"ordinary_missing_dependencies": [], "music_ready": True}, runtime_archive_roots=(archive_root,),
    )
    attempts: list[Path] = []
    import_runtime_archive = installer.import_runtime_archive
    monkeypatch.setattr(installer, "import_runtime_archive", lambda *, runtime_archive: (attempts.append(runtime_archive), import_runtime_archive(runtime_archive=runtime_archive))[1])
    archives = tuple((archive_root / f"Olivia-video-runtime-{name}.zip").resolve() for name in ("a", "b"))
    for archive in archives:
        with zipfile.ZipFile(archive, "w") as payload:
            payload.writestr("runtime-manifest.json", "{}")
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        installer.status()
        time.sleep(0.01)
    assert len(attempts) == 2 and set(attempts) == set(archives)
    replacement.replace(archives[0])
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and installer.status()["runtime_import"]["state"] != "ready":
        time.sleep(0.01)
    assert installer.status()["runtime_import"]["state"] == "ready"
    assert len(attempts) == 3 and attempts[-1] == archives[0]


def test_complete_download_reports_missing_runtime_archive_instead_of_idle(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    install_root = data_root / "capabilities" / "video"
    for bundle_id in ("ordinary_video", "music_video"):
        root = install_root / bundle_id
        root.mkdir(parents=True, exist_ok=True)
        (root / ".ready.json").write_text(
            json.dumps(
                {
                    "schema_version": "olivia.video-bundle.v1",
                    "bundle": bundle_id,
                    "version": "1.0",
                }
            ),
            encoding="utf-8",
        )

    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=VideoManifest(
            "1.0",
            (
                VideoBundle("ordinary_video", "video", "FIXED", False, (), ()),
                VideoBundle("music_video", "music", "FIXED", False, (), ()),
            ),
        ),
        readiness_probe=lambda _environment: {
            "ordinary_missing_dependencies": ["cosyvoice", "latentsync"],
            "music_ready": False,
        },
    )

    status = installer.status()
    assert status["runtime_import"] == {
        "state": "required",
        "checked_bytes": 0,
        "total_bytes": 0,
        "reason_code": "VIDEO_RUNTIME_ARCHIVE_REQUIRED",
    }
    status_schema = json.loads(
        Path("contracts/video_capability_status.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(status_schema).validate(status)


def test_disappeared_runtime_archive_reports_required_instead_of_idle(
    tmp_path: Path,
) -> None:
    archive = _runtime_archive(tmp_path)
    data_root = (tmp_path / "data").resolve()
    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=_runtime_ready_manifest(),
        readiness_probe=lambda _environment: {},
        runtime_archives=(archive,),
    )
    archive.unlink()
    _prepare_runtime_dependencies(data_root, installer.manifest)
    status = installer.status()
    assert status["runtime_import"] == {
        "state": "required",
        "checked_bytes": 0,
        "total_bytes": 0,
        "reason_code": "VIDEO_RUNTIME_ARCHIVE_REQUIRED",
    }


def test_bundle_ready_requires_safe_archive_assembly_and_persisted_runtime_wiring(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "offline" / "sources" / "runtime.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as payload:
        payload.writestr("upstream/scripts/inference.py", "# pinned runtime\n")
    spec = VideoFile(
        "runtime", "sources/runtime.zip", archive.stat().st_size,
        hashlib.sha256(archive.read_bytes()).hexdigest(), "MIT", {}, True,
        VideoFileInstall("zip", "latentsync/runtime", 1),
    )
    ordinary = VideoBundle(
        "ordinary_video", "ordinary", "FIXED", True, ("official_video_assets",), (spec,), False,
        {"OLIVIA_LATENTSYNC_ROOT": "latentsync/runtime"},
    )
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest("1.0.0", (ordinary, VideoBundle("music_video", "music", "FIXED", True, (), ()))),
    )
    stale = installer.install_root / ".staging" / "ordinary_video" / "stale.bin"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"must not promote")

    assert installer.import_offline(
        bundle_id="ordinary_video", offline_root=archive.parents[1]
    ) == "APPLIED"
    assert _wait(installer, 0, "prerequisites_required", "failed") == "prerequisites_required"
    assert installer.status()["bundles"][0]["reason_code"] == "VIDEO_NATIVE_PATH_SELECTION_UNAVAILABLE"
    assert not (installer.install_root / "ordinary_video" / "stale.bin").exists()
    runtime = installer.install_root / "ordinary_video" / "latentsync" / "runtime"
    assert (runtime / "scripts" / "inference.py").read_text(encoding="utf-8") == "# pinned runtime\n"
    assert load_video_runtime_environment(installer.data_root) == {
        "OLIVIA_LATENTSYNC_ROOT": str(runtime.resolve())
    }
    profile = installer.install_root / "runtime-environment.json"
    profile_payload = profile.read_text(encoding="utf-8")
    profile.unlink()
    assert installer.status()["bundles"][0]["state"] == "prerequisites_required"
    profile.write_text(profile_payload, encoding="utf-8")
    assert installer.status()["bundles"][0]["state"] == "prerequisites_required"
    shutil.rmtree(runtime)
    assert installer.status()["bundles"][0]["state"] != "ready"


def test_downloaded_models_remain_installed_when_runtime_prerequisites_are_missing(
    tmp_path: Path,
) -> None:
    payload = b"verified model"
    offline_root = tmp_path / "offline"
    source = offline_root / "models" / "model.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    spec = VideoFile(
        "model",
        "models/model.bin",
        len(payload),
        hashlib.sha256(payload).hexdigest(),
        "fixture",
        {},
    )
    ordinary = VideoBundle(
        "ordinary_video",
        "ordinary",
        "FIXED",
        True,
        (),
        (spec,),
        False,
        {"OLIVIA_LATENTSYNC_PYTHON": "runtime/python/python.exe"},
    )
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest(
            "1.0",
            (
                ordinary,
                VideoBundle("music_video", "music", "FIXED", True, (), ()),
            ),
        ),
    )

    assert installer.import_offline(
        bundle_id="ordinary_video", offline_root=offline_root
    ) == "APPLIED"
    assert _wait(installer, 0, "prerequisites_required", "failed") == "prerequisites_required"
    status = installer.status()["bundles"][0]
    assert status["reason_code"] == "VIDEO_RUNTIME_PREREQUISITES_MISSING"
    assert (
        installer.install_root / "ordinary_video" / "models" / "model.bin"
    ).read_bytes() == payload
    assert load_video_runtime_environment(installer.data_root) == {}


def test_offline_retry_reuses_an_already_verified_cached_file(tmp_path: Path) -> None:
    payload = b"verified cached payload"
    spec = VideoFile(
        "model",
        "models/model.bin",
        len(payload),
        hashlib.sha256(payload).hexdigest(),
        "fixture",
        {},
    )
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest(
            "1.0",
            (
                VideoBundle("ordinary_video", "ordinary", "FIXED", False, (), (spec,)),
                VideoBundle("music_video", "music", "FIXED", False, (), ()),
            ),
        ),
    )
    target = installer.install_root / ".downloads/models/model.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    archive = tmp_path / "offline.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr(spec.relative_path, b"must not overwrite verified cache")

    installer._copy_offline(archive, spec, target)

    assert target.read_bytes() == payload


@pytest.mark.parametrize("corrupt", [False, True])
def test_offline_import_reads_hash_checked_wheels_from_supplement(
    tmp_path: Path, corrupt: bool,
) -> None:
    payload = b"locked wheel fixture"
    spec = VideoFile("wheel", "breeze/wheels/fixture.whl", len(payload),
                     hashlib.sha256(payload).hexdigest(), "MIT", {})
    archive = tmp_path / "old-video.zip"
    with zipfile.ZipFile(archive, "w"):
        pass
    with zipfile.ZipFile(tmp_path / "Olivia-breeze-runtime-offline.zip", "w") as supplement:
        supplement.writestr(spec.relative_path, b"x" * len(payload) if corrupt else payload)
    def reject_network(*args, **kwargs):
        raise AssertionError("Offline import contacted network")
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest("fixture", (
            VideoBundle("ordinary_video", "ordinary", "MIT", False, (), (spec,)),
        )), opener=reject_network,
    )
    assert installer.import_offline(bundle_id="ordinary_video", offline_root=archive) == "APPLIED"
    state = _wait(installer, 0, "ready", "prerequisites_required", "failed")
    if corrupt:
        assert state == "failed"
        assert not (installer.install_root / "ordinary_video" / spec.relative_path).exists()
    else:
        assert state in {"ready", "prerequisites_required"}, installer.status()
        assert (installer.install_root / "ordinary_video" / spec.relative_path).read_bytes() == payload


def test_manifest_append_downloads_only_new_direct_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_payload = b"large existing model"
    new_payload = b"small new scene"
    old_spec = VideoFile(
        "existing-model",
        "models/existing.bin",
        len(old_payload),
        hashlib.sha256(old_payload).hexdigest(),
        "fixture",
        {"official": "https://example.invalid/existing.bin"},
    )
    new_spec = VideoFile(
        "new-scene",
        "scenes/new.mp4",
        len(new_payload),
        hashlib.sha256(new_payload).hexdigest(),
        "fixture",
        {"official": "https://example.invalid/new.mp4"},
    )
    data_root = (tmp_path / "data").resolve()
    root = data_root / "capabilities" / "video" / "ordinary_video"
    existing = root / old_spec.relative_path
    existing.parent.mkdir(parents=True)
    existing.write_bytes(old_payload)
    (root / ".ready.json").write_text(
        json.dumps(
            {
                "schema_version": "olivia.video-bundle.v1",
                "bundle": "ordinary_video",
                "version": "1.0",
            }
        ),
        encoding="utf-8",
    )
    music_root = root.parent / "music_video"
    music_root.mkdir()
    (music_root / ".ready.json").write_text(
        json.dumps(
            {
                "schema_version": "olivia.video-bundle.v1",
                "bundle": "music_video",
                "version": "1.0",
            }
        ),
        encoding="utf-8",
    )
    runtime_profile = root.parent / "runtime-environment.json"
    runtime_profile.write_text(
        json.dumps(
            {
                "schema_version": "olivia.video-runtime-environment.v1",
                "environment": {"OLIVIA_FFMPEG_EXE": str(existing.resolve())},
            }
        ),
        encoding="utf-8",
    )
    requested: list[str] = []

    class Response:
        status = 200

        def __init__(self) -> None:
            self.sent = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size: int) -> bytes:
            if self.sent:
                return b""
            self.sent = True
            return new_payload

    def opener(request, **_kwargs):
        requested.append(request.full_url)
        assert request.full_url.endswith("/new.mp4")
        return Response()

    ordinary = VideoBundle(
        "ordinary_video",
        "ordinary",
        "FIXED",
        False,
        (),
        (old_spec, new_spec),
        False,
        {"OLIVIA_ORDINARY_ACTION_BASE": new_spec.relative_path},
    )
    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=VideoManifest(
            "2.0",
            (
                ordinary,
                VideoBundle("music_video", "music", "FIXED", False, (), ()),
            ),
        ),
        opener=opener,
    )
    monkeypatch.setattr(
        installer,
        "_promote_directory",
        lambda *_args, **_kwargs: pytest.fail("append-only update must not restage"),
    )

    assert installer.start(bundle_id="ordinary_video", source_mode="official") == "APPLIED"
    assert _wait(installer, 0, "ready", "failed") == "ready"
    assert requested == ["https://example.invalid/new.mp4"]
    assert existing.read_bytes() == old_payload
    assert (root / new_spec.relative_path).read_bytes() == new_payload
    assert json.loads((root / ".ready.json").read_text(encoding="utf-8"))[
        "version"
    ] == "2.0"
    environment = load_video_runtime_environment(data_root)
    assert environment["OLIVIA_FFMPEG_EXE"] == str(existing.resolve())
    assert environment["OLIVIA_ORDINARY_ACTION_BASE"] == str(
        (root / new_spec.relative_path).resolve()
    )
    assert installer.start(bundle_id="music_video", source_mode="official") == "APPLIED"
    assert _wait(installer, 1, "ready", "failed") == "ready"
    assert requested == ["https://example.invalid/new.mp4"]
    assert json.loads((music_root / ".ready.json").read_text(encoding="utf-8"))[
        "version"
    ] == "2.0"


def test_install_fully_verifies_staged_payload_before_writing_ready_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"verified model"
    offline_root = tmp_path / "offline"
    source = offline_root / "models" / "model.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    bundle = VideoBundle(
        "ordinary_video",
        "ordinary",
        "FIXED",
        False,
        (),
        (
            VideoFile(
                "model",
                "models/model.bin",
                len(payload),
                hashlib.sha256(payload).hexdigest(),
                "fixture",
                {},
            ),
        ),
    )
    marker_presence_during_verification: list[bool] = []
    original_verify_staged_tree = video_capability_install._verify_staged_tree

    def observe_verification(root: Path, expected: list[dict[str, object]]) -> None:
        marker_presence_during_verification.append((root / ".ready.json").exists())
        original_verify_staged_tree(root, expected)

    monkeypatch.setattr(
        video_capability_install, "_verify_staged_tree", observe_verification
    )
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest("1.0", (bundle,)),
    )

    assert installer.import_offline(
        bundle_id=bundle.identifier, offline_root=offline_root
    ) == "APPLIED"
    assert _wait(installer, 0, "ready", "failed") == "ready"
    assert marker_presence_during_verification == [False]


def test_import_runtime_root_verifies_manifest_and_persists_external_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "video_capability_install._runtime_environment_is_portable",
        lambda *_args: True,
    )
    runtime_root = (tmp_path / "portable-runtime").resolve()
    python = runtime_root / "latentsync/python/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"accepted-python")
    tts_config = runtime_root / "config/tts_local.json"
    tts_config.parent.mkdir(parents=True)
    tts_config.write_text("{}", encoding="utf-8")
    runtime_manifest = {
        "schema_version": "olivia.video-runtime-root.v1",
        "version": "2026.08.29",
        "environment": {
            "OLIVIA_LATENTSYNC_PYTHON": "latentsync/python/python.exe",
            "OLIVIA_TTS_CONFIG": "config/tts_local.json",
        },
        "files": [
            {
                "path": "latentsync/python/python.exe",
                "size_bytes": python.stat().st_size,
                "sha256": hashlib.sha256(python.read_bytes()).hexdigest(),
            },
            {
                "path": "config/tts_local.json",
                "size_bytes": tts_config.stat().st_size,
                "sha256": hashlib.sha256(tts_config.read_bytes()).hexdigest(),
            },
        ],
    }
    manifest_path = runtime_root / "runtime-manifest.json"
    manifest_path.write_text(json.dumps(runtime_manifest), encoding="utf-8")
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    ordinary = VideoBundle(
        "ordinary_video",
        "ordinary",
        "FIXED",
        False,
        (),
        (),
        False,
        {"OLIVIA_LATENTSYNC_PYTHON": "managed/python.exe"},
    )
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest(
            "1.0",
            (ordinary, VideoBundle("music_video", "music", "FIXED", False, (), ())),
        ),
        readiness_probe=lambda _environment: {
            "ordinary_missing_dependencies": [],
            "music_ready": True,
        },
    )
    final = installer.install_root / "ordinary_video"
    final.mkdir(parents=True)
    (final / ".ready.json").write_text(
        json.dumps(
            {
                "schema_version": "olivia.video-bundle.v1",
                "bundle": "ordinary_video",
                "version": "1.0",
            }
        ),
        encoding="utf-8",
    )

    assert installer.import_runtime_root(
        runtime_root=runtime_root,
        manifest_sha256=manifest_sha,
    ) == "APPLIED"
    assert load_video_runtime_environment(installer.data_root) == {
        "OLIVIA_LATENTSYNC_PYTHON": str(python.resolve()),
        "OLIVIA_TTS_CONFIG": str(tts_config.resolve()),
    }
    assert installer.status()["bundles"][0]["state"] == "ready"
    assert installer.status()["runtime_import"] == {
        "state": "ready",
        "checked_bytes": python.stat().st_size + tts_config.stat().st_size,
        "total_bytes": python.stat().st_size + tts_config.stat().st_size,
    }
    status_schema = json.loads(
        Path("contracts/video_capability_status.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(status_schema).validate(installer.status())

    restarted = VideoCapabilityInstaller(
        data_root=installer.data_root,
        manifest=installer.manifest,
        readiness_probe=lambda _environment: pytest.fail(
            "a verified external runtime must not rerun heavyweight probes on restart"
        ),
    )
    assert restarted.status()["bundles"][0]["state"] == "ready"

    (runtime_root / "unlisted-provider.py").write_text(
        "raise RuntimeError('must never execute')", encoding="utf-8"
    )
    with pytest.raises(VideoCapabilityError, match="VIDEO_RUNTIME_ROOT_INVALID"):
        installer.import_runtime_root(
            runtime_root=runtime_root,
            manifest_sha256=manifest_sha,
        )
    assert installer.status()["runtime_import"]["state"] == "failed"
    (runtime_root / "unlisted-provider.py").unlink()

    runtime_manifest["environment"]["OLIVIA_LATENTSYNC_PYTHON"] = "../outside.exe"
    manifest_path.write_text(json.dumps(runtime_manifest), encoding="utf-8")
    bad_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    with pytest.raises(VideoCapabilityError, match="VIDEO_RUNTIME_ROOT_INVALID"):
        installer.import_runtime_root(
            runtime_root=runtime_root,
            manifest_sha256=bad_sha,
        )


def test_runtime_manifest_verification_reports_real_byte_progress(tmp_path: Path) -> None:
    runtime_root = (tmp_path / "runtime").resolve()
    python = runtime_root / "python/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python-runtime")
    config = runtime_root / "config/tts.json"
    config.parent.mkdir(parents=True)
    config.write_bytes(b"tts-config")
    files = [python, config]
    manifest = {
        "schema_version": "olivia.video-runtime-root.v1",
        "version": "test",
        "environment": {
            "OLIVIA_LATENTSYNC_PYTHON": "python/python.exe",
            "OLIVIA_TTS_CONFIG": "config/tts.json",
        },
        "files": [
            {
                "path": path.relative_to(runtime_root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in files
        ],
    }
    manifest_path = runtime_root / "runtime-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    progress: list[tuple[int, int]] = []

    video_capability_install._load_runtime_root_manifest(
        runtime_root,
        hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        verify_files=True,
        progress=lambda checked, total: progress.append((checked, total)),
    )

    expected_total = sum(path.stat().st_size for path in files)
    assert progress[0] == (0, expected_total)
    assert progress[-1] == (expected_total, expected_total)
    assert [checked for checked, _total in progress] == sorted(
        checked for checked, _total in progress
    )


def test_runtime_manifest_file_hashes_run_in_parallel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = (tmp_path / "runtime").resolve()
    python = runtime_root / "python/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    for index in range(4):
        target = runtime_root / f"packages/item-{index}.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"item-{index}".encode())
    manifest_sha256 = write_runtime_root_manifest(
        runtime_root,
        version="parallel-test",
        environment={"OLIVIA_LATENTSYNC_PYTHON": "python/python.exe"},
    )
    real_sha256_file = video_capability_install._sha256_file
    started_together = threading.Event()
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    def observed_sha256(path: Path, **kwargs: object) -> tuple[int, str]:
        nonlocal active, maximum_active
        if path.name == "runtime-manifest.json":
            return real_sha256_file(path, **kwargs)
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            if active >= 2:
                started_together.set()
        try:
            started_together.wait(timeout=0.5)
            return real_sha256_file(path, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(video_capability_install, "_sha256_file", observed_sha256)
    monkeypatch.setattr(video_capability_install.os, "cpu_count", lambda: 4)

    video_capability_install._load_runtime_root_manifest(
        runtime_root,
        manifest_sha256,
        verify_files=True,
    )

    assert maximum_active >= 2


def test_runtime_manifest_parallel_hash_queue_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = (tmp_path / "runtime").resolve()
    python = runtime_root / "python/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    for index in range(64):
        target = runtime_root / f"packages/item-{index:03d}.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"item-{index}".encode())
    manifest_sha256 = write_runtime_root_manifest(
        runtime_root,
        version="bounded-test",
        environment={"OLIVIA_LATENTSYNC_PYTHON": "python/python.exe"},
    )
    real_sha256_file = video_capability_install._sha256_file
    release = threading.Event()
    pending = 0
    maximum_pending = 0
    lock = threading.Lock()

    class TrackedFuture:
        def __init__(self, future: Future[tuple[int, str]]) -> None:
            self._future = future
            self._released = False

        def _release(self) -> None:
            nonlocal pending
            with lock:
                if not self._released:
                    self._released = True
                    pending -= 1

        def result(self) -> tuple[int, str]:
            try:
                return self._future.result()
            finally:
                self._release()

        def cancel(self) -> bool:
            cancelled = self._future.cancel()
            if cancelled:
                self._release()
            return cancelled

    class TrackingExecutor:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._executor = ThreadPoolExecutor(*args, **kwargs)

        def submit(self, function: object, *args: object) -> TrackedFuture:
            nonlocal pending, maximum_pending
            future = self._executor.submit(function, *args)
            with lock:
                pending += 1
                maximum_pending = max(maximum_pending, pending)
            return TrackedFuture(future)

        def shutdown(self, **kwargs: object) -> None:
            self._executor.shutdown(**kwargs)

    def blocked_sha256(path: Path, **kwargs: object) -> tuple[int, str]:
        if path.name != "runtime-manifest.json":
            release.wait(timeout=1)
        return real_sha256_file(path, **kwargs)

    monkeypatch.setattr(video_capability_install, "ThreadPoolExecutor", TrackingExecutor)
    monkeypatch.setattr(video_capability_install, "_sha256_file", blocked_sha256)
    monkeypatch.setattr(video_capability_install.os, "cpu_count", lambda: 4)
    timer = threading.Timer(0.2, release.set)
    timer.start()
    try:
        video_capability_install._load_runtime_root_manifest(
            runtime_root,
            manifest_sha256,
            verify_files=True,
        )
    finally:
        release.set()
        timer.cancel()

    assert maximum_pending <= 8
    assert maximum_pending < 64


def test_runtime_manifest_parallel_hash_rejects_same_size_tamper(
    tmp_path: Path,
) -> None:
    runtime_root = (tmp_path / "runtime").resolve()
    python = runtime_root / "python/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"original")
    manifest_sha256 = write_runtime_root_manifest(
        runtime_root,
        version="tamper-test",
        environment={"OLIVIA_LATENTSYNC_PYTHON": "python/python.exe"},
    )
    python.write_bytes(b"tampered")

    with pytest.raises(VideoCapabilityError, match="^VIDEO_RUNTIME_ROOT_INVALID$"):
        video_capability_install._load_runtime_root_manifest(
            runtime_root,
            manifest_sha256,
            verify_files=True,
        )


def test_runtime_manifest_parallel_hash_reports_first_manifest_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = (tmp_path / "runtime").resolve()
    first = runtime_root / "a-first.bin"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"first")
    second = runtime_root / "b-second.bin"
    second.write_bytes(b"second")
    manifest_sha256 = write_runtime_root_manifest(
        runtime_root,
        version="ordered-error-test",
        environment={"OLIVIA_LATENTSYNC_PYTHON": "a-first.bin"},
    )
    real_sha256_file = video_capability_install._sha256_file
    both_started = threading.Event()
    started = 0
    lock = threading.Lock()

    def failing_sha256(path: Path, **kwargs: object) -> tuple[int, str]:
        nonlocal started
        if path.name == "runtime-manifest.json":
            return real_sha256_file(path, **kwargs)
        with lock:
            started += 1
            if started >= 2:
                both_started.set()
        both_started.wait(timeout=1)
        if path.name == "a-first.bin":
            raise FileNotFoundError("first manifest entry")
        raise PermissionError("second manifest entry")

    monkeypatch.setattr(video_capability_install, "_sha256_file", failing_sha256)
    monkeypatch.setattr(video_capability_install.os, "cpu_count", lambda: 2)

    with pytest.raises(VideoCapabilityError) as error:
        video_capability_install._load_runtime_root_manifest(
            runtime_root,
            manifest_sha256,
            verify_files=True,
        )

    assert isinstance(error.value.__cause__, FileNotFoundError)
    assert str(error.value.__cause__) == "first manifest entry"


def test_runtime_manifest_generator_hashes_the_exact_sorted_tree(tmp_path: Path) -> None:
    runtime_root = (tmp_path / "runtime").resolve()
    python = runtime_root / "python/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    worker = runtime_root / "tools/worker.py"
    worker.parent.mkdir(parents=True)
    worker.write_bytes(b"worker")

    digest = write_runtime_root_manifest(
        runtime_root,
        version="2026.08.29",
        environment={
            "OLIVIA_LATENTSYNC_PYTHON": "python/python.exe",
            "OLIVIA_MINIMAX_WORKER": "tools/worker.py",
        },
    )

    manifest_path = runtime_root / "runtime-manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert digest == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert [item["path"] for item in payload["files"]] == [
        "python/python.exe",
        "tools/worker.py",
    ]
    assert not any(str(runtime_root) in value for value in payload["environment"].values())


def test_runtime_manifest_duplicate_check_is_linear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CountingPath(str):
        calls = 0

        def casefold(self) -> str:
            type(self).calls += 1
            return super().casefold()

    runtime_root = (tmp_path / "runtime").resolve()
    python = runtime_root / "python/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    manifest_path = runtime_root / "runtime-manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    digest = "a" * 64
    paths = [CountingPath("python/python.exe")]
    paths.extend(CountingPath(f"packages/item-{index}.bin") for index in range(99))
    payload = {
        "schema_version": "olivia.video-runtime-root.v1",
        "version": "2026.08.29",
        "environment": {"OLIVIA_LATENTSYNC_PYTHON": paths[0]},
        "files": [
            {"path": path, "size_bytes": 1, "sha256": "0" * 64}
            for path in paths
        ],
    }
    monkeypatch.setattr(video_capability_install, "_safe_relative", lambda value: value)
    monkeypatch.setattr(video_capability_install, "_sha256_file", lambda _path: (1, digest))
    monkeypatch.setattr(video_capability_install.json, "loads", lambda _raw: payload)

    environment = video_capability_install._load_runtime_root_manifest(
        runtime_root,
        digest,
        verify_files=False,
    )

    assert environment["OLIVIA_LATENTSYNC_PYTHON"] == str(python)
    assert CountingPath.calls <= len(paths) * 4


def test_portable_python_probe_rejects_a_runtime_outside_its_base_prefix(
    tmp_path: Path,
) -> None:
    base_root = Path(sys.base_prefix).resolve()
    python = base_root / "python.exe"
    if not python.is_file():
        pytest.skip("base interpreter is unavailable")
    assert _portable_python_runtime(python, base_root)
    assert not _portable_python_runtime(Path(sys.executable).resolve(), tmp_path.resolve())


def test_portable_python_probe_hides_its_windows_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = (tmp_path / "runtime").resolve()
    python = runtime_root / "python.exe"
    runtime_root.mkdir()
    python.write_bytes(b"fixture")
    observed: dict[str, object] = {}

    def run(*_args, **kwargs):
        observed.update(kwargs)
        return video_capability_install.subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(video_capability_install.subprocess, "run", run)

    assert _portable_python_runtime(python, runtime_root)
    assert observed["creationflags"] == getattr(
        video_capability_install.subprocess,
        "CREATE_NO_WINDOW",
        0,
    )


def test_runtime_profile_restores_interrupted_bundle_backup(tmp_path: Path) -> None:
    install_root = tmp_path / "data" / "capabilities" / "video"
    backup = install_root / ".ordinary_video.backup"
    runtime = backup / "latentsync" / "runtime"
    runtime.mkdir(parents=True)
    final_runtime = (install_root / "ordinary_video" / "latentsync" / "runtime").resolve()
    profile = {"schema_version": "olivia.video-runtime-environment.v1", "environment": {"OLIVIA_LATENTSYNC_ROOT": str(final_runtime)}}
    (install_root / "runtime-environment.json").write_text(json.dumps(profile), encoding="utf-8")
    assert load_video_runtime_environment((tmp_path / "data").resolve())["OLIVIA_LATENTSYNC_ROOT"] == str((install_root / "ordinary_video" / "latentsync" / "runtime").resolve())
    assert (install_root / "ordinary_video" / "latentsync" / "runtime").exists()
    assert not backup.exists()


def test_ready_state_uses_complete_video_dependency_probe(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    observed: list[dict[str, str]] = []

    def probe(environment):
        observed.append(dict(environment))
        return {
            "music_ready": False,
            "ordinary_missing_dependencies": [],
        }

    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=VideoManifest(
            "1.0",
            (
                VideoBundle("ordinary_video", "ordinary", "FIXED", True, (), ()),
                VideoBundle("music_video", "music", "FIXED", True, (), ()),
            ),
        ),
        readiness_probe=probe,
    )
    for bundle_id in ("ordinary_video", "music_video"):
        root = installer.install_root / bundle_id
        root.mkdir(parents=True)
        (root / ".ready.json").write_text(
            json.dumps(
                {
                    "schema_version": "olivia.video-bundle.v1",
                    "bundle": bundle_id,
                    "version": "1.0",
                }
            ),
            encoding="utf-8",
        )
    ffmpeg = (installer.install_root / "ordinary_video" / "ffmpeg.exe").resolve()
    ffmpeg.write_bytes(b"fixture")
    (installer.install_root / "runtime-environment.json").write_text(
        json.dumps(
            {
                "schema_version": "olivia.video-runtime-environment.v1",
                "environment": {"OLIVIA_FFMPEG_EXE": str(ffmpeg)},
            }
        ),
        encoding="utf-8",
    )

    status = installer.status()

    assert [bundle["state"] for bundle in status["bundles"]] == [
        "ready",
        "prerequisites_required",
    ]
    assert status["bundles"][1]["reason_code"] == "VIDEO_RUNTIME_DEPENDENCIES_MISSING"
    assert len(observed) == 1
    assert observed[-1]["OLIVIA_LOCAL_DATA_ROOT"] == str(data_root)
    assert observed[-1]["OLIVIA_FFMPEG_EXE"] == str(ffmpeg)


def test_ready_status_does_not_rehash_installed_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = (tmp_path / "data").resolve()
    bundle = VideoBundle(
        "ordinary_video",
        "ordinary",
        "FIXED",
        False,
        (),
        (
            VideoFile(
                "model",
                "models/model.bin",
                7,
                hashlib.sha256(b"fixture").hexdigest(),
                "MIT",
                {},
            ),
        ),
    )
    root = data_root / "capabilities" / "video" / bundle.identifier
    (root / "models").mkdir(parents=True)
    (root / "models" / "model.bin").write_bytes(b"fixture")
    (root / ".ready.json").write_text(
        json.dumps(
            {
                "schema_version": "olivia.video-bundle.v1",
                "bundle": bundle.identifier,
                "version": "1.0",
            }
        ),
        encoding="utf-8",
    )

    def reject_hash(*_args, **_kwargs):
        raise AssertionError("steady-state status must not hash installed files")

    monkeypatch.setattr(video_capability_install, "_sha256_file", reject_hash)

    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=VideoManifest("1.0", (bundle,)),
    )

    assert installer.status()["bundles"][0]["state"] == "ready"


@pytest.mark.parametrize(
    "target_kind", ("bundle_root", "nested_parent", "marker", "file")
)
def test_ready_status_rejects_reparse_points_in_bundle_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_kind: str
) -> None:
    data_root = (tmp_path / target_kind / "data").resolve()
    bundle = VideoBundle(
        "ordinary_video",
        "ordinary",
        "FIXED",
        False,
        (),
        (
            VideoFile(
                "model",
                "models/nested/model.bin",
                7,
                hashlib.sha256(b"fixture").hexdigest(),
                "MIT",
                {},
            ),
        ),
    )
    root = data_root / "capabilities" / "video" / bundle.identifier
    model = root / "models" / "nested" / "model.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"fixture")
    marker = root / ".ready.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": "olivia.video-bundle.v1",
                "bundle": bundle.identifier,
                "version": "1.0",
            }
        ),
        encoding="utf-8",
    )
    targets = {
        "bundle_root": root,
        "nested_parent": model.parent,
        "marker": marker,
        "file": model,
    }
    target = targets[target_kind]
    monkeypatch.setattr(
        video_capability_install,
        "_is_reparse_point",
        lambda path: path == target,
    )

    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=VideoManifest("1.0", (bundle,)),
    )

    assert installer.status()["bundles"][0]["state"] == "missing"


def test_ready_status_fails_closed_when_reparse_lstat_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = (tmp_path / "data").resolve()
    bundle = VideoBundle("ordinary_video", "ordinary", "FIXED", False, (), ())
    root = data_root / "capabilities" / "video" / bundle.identifier
    root.mkdir(parents=True)
    marker = root / ".ready.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": "olivia.video-bundle.v1",
                "bundle": bundle.identifier,
                "version": "1.0",
            }
        ),
        encoding="utf-8",
    )

    def race(path: Path) -> bool:
        if path == marker:
            raise video_capability_install.ComponentUpdateError(
                "UPDATE_STAGED_TREE_MISMATCH"
            )
        return False

    monkeypatch.setattr(video_capability_install, "_is_reparse_point", race)

    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=VideoManifest("1.0", (bundle,)),
    )

    assert installer.status()["bundles"][0]["state"] == "missing"


@pytest.mark.parametrize(
    "damage",
    ("corrupt_marker", "wrong_bundle", "wrong_version", "missing_file", "wrong_size"),
)
def test_ready_status_fails_closed_on_stale_marker_or_payload_shape(
    tmp_path: Path, damage: str
) -> None:
    data_root = (tmp_path / damage / "data").resolve()
    bundle = VideoBundle(
        "ordinary_video",
        "ordinary",
        "FIXED",
        False,
        (),
        (
            VideoFile(
                "model",
                "models/model.bin",
                7,
                hashlib.sha256(b"fixture").hexdigest(),
                "MIT",
                {},
            ),
        ),
    )
    root = data_root / "capabilities" / "video" / bundle.identifier
    model = root / "models" / "model.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"fixture")
    marker = root / ".ready.json"
    marker_payload = {
        "schema_version": "olivia.video-bundle.v1",
        "bundle": bundle.identifier,
        "version": "1.0",
    }
    if damage == "corrupt_marker":
        marker.write_text("{", encoding="utf-8")
    else:
        if damage == "wrong_bundle":
            marker_payload["bundle"] = "music_video"
        elif damage == "wrong_version":
            marker_payload["version"] = "0.9"
        elif damage == "missing_file":
            model.unlink()
        elif damage == "wrong_size":
            model.write_bytes(b"fixture!")
        marker.write_text(json.dumps(marker_payload), encoding="utf-8")

    installer = VideoCapabilityInstaller(
        data_root=data_root,
        manifest=VideoManifest("1.0", (bundle,)),
    )

    assert installer.status()["bundles"][0]["state"] == "missing"


def test_auto_install_reuses_verified_local_artifact_before_network(
    tmp_path: Path,
) -> None:
    payload = b"accepted-runtime-model"
    accepted_root = (tmp_path / "accepted-latentsync").resolve()
    accepted = accepted_root / "checkpoints" / "model.pt"
    accepted.parent.mkdir(parents=True)
    accepted.write_bytes(payload)
    spec = VideoFile(
        "model",
        "latentsync/runtime/checkpoints/model.pt",
        len(payload),
        hashlib.sha256(payload).hexdigest(),
        "fixture",
        {"official": "https://example.invalid/model.pt"},
    )

    def no_network(*_args, **_kwargs):
        raise AssertionError("verified local artifact should win")

    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest(
            "1.0",
            (
                VideoBundle("ordinary_video", "ordinary", "FIXED", True, (), (spec,)),
                VideoBundle("music_video", "music", "FIXED", True, (), ()),
            ),
        ),
        opener=no_network,
        artifact_roots=(accepted_root,),
    )

    assert installer.start(bundle_id="ordinary_video") == "APPLIED"
    assert _wait(installer, 0, "ready", "failed") == "ready"
    assert (
        installer.install_root
        / "ordinary_video"
        / "latentsync"
        / "runtime"
        / "checkpoints"
        / "model.pt"
    ).read_bytes() == payload


def test_installer_rejects_reparse_install_root(tmp_path: Path, monkeypatch) -> None:
    data_root = (tmp_path / "data").resolve()
    (data_root / "capabilities").mkdir(parents=True)
    monkeypatch.setattr("video_capability_install._is_reparse_point", lambda path: path.name == "capabilities")
    with pytest.raises(VideoCapabilityError, match="VIDEO_INSTALL_ROOT_INVALID"):
        VideoCapabilityInstaller(data_root=data_root, manifest=VideoManifest("1.0", ()))


def test_runtime_profile_read_waits_for_promotion_lock(tmp_path: Path, monkeypatch) -> None:
    installer = VideoCapabilityInstaller(data_root=(tmp_path / "data").resolve(), manifest=VideoManifest("1.0", ()))
    entered = threading.Event()
    monkeypatch.setattr("video_capability_install._load_video_runtime_environment", lambda _: entered.set() or {})
    with installer._commit_lock:
        worker = threading.Thread(target=load_video_runtime_environment, args=(installer.data_root,))
        worker.start()
        assert not entered.wait(0.1)
    worker.join(timeout=1)
    assert entered.is_set()


@pytest.mark.parametrize(("names", "reason"), [(('Runtime/inference.py', 'runtime/inference.py'), 'VIDEO_ARCHIVE_DUPLICATE_PATH'), (("asset:stream",), "VIDEO_ARCHIVE_PATH_INVALID"), (("CON/file.txt",), "VIDEO_ARCHIVE_PATH_INVALID"), (("trailing./file.txt",), "VIDEO_ARCHIVE_PATH_INVALID")])
def test_safe_archive_rejects_windows_unsafe_paths(tmp_path: Path, names, reason) -> None:
    archive = tmp_path / "collision.zip"
    with zipfile.ZipFile(archive, "w") as payload:
        for name in names:
            payload.writestr(name, "fixture")

    with pytest.raises(VideoCapabilityError, match=reason):
        _extract_zip_safely(archive, tmp_path / "runtime", strip_components=0)


def test_safe_archive_still_bounds_expanded_regular_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("video_capability_install._MAX_ARCHIVE_EXPANDED_BYTES", 4)
    archive = tmp_path / "oversized.zip"
    with zipfile.ZipFile(archive, "w") as payload:
        payload.writestr("runtime/worker.py", b"12345")

    destination = tmp_path / "runtime"
    with pytest.raises(VideoCapabilityError, match="VIDEO_ARCHIVE_TOO_LARGE"):
        _extract_zip_safely(archive, destination, strip_components=0)

    assert not (destination / "runtime" / "worker.py").exists()


def test_safe_archive_preserves_preinstalled_model_files(tmp_path: Path) -> None:
    destination = tmp_path / "runtime"
    model = destination / "models" / "weights.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"model")
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as payload:
        payload.writestr("source-main/inference.py", "print('ready')\n")

    _extract_zip_safely(archive, destination, strip_components=1)

    assert model.read_bytes() == b"model"
    assert (destination / "inference.py").read_text(encoding="utf-8") == "print('ready')\n"


def test_current_cosyvoice_snapshot_shape_installs_without_archive_symlinks(
    tmp_path: Path,
) -> None:
    revision = "074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc"
    prefix = f"CosyVoice-{revision}"
    symlinks = {
        "examples/libritts/cosyvoice2/local": "../cosyvoice/local",
        "examples/libritts/cosyvoice2/path.sh": "../cosyvoice/path.sh",
        "examples/libritts/cosyvoice2/tts_text.json": "../cosyvoice/tts_text.json",
        "examples/libritts/cosyvoice3/local": "../cosyvoice/local",
        "examples/libritts/cosyvoice3/path.sh": "../cosyvoice/path.sh",
        "examples/magicdata-read/cosyvoice/conf": "../../libritts/cosyvoice/conf",
        "examples/magicdata-read/cosyvoice/path.sh": "../../libritts/cosyvoice/path.sh",
        "runtime/triton_trtllm/token2wav_dit.py": (
            "model_repo/token2wav_dit/1/token2wav_dit.py"
        ),
    }
    archive = tmp_path / "offline" / "sources" / "CosyVoice-074ca6dc.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as payload:
        payload.writestr(f"{prefix}/cosyvoice/__init__.py", "# pinned runtime\n")
        for relative, target in symlinks.items():
            member = zipfile.ZipInfo(f"{prefix}/{relative}")
            member.create_system = 3
            member.external_attr = (stat.S_IFLNK | 0o777) << 16
            payload.writestr(member, target)
    spec = VideoFile(
        "cosyvoice-code",
        "sources/CosyVoice-074ca6dc.zip",
        archive.stat().st_size,
        hashlib.sha256(archive.read_bytes()).hexdigest(),
        "Apache-2.0",
        {},
        True,
        VideoFileInstall("zip", "cosyvoice/runtime", 1),
    )
    ordinary = VideoBundle(
        "ordinary_video", "ordinary", "FIXED", True, (), (spec,)
    )
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest(
            "1.0",
            (
                ordinary,
                VideoBundle("music_video", "music", "FIXED", True, (), ()),
            ),
        ),
    )

    assert installer.import_offline(
        bundle_id="ordinary_video", offline_root=archive.parents[1]
    ) == "APPLIED"
    assert _wait(installer, 0, "ready", "failed") == "ready"
    runtime = installer.install_root / "ordinary_video" / "cosyvoice" / "runtime"
    assert (runtime / "cosyvoice" / "__init__.py").read_text(
        encoding="utf-8"
    ) == "# pinned runtime\n"
    for relative in symlinks:
        skipped = runtime / Path(relative)
        assert not skipped.exists()
        assert not skipped.is_symlink()


def test_safe_archive_does_not_materialize_symlink_target(tmp_path: Path) -> None:
    archive = tmp_path / "malicious-target.zip"
    link = zipfile.ZipInfo("snapshot/runtime/link.py")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as payload:
        payload.writestr(link, "../../outside.py")
        payload.writestr("snapshot/runtime/worker.py", "# safe file\n")

    destination = tmp_path / "runtime"
    extracted = _extract_zip_safely(archive, destination, strip_components=1)

    assert [entry["path"] for entry in extracted] == ["runtime/worker.py"]
    assert not (destination / "runtime" / "link.py").exists()
    assert not (destination / "runtime" / "link.py").is_symlink()
    assert not (tmp_path / "outside.py").exists()


def test_safe_archive_rejects_traversal_symlink_member_without_writing(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "traversal-link.zip"
    link = zipfile.ZipInfo("snapshot/../../outside.py")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as payload:
        payload.writestr(link, "runtime/worker.py")

    with pytest.raises(VideoCapabilityError, match="VIDEO_ARCHIVE_PATH_INVALID"):
        _extract_zip_safely(archive, tmp_path / "runtime", strip_components=1)

    assert not (tmp_path / "outside.py").exists()


@pytest.mark.parametrize("source_matches", [True, False])
def test_music_bundle_install_applies_seed_patch_or_fails_closed(
    tmp_path: Path, source_matches: bool
) -> None:
    archive = tmp_path / "offline" / "sources" / "seed.zip"
    archive.parent.mkdir(parents=True)
    inference = (
        "    overlap_frame_len = 16\n"
        '    parser.add_argument("--fp16", type=str2bool, default=True)\n'
        if source_matches
        else "# drifted upstream source\n"
    )
    with zipfile.ZipFile(archive, "w") as payload:
        payload.writestr("seed-vc-pinned/inference.py", inference)
    spec = VideoFile(
        "seed-vc-code", "sources/seed.zip", archive.stat().st_size,
        hashlib.sha256(archive.read_bytes()).hexdigest(), "GPL-3.0", {}, True,
        VideoFileInstall("zip", "seed_vc/runtime", 1),
    )
    music = VideoBundle("music_video", "music", "FIXED", False, (), (spec,), True,
                        {"OLIVIA_SEED_VC_ROOT": "seed_vc/runtime"})
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest("1.0.0", (
            VideoBundle("ordinary_video", "ordinary", "FIXED", False, (), ()), music)),
    )

    assert installer.import_offline(
        bundle_id="music_video",
        offline_root=archive.parents[1],
        accept_licenses=True,
    ) == "APPLIED"
    expected = "license_review_required" if source_matches else "failed"
    assert _wait(installer, 1, expected) == expected
    installed = installer.install_root / "music_video" / "seed_vc" / "runtime"
    if source_matches:
        assert "overlap_frame_len = args.overlap_frames" in (
            installed / "inference.py"
        ).read_text(encoding="utf-8")
        assert (installed / ".olivia-overlap-frames-patched.json").is_file()
    else:
        assert not (installer.install_root / "music_video" / ".ready.json").exists()


def test_windows_runtime_picker_fails_closed_without_a_system_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(video_capability_api.os, "name", "nt")
    monkeypatch.delenv("SystemRoot", raising=False)
    monkeypatch.delenv("WINDIR", raising=False)
    monkeypatch.setattr(
        video_capability_api.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("picker process must not start"),
    )

    with pytest.raises(video_capability_api.VideoCapabilityAPIError) as captured:
        video_capability_api._select_windows_runtime_root()

    assert captured.value.code == "VIDEO_RUNTIME_PICKER_UNAVAILABLE"


def test_runtime_selection_rejects_a_folder_without_a_manifest(tmp_path: Path) -> None:
    runtime_root = (tmp_path / "runtime").resolve()
    runtime_root.mkdir()

    with pytest.raises(video_capability_api.VideoCapabilityAPIError) as captured:
        video_capability_api._runtime_manifest_sha256(runtime_root)

    assert captured.value.code == "VIDEO_RUNTIME_ROOT_INVALID"


def test_video_capability_api_selects_and_imports_runtime_root(tmp_path: Path) -> None:
    runtime_root = (tmp_path / "runtime").resolve()
    runtime_root.mkdir()
    manifest = runtime_root / "runtime-manifest.json"
    manifest.write_text('{"synthetic": true}', encoding="utf-8")
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    observed: list[tuple[Path, str]] = []

    class FakeInstaller:
        def status(self):
            return {"schema_version": "olivia.video-capability-status.v2", "status": "UNAVAILABLE", "capability": "video", "can_uninstall": False, "install_locations": [], "bundles": []}

        def import_runtime_root(self, *, runtime_root: Path, manifest_sha256: str):
            observed.append((runtime_root, manifest_sha256))
            return "APPLIED"

    async def call():
        app = web.Application()
        mount_original_client_video_capability_api(
            app,
            FakeInstaller(),
            trusted_origins=(),
            authorize_session=lambda _token: None,
            select_runtime_root=lambda: runtime_root,
        )
        async with TestClient(TestServer(app)) as client:
            status_response = await client.get(
                "/toy/capabilities/video", headers={"Origin": "http://localhost:3000"}
            )
            selected_response = await client.post(
                "/toy/capabilities/video/action",
                json={"action": "select_runtime"},
                headers={
                    "Origin": "http://localhost:3000",
                    "X-Olivia-Capability-Action": "confirmed",
                    "X-Olivia-Setup-Session": "session",
                },
            )
            selected = await selected_response.json()
            response = await client.post(
                "/toy/capabilities/video/action",
                json={
                    "action": "import_runtime",
                    "runtime_root": selected["runtime_root"],
                    "manifest_sha256": selected["manifest_sha256"],
                },
                headers={
                    "Origin": "http://localhost:3000",
                    "X-Olivia-Capability-Action": "confirmed",
                    "X-Olivia-Setup-Session": "session",
                },
            )
            return (
                await status_response.json(),
                selected_response.status,
                selected,
                response.status,
                await response.json(),
            )

    status_payload, selected_status, selected, status, payload = asyncio.run(call())
    for name, document in (
        ("status", status_payload),
        ("action", {"action": "pause"}),
        ("action", {"action": "uninstall"}),
        ("action", {"action": "select_runtime"}),
        (
            "action",
            {
                "action": "import_runtime",
                "runtime_root": str(runtime_root),
                "manifest_sha256": "a" * 64,
            },
        ),
    ):
        schema = json.loads(Path(f"contracts/video_capability_{name}.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(document)
    assert selected_status == 200
    assert selected == {
        "status": "SELECTED",
        "runtime_root": str(runtime_root),
        "manifest_sha256": manifest_sha256,
    }
    assert status == 200
    assert payload == {"status": "APPLIED"}
    assert observed == [(runtime_root, manifest_sha256)]


def test_video_capability_api_uninstalls_the_managed_capability() -> None:
    observed: list[str] = []

    class FakeInstaller:
        def status(self):
            return {
                "schema_version": "olivia.video-capability-status.v2",
                "status": "UNAVAILABLE",
                "capability": "video",
                "can_uninstall": True,
                "install_locations": [],
                "bundles": [],
            }

        def uninstall(self):
            observed.append("uninstall")
            return "APPLIED"

    async def call():
        app = web.Application()
        mount_original_client_video_capability_api(
            app,
            FakeInstaller(),
            trusted_origins=(),
            authorize_session=lambda _token: None,
        )
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/toy/capabilities/video/action",
                json={"action": "uninstall"},
                headers={
                    "Origin": "http://localhost:3000",
                    "X-Olivia-Capability-Action": "confirmed",
                    "X-Olivia-Setup-Session": "session",
                },
            )
            return response.status, await response.json()

    status, payload = asyncio.run(call())

    assert status == 200
    assert payload == {"status": "APPLIED"}
    assert observed == ["uninstall"]


def test_video_capability_api_selects_and_imports_runtime_archive(tmp_path: Path) -> None:
    archive = (tmp_path / "Olivia-video-runtime-fixture.zip").resolve()
    with zipfile.ZipFile(archive, "w") as payload:
        payload.writestr("runtime-manifest.json", "{}")
    observed: list[Path] = []

    class FakeInstaller:
        def status(self):
            return {"schema_version": "olivia.video-capability-status.v2", "status": "UNAVAILABLE", "capability": "video", "install_locations": [], "bundles": []}

        def start_runtime_archive_import(self, *, runtime_archive: Path):
            observed.append(runtime_archive)
            return "APPLIED"

        def import_runtime_archive(self, *, runtime_archive: Path):
            pytest.fail("the HTTP request must not run the archive import inline")

    async def call():
        app = web.Application()
        mount_original_client_video_capability_api(
            app,
            FakeInstaller(),
            trusted_origins=(),
            authorize_session=lambda _token: None,
            select_runtime_archive=lambda: archive,
        )
        headers = {
            "Origin": "http://localhost:3000",
            "X-Olivia-Capability-Action": "confirmed",
            "X-Olivia-Setup-Session": "session",
        }
        async with TestClient(TestServer(app)) as client:
            selected_response = await client.post(
                "/toy/capabilities/video/action",
                json={"action": "select_runtime_archive"},
                headers=headers,
            )
            selected = await selected_response.json()
            imported_response = await client.post(
                "/toy/capabilities/video/action",
                json={
                    "action": "import_runtime_archive",
                    "runtime_archive": selected["runtime_archive"],
                },
                headers=headers,
            )
            return selected, imported_response.status, await imported_response.json()

    selected, status, imported = asyncio.run(call())
    assert selected == {"status": "SELECTED", "runtime_archive": str(archive)}
    assert status == 200
    assert imported == {"status": "APPLIED"}
    assert observed == [archive]
    schema = json.loads(Path("contracts/video_capability_action.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate({"action": "select_runtime_archive"})
    Draft202012Validator(schema).validate(
        {"action": "import_runtime_archive", "runtime_archive": str(archive)}
    )


def test_video_capability_api_single_offline_import_detects_runtime_archive(
    tmp_path: Path,
) -> None:
    archive = (tmp_path / "Olivia-video-runtime-fixture.zip").resolve()
    with zipfile.ZipFile(archive, "w") as payload:
        payload.writestr("runtime-manifest.json", "{}")
    observed: list[Path] = []

    class FakeInstaller:
        def status(self):
            return {
                "schema_version": "olivia.video-capability-status.v1",
                "status": "UNAVAILABLE",
                "capability": "video",
                "install_locations": [],
                "bundles": [],
            }

        def start_runtime_archive_import(self, *, runtime_archive: Path):
            observed.append(runtime_archive)
            return "APPLIED"

        def import_runtime_archive(self, *, runtime_archive: Path):
            pytest.fail("the HTTP request must not run the archive import inline")

        def import_offline(self, **_kwargs):
            raise AssertionError("runtime archives must not be treated as component ZIPs")

    async def call():
        app = web.Application()
        mount_original_client_video_capability_api(
            app,
            FakeInstaller(),
            trusted_origins=(),
            authorize_session=lambda _token: None,
            select_offline_archive=lambda: archive,
        )
        headers = {
            "Origin": "http://localhost:3000",
            "X-Olivia-Capability-Action": "confirmed",
            "X-Olivia-Setup-Session": "session",
        }
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/toy/capabilities/video/action",
                json={"action": "import_offline"},
                headers=headers,
            )
            return response.status, await response.json()

    status, payload = asyncio.run(call())
    assert status == 200
    assert payload == {"status": "APPLIED"}
    assert observed == [archive]


def test_video_capability_api_selects_and_imports_one_offline_zip_for_both_bundles(
    tmp_path: Path,
) -> None:
    archive = (tmp_path / "Olivia-video-offline.zip").resolve()
    with zipfile.ZipFile(archive, "w") as payload:
        payload.writestr("fixture.txt", "fixture")
    observed: list[tuple[str, Path, bool]] = []

    class FakeInstaller:
        def status(self):
            return {
                "schema_version": "olivia.video-capability-status.v2",
                "status": "UNAVAILABLE",
                "capability": "video",
                "install_locations": [],
                "bundles": [],
            }

        def import_offline(
            self,
            *,
            bundle_id: str,
            offline_root: Path,
            source_mode: str = "official",
            accept_licenses: bool = False,
        ):
            observed.append((bundle_id, offline_root, accept_licenses))
            return "APPLIED"

    async def call():
        app = web.Application()
        mount_original_client_video_capability_api(
            app,
            FakeInstaller(),
            trusted_origins=(),
            authorize_session=lambda _token: None,
            select_offline_archive=lambda: archive,
        )
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/toy/capabilities/video/action",
                json={"action": "import_offline"},
                headers={
                    "Origin": "http://localhost:3000",
                    "X-Olivia-Capability-Action": "confirmed",
                    "X-Olivia-Setup-Session": "session",
                },
            )
            return response.status, await response.json()

    status, payload = asyncio.run(call())
    schema = json.loads(
        Path("contracts/video_capability_action.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate({"action": "import_offline"})
    assert status == 200
    assert payload == {"status": "APPLIED"}
    assert observed == [
        ("ordinary_video", archive, False),
        ("music_video", archive, True),
    ]


def test_video_capability_offline_import_failure_uses_the_stable_api_error(
    tmp_path: Path,
) -> None:
    archive = (tmp_path / "Olivia-video-offline.zip").resolve()
    with zipfile.ZipFile(archive, "w") as payload:
        payload.writestr("fixture.txt", "fixture")

    class FailingInstaller:
        def import_offline(self, **_kwargs):
            raise RuntimeError("private failure detail")

    async def call():
        app = web.Application()
        mount_original_client_video_capability_api(
            app,
            FailingInstaller(),
            trusted_origins=(),
            authorize_session=lambda _token: None,
            select_offline_archive=lambda: archive,
        )
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/toy/capabilities/video/action",
                json={"action": "import_offline"},
                headers={
                    "Origin": "http://localhost:3000",
                    "X-Olivia-Capability-Action": "confirmed",
                    "X-Olivia-Setup-Session": "session",
                },
            )
            return response.status, await response.json()

    status, payload = asyncio.run(call())
    assert status == 503
    assert payload == {
        "status": "FAILED",
        "error_code": "VIDEO_CAPABILITY_ACTION_UNAVAILABLE",
    }


def test_video_capability_status_remains_available_during_runtime_import(
    tmp_path: Path,
) -> None:
    runtime_root = (tmp_path / "runtime").resolve()
    runtime_root.mkdir()
    manifest = runtime_root / "runtime-manifest.json"
    manifest.write_text('{"synthetic": true}', encoding="utf-8")
    started = threading.Event()
    release = threading.Event()

    class FakeInstaller:
        def status(self):
            return {
                "schema_version": "olivia.video-capability-status.v2",
                "status": "UNAVAILABLE",
                "capability": "video",
                "install_locations": [],
                "bundles": [],
                "runtime_import": {
                    "state": "checking",
                    "checked_bytes": 1,
                    "total_bytes": 2,
                },
            }

        def import_runtime_root(self, *, runtime_root: Path, manifest_sha256: str):
            started.set()
            assert release.wait(2)
            return "APPLIED"

    async def call() -> dict[str, object]:
        app = web.Application()
        mount_original_client_video_capability_api(
            app,
            FakeInstaller(),
            trusted_origins=(),
            authorize_session=lambda _token: None,
        )
        async with TestClient(TestServer(app)) as client:
            action = asyncio.create_task(
                client.post(
                    "/toy/capabilities/video/action",
                    json={
                        "action": "import_runtime",
                        "runtime_root": str(runtime_root),
                        "manifest_sha256": "a" * 64,
                    },
                    headers={
                        "Origin": "http://localhost:3000",
                        "X-Olivia-Capability-Action": "confirmed",
                        "X-Olivia-Setup-Session": "session",
                    },
                )
            )
            assert await asyncio.to_thread(started.wait, 1)
            try:
                response = await asyncio.wait_for(
                    client.get(
                        "/toy/capabilities/video",
                        headers={"Origin": "http://localhost:3000"},
                    ),
                    timeout=0.5,
                )
                return await response.json()
            finally:
                release.set()
                await action

    payload = asyncio.run(call())
    assert payload["runtime_import"] == {
        "state": "checking",
        "checked_bytes": 1,
        "total_bytes": 2,
    }


def test_download_progress_updates_before_a_large_file_finishes(tmp_path: Path) -> None:
    first_chunk = threading.Event()
    release = threading.Event()
    content = b"abcdef"

    class SlowResponse:
        status = 200

        def __init__(self) -> None:
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size: int) -> bytes:
            self.calls += 1
            if self.calls == 1:
                first_chunk.set()
                return content[:3]
            if self.calls == 2:
                assert release.wait(1)
                return content[3:]
            return b""

    spec = VideoFile(
        "fixture",
        "models/fixture.bin",
        len(content),
        hashlib.sha256(content).hexdigest(),
        "MIT",
        {"official": "https://example.invalid/fixture.bin"},
    )
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest(
            "1.0.0",
            (
                VideoBundle("ordinary_video", "video", "FIXED", False, (), (spec,)),
                VideoBundle("music_video", "music", "FIXED", False, (), ()),
            ),
        ),
        opener=lambda *_args, **_kwargs: SlowResponse(),
    )

    assert installer.start(bundle_id="ordinary_video", source_mode="official") == "APPLIED"
    assert first_chunk.wait(1)
    assert installer.status()["bundles"][0]["downloaded_bytes"] == 3
    release.set()
    assert _wait(installer, 0, "ready", "failed") == "ready"


def test_source_fallback_restarts_instead_of_resuming_bytes_from_another_mirror(
    tmp_path: Path,
) -> None:
    content = b"abc"
    requests = []

    class Response:
        def __init__(self, body: bytes, status: int) -> None:
            self.body = body
            self.status = status
            self.sent = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size: int) -> bytes:
            if self.sent:
                return b""
            self.sent = True
            return self.body

    def opener(request, **_kwargs):
        requests.append(request)
        if "domestic.invalid" in request.full_url:
            return Response(b"wrong", 200)
        return Response(content, 206 if request.get_header("Range") else 200)

    spec = VideoFile(
        "fixture",
        "models/fixture.bin",
        len(content),
        hashlib.sha256(content).hexdigest(),
        "MIT",
        {
            "domestic": "https://domestic.invalid/fixture.bin",
            "official": "https://official.invalid/fixture.bin",
        },
    )
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest(
            "1.0.0",
            (
                VideoBundle("ordinary_video", "video", "FIXED", False, (), (spec,)),
                VideoBundle("music_video", "music", "FIXED", False, (), ()),
            ),
        ),
        opener=opener,
    )

    assert installer.start(bundle_id="ordinary_video", source_mode="auto") == "APPLIED"
    assert _wait(installer, 0, "ready", "failed") == "ready"
    assert len(requests) == 2
    assert requests[1].get_header("Range") is None
