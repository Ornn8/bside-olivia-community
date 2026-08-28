from __future__ import annotations

import asyncio
import json
import hashlib
from pathlib import Path
import shutil
import time
import zipfile

import pytest
from jsonschema import Draft202012Validator

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from video_capability_install import (
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


def test_repository_bom_keeps_fixed_cosyvoice_and_license_boundaries() -> None:
    manifest_path = Path("installer/video-capability-manifest.json")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = load_video_manifest(manifest_path)
    ordinary, music = manifest.bundles
    assert len([item for item in ordinary.files if item.identifier.startswith("cosy-")]) == 20
    assert sum(item.size_bytes for item in ordinary.files if item.identifier.startswith("cosy-")) == 9747516745
    assert music.license_review_required is True
    assert ordinary.runtime_environment == {
        "OLIVIA_FFMPEG_EXE": "ffmpeg/runtime/bin/ffmpeg.exe",
        "OLIVIA_LATENTSYNC_ROOT": "latentsync/runtime",
    }
    seed_source = next(item for item in music.files if item.identifier == "seed-vc-code")
    assert seed_source.install is not None
    assert seed_source.install.destination == "seed_vc/runtime"
    provenance = payload["provenance"]
    assert provenance["roformer"]["license_review_required"] is True
    assert provenance["seed_vc"]["overlap_frames_patch"] == "installer/seed-vc-overlap-frames.patch"
    assert provenance["seed_vc"]["overlap_frames_patch_sha256"] == hashlib.sha256(
        Path("installer/seed-vc-overlap-frames.patch").read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()
    assert provenance["seed_vc"]["weights_redistributable"] is False

    schema = json.loads(Path("contracts/video_capability_manifest.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(payload)


class _Response:
    status = 206

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self, _size: int) -> bytes:
        payload, self._payload = self._payload, b""
        return payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _wait(installer: VideoCapabilityInstaller, index: int, *states: str) -> str:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = installer.status()["bundles"][index]["state"]
        if state in states:
            return state
        time.sleep(0.01)
    return installer.status()["bundles"][index]["state"]


def test_install_resumes_part_and_promotes_only_after_hash_verification(tmp_path: Path) -> None:
    payload = b"verified video payload"
    spec = VideoFile(
        "fixture",
        "runtime/fixture.bin",
        len(payload),
        hashlib.sha256(payload).hexdigest(),
        "MIT",
        {"domestic": "https://mirror.example/fixture"},
    )
    bundle = VideoBundle("ordinary_video", "普通视频", "FIXED", False, (), (spec,))
    installer = VideoCapabilityInstaller(
        data_root=tmp_path / "data",
        manifest=VideoManifest("1.0.0", (bundle, VideoBundle("music_video", "音乐视频", "FIXED", False, (), ()))),
        opener=lambda request, timeout: (
            assert_range(request), _Response(payload[8:])
        )[1],
    )
    part = installer.install_root / ".staging" / "ordinary_video" / "runtime" / "fixture.bin.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(payload[:8])
    assert installer.start(bundle_id="ordinary_video") == "APPLIED"
    assert _wait(installer, 0, "ready", "failed") == "ready"
    assert (installer.install_root / "ordinary_video" / "runtime" / "fixture.bin").read_bytes() == payload
    assert (installer.install_root / "ordinary_video" / ".ready.json").is_file()


def test_failed_runtime_profile_write_restores_previous_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"replacement bundle"
    spec = VideoFile(
        "fixture",
        "runtime/fixture.bin",
        len(payload),
        hashlib.sha256(payload).hexdigest(),
        "MIT",
        {},
    )
    bundle = VideoBundle("ordinary_video", "ordinary", "FIXED", False, (), (spec,))
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest(
            "1.0.0",
            (bundle, VideoBundle("music_video", "music", "FIXED", False, (), ())),
        ),
    )
    previous = installer.install_root / "ordinary_video"
    previous.mkdir(parents=True)
    (previous / "previous.txt").write_text("preserve me", encoding="utf-8")
    offline = tmp_path / "offline" / "runtime" / "fixture.bin"
    offline.parent.mkdir(parents=True)
    offline.write_bytes(payload)

    def fail_profile_write() -> None:
        raise OSError("synthetic profile failure")

    monkeypatch.setattr(installer, "_write_runtime_environment", fail_profile_write)
    assert installer.import_offline(
        bundle_id="ordinary_video", offline_root=offline.parents[1]
    ) == "APPLIED"
    assert _wait(installer, 0, "failed") == "failed"
    assert (previous / "previous.txt").read_text(encoding="utf-8") == "preserve me"
    assert not (previous / "runtime" / "fixture.bin").exists()


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
        "ordinary_video", "ordinary", "FIXED", True, (), (spec,), False,
        {"OLIVIA_LATENTSYNC_ROOT": "latentsync/runtime"},
    )
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest("1.0.0", (ordinary, VideoBundle("music_video", "music", "FIXED", True, (), ()))),
    )

    assert installer.import_offline(
        bundle_id="ordinary_video", offline_root=archive.parents[1]
    ) == "APPLIED"
    assert _wait(installer, 0, "ready", "failed") == "ready"
    runtime = installer.install_root / "ordinary_video" / "latentsync" / "runtime"
    assert (runtime / "scripts" / "inference.py").read_text(encoding="utf-8") == "# pinned runtime\n"
    assert load_video_runtime_environment(installer.data_root) == {
        "OLIVIA_LATENTSYNC_ROOT": str(runtime.resolve())
    }
    profile = installer.install_root / "runtime-environment.json"
    profile_payload = profile.read_text(encoding="utf-8")
    profile.unlink()
    assert installer.status()["bundles"][0]["state"] != "ready"
    profile.write_text(profile_payload, encoding="utf-8")
    assert installer.status()["bundles"][0]["state"] == "ready"
    shutil.rmtree(runtime)
    assert installer.status()["bundles"][0]["state"] != "ready"


