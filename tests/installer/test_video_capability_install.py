from __future__ import annotations

import asyncio
import json
import hashlib
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import threading
import time
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
)
import original_client_video_capability_api as video_capability_api
import original_client_server
from original_client_video_capability_api import mount_original_client_video_capability_api
from runtime.media.music_reply import video_reply_source_url


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


def test_repository_bom_keeps_fixed_cosyvoice_and_license_boundaries() -> None:
    manifest_path = Path("installer/video-capability-manifest.json")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = load_video_manifest(manifest_path)
    ordinary, music = manifest.bundles
    assert len([item for item in ordinary.files if item.identifier.startswith("cosy-")]) == 20
    assert sum(item.size_bytes for item in ordinary.files if item.identifier.startswith("cosy-")) == 9747516745
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
        "OLIVIA_COSYVOICE_PYTHON": "cosyvoice/runtime/python/python.exe",
        "OLIVIA_FFMPEG_EXE": "ffmpeg/runtime/bin/ffmpeg.exe",
        "OLIVIA_LATENTSYNC_PYTHON": "latentsync/runtime/python/python.exe",
        "OLIVIA_LATENTSYNC_ROOT": "latentsync/runtime",
        "OLIVIA_TTS_CONFIG": "cosyvoice/config/tts_local.json",
    }
    assert {
        patch.identifier: (patch.target_path, patch.sha256)
        for patch in ordinary.runtime_patches
    } == {
        "cosyvoice-windows-audio": (
            "cosyvoice/runtime/cosyvoice/utils/file_utils.py",
            "019a0f163e397186c0a6d26c5eeaed1c56ba88462200662950c276ceb50c2d27",
        ),
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
    minimax_files = [item for item in music.files if item.identifier.startswith("minimax-")]
    assert minimax_files
    assert all(
        "/resolve/fbc3502b5d2ca0049348ee28b632f270b35e193a/"
        in item.sources["domestic"]
        for item in minimax_files
    )

    schema = json.loads(Path("contracts/video_capability_manifest.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(payload)


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
        "OLIVIA_COSYVOICE_PYTHON": "cosyvoice/python/python.exe",
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
                "OLIVIA_COSYVOICE_ROOT": "cosyvoice/runtime", "OLIVIA_LATENTSYNC_ROOT": "latentsync/runtime"}),
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
        "ordinary_video/cosyvoice/runtime/cosyvoice/cli/cosyvoice.py",
        "ordinary_video/cosyvoice/runtime/LICENSE", "ordinary_video/ffmpeg/runtime/bin/ffmpeg.exe",
        "music_video/roformer/models/MelBandRoformer.ckpt",
        "music_video/roformer/runtime/src/mel_band_roformer/configs/config_vocals_mel_band_roformer.yaml",
        "shared/linli-reference.wav"):
        target = install_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fixture")
    for relative in ("ordinary_video/cosyvoice/model", "ordinary_video/latentsync/runtime",
                     "music_video/minimax/runtime"):
        (install_root / relative).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(video_capability_install, "_ready_marker_matches", lambda *_: True)
    monkeypatch.setattr(video_capability_install, "_size_matches", lambda *_: True)
    monkeypatch.setattr(video_capability_install, "_runtime_environment_is_portable", lambda *_: True)

    def readiness(environment: Mapping[str, str]) -> dict[str, object]:
        settings = json.loads(Path(environment["OLIVIA_TTS_CONFIG"]).read_text(encoding="utf-8"))
        ready = Path(settings["settings"]["runtime_root"]) == (install_root / "ordinary_video/cosyvoice/runtime").resolve()
        return {"ordinary_missing_dependencies": [] if ready else ["cosyvoice"], "music_ready": ready}

    installer = VideoCapabilityInstaller(
        data_root=data_root, manifest=manifest, readiness_probe=readiness)
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
    assert installer.status()["status"] == "READY"


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
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest("1.0", ()),
        readiness_probe=lambda _environment: {},
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
            return {"schema_version": "olivia.video-capability-status.v2", "status": "UNAVAILABLE", "capability": "video", "install_locations": [], "bundles": []}

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
