from __future__ import annotations

import asyncio
import json
import hashlib
from pathlib import Path
import shutil
import threading
import time
import zipfile

import pytest
from jsonschema import Draft202012Validator

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from video_capability_install import (
    apply_runtime_text_patch,
    _extract_zip_safely,
    load_video_manifest,
    load_video_runtime_environment,
    VideoCapabilityError,
)
from video_capability_install import (
    VideoBundle,
    VideoCapabilityInstaller,
    VideoFile,
    VideoFileInstall,
    VideoManifest,
)
from original_client_video_capability_api import mount_original_client_video_capability_api


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
        "domestic": "https://hf-mirror.com/ByteDance/LatentSync-1.5/resolve/32a20d29aead0498e3e885e90dbbe8027da1b61b/latentsync_unet.pt",
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
    assert ordinary.runtime_environment == {
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
    }
    music_file_ids = {item.identifier for item in music.files}
    assert "seed-vc-code" not in music_file_ids
    assert "demucs-htdemucs6s" not in music_file_ids
    assert {"roformer-code", "roformer-checkpoint"} <= music_file_ids
    assert music.runtime_environment == {
        "OLIVIA_MINIMAX_COMFY_PYTHON": "minimax/runtime/python/python.exe",
        "OLIVIA_MINIMAX_COMFY_ROOT": "minimax/runtime",
        "OLIVIA_MINIMAX_WORKER": "minimax/runtime/tools/minimax_music3_worker.py",
        "OLIVIA_ROFORMER_EXE": "roformer/runtime/python/Scripts/melband-roformer-infer.exe",
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
    assert installer.status()["bundles"][0]["state"] == "missing"
    profile.write_text(profile_payload, encoding="utf-8")
    assert installer.status()["bundles"][0]["state"] == "prerequisites_required"
    shutil.rmtree(runtime)
    assert installer.status()["bundles"][0]["state"] != "ready"


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
        (root / ".ready.json").write_text("{}", encoding="utf-8")
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
    assert observed[-1]["OLIVIA_LOCAL_DATA_ROOT"] == str(data_root)
    assert observed[-1]["OLIVIA_FFMPEG_EXE"] == str(ffmpeg)


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


def test_video_capability_api_reports_native_path_selection_unavailable() -> None:
    class FakeInstaller:
        def status(self):
            return {"schema_version": "olivia.video-capability-status.v1", "status": "UNAVAILABLE", "capability": "video", "install_locations": [], "bundles": []}

        def __getattr__(self, _name):
            raise AssertionError("path selection must fail before installer dispatch")

    async def call():
        app = web.Application()
        mount_original_client_video_capability_api(
            app,
            FakeInstaller(),
            trusted_origins=(),
            authorize_session=lambda _token: None,
        )
        async with TestClient(TestServer(app)) as client:
            status_response = await client.get(
                "/toy/capabilities/video", headers={"Origin": "http://localhost:3000"}
            )
            response = await client.post(
                "/toy/capabilities/video/action",
                json={"action": "import_offline", "bundle_id": "ordinary_video"},
                headers={
                    "Origin": "http://localhost:3000",
                    "X-Olivia-Capability-Action": "confirmed",
                    "X-Olivia-Setup-Session": "session",
                },
            )
            return await status_response.json(), response.status, await response.json()

    status_payload, status, payload = asyncio.run(call())
    for name, document in (("status", status_payload), ("action", {"action": "pause"})):
        schema = json.loads(Path(f"contracts/video_capability_{name}.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(document)
    assert status == 503
    assert payload == {
        "status": "FAILED",
        "error_code": "VIDEO_NATIVE_PATH_SELECTION_UNAVAILABLE",
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