@pytest.mark.parametrize(("names", "reason"), [
    (("Runtime/inference.py", "runtime/inference.py"), "VIDEO_ARCHIVE_DUPLICATE_PATH"),
    (("asset:stream",), "VIDEO_ARCHIVE_PATH_INVALID"),
    (("CON/file.txt",), "VIDEO_ARCHIVE_PATH_INVALID"),
    (("trailing./file.txt",), "VIDEO_ARCHIVE_PATH_INVALID"),
])
def test_safe_archive_rejects_windows_unsafe_paths(tmp_path: Path, names, reason) -> None:
    archive = tmp_path / "collision.zip"
    with zipfile.ZipFile(archive, "w") as payload:
        for name in names:
            payload.writestr(name, "fixture")

    with pytest.raises(VideoCapabilityError, match=reason):
        _extract_zip_safely(archive, tmp_path / "runtime", strip_components=0)


def test_real_client_prerequisites_prevent_public_bundle_ready_claim(
    tmp_path: Path,
) -> None:
    ordinary = VideoBundle(
        "ordinary_video",
        "ordinary",
        "FIXED",
        False,
        ("cosyvoice", "latentsync", "ffmpeg", "official_video_assets"),
        (),
    )
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest(
            "1.0.0",
            (ordinary, VideoBundle("music_video", "music", "FIXED", False, (), ())),
        ),
    )

    assert installer.start(bundle_id="ordinary_video") == "APPLIED"
    assert _wait(installer, 0, "prerequisites_required", "failed") == "prerequisites_required"
    assert installer.status()["bundles"][0]["reason_code"] == (
        "VIDEO_NATIVE_PATH_SELECTION_UNAVAILABLE"
    )
    assert installer.status()["status"] == "UNAVAILABLE"


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
        "seed-vc-code",
        "sources/seed.zip",
        archive.stat().st_size,
        hashlib.sha256(archive.read_bytes()).hexdigest(),
        "GPL-3.0",
        {},
        True,
        VideoFileInstall("zip", "seed_vc/runtime", 1),
    )
    music = VideoBundle(
        "music_video",
        "music",
        "FIXED",
        False,
        (),
        (spec,),
        True,
        {"OLIVIA_SEED_VC_ROOT": "seed_vc/runtime"},
    )
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest(
            "1.0.0",
            (VideoBundle("ordinary_video", "ordinary", "FIXED", False, (), ()), music),
        ),
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


def assert_range(request) -> None:
    assert request.headers.get("Range") == "bytes=8-"


def test_video_capability_api_requires_confirmation_and_exposes_bundle_status() -> None:
    class FakeInstaller:
        def status(self):
            return {"status": "UNAVAILABLE", "capability": "video", "bundles": []}

        def start(self, **_kwargs):
            return "APPLIED"

        def pause(self):
            return "NOOP"

        def resume(self, **_kwargs):
            return "APPLIED"

        def retry(self, **_kwargs):
            return "APPLIED"

    async def calls():
        app = web.Application()
        mount_original_client_video_capability_api(
            app,
            FakeInstaller(),
            trusted_origins=(),
            authorize_session=lambda token: None if token == "session" else (_ for _ in ()).throw(ValueError()),
        )
        async with TestClient(TestServer(app)) as client:
            status = await client.get("/toy/capabilities/video", headers={"Origin": "http://localhost:3000"})
            rejected = await client.post("/toy/capabilities/video/action", json={"action": "pause"}, headers={"Origin": "http://localhost:3000", "X-Olivia-Setup-Session": "session"})
            applied = await client.post("/toy/capabilities/video/action", json={"action": "pause"}, headers={"Origin": "http://localhost:3000", "X-Olivia-Capability-Action": "confirmed", "X-Olivia-Setup-Session": "session"})
            return status.status, await status.json(), rejected.status, applied.status, await applied.json()

    status_code, status, rejected_code, applied_code, applied = asyncio.run(calls())
    assert status_code == 200 and status["capability"] == "video"
    assert rejected_code == 403
    assert applied_code == 200 and applied["status"] == "NOOP"


def test_video_capability_api_reports_native_path_selection_unavailable() -> None:
    class FakeInstaller:
        def status(self):
            return {"status": "UNAVAILABLE", "capability": "video", "bundles": []}

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
            response = await client.post(
                "/toy/capabilities/video/action",
                json={"action": "import_offline", "bundle_id": "ordinary_video"},
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
        "error_code": "VIDEO_NATIVE_PATH_SELECTION_UNAVAILABLE",
    }
