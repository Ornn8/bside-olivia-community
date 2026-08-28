from __future__ import annotations

import asyncio
import json
import hashlib
from pathlib import Path
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from video_capability_install import load_video_manifest
from video_capability_install import VideoBundle, VideoCapabilityInstaller, VideoFile, VideoManifest
from original_client_video_capability_api import mount_original_client_video_capability_api


def _manifest(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": "olivia.video-capability-bom.v1",
                "version": "1.0.0",
                "bundles": [
                    {
                        "id": "ordinary_video",
                        "label": "普通视频",
                        "status": "FIXED",
                        "requires_gpu": True,
                        "dependencies": ["cosyvoice", "latentsync", "ffmpeg", "official_video_assets"],
                        "files": [
                            {
                                "id": "fixture",
                                "path": "fixture.bin",
                                "size_bytes": 4,
                                "sha256": "" + "0" * 64,
                                "license": "MIT",
                                "sources": {"domestic": "https://mirror.example/fixture", "official": "https://official.example/fixture"},
                            }
                        ],
                    },
                    {
                        "id": "music_video",
                        "label": "音乐视频扩展",
                        "status": "FIXED",
                        "requires_gpu": True,
                        "dependencies": ["ordinary_video", "minimax_music3", "roformer", "seed_vc", "demucs"],
                        "files": [],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_load_video_manifest_exposes_separate_bundles(tmp_path: Path) -> None:
    manifest = load_video_manifest(_manifest(tmp_path / "video.json"))
    assert [bundle.id for bundle in manifest.bundles] == ["ordinary_video", "music_video"]
    assert manifest.bundles[0].dependencies == (
        "cosyvoice",
        "latentsync",
        "ffmpeg",
        "official_video_assets",
    )


def test_repository_bom_keeps_fixed_cosyvoice_and_license_boundaries() -> None:
    manifest = load_video_manifest(Path("installer/video-capability-manifest.json"))
    ordinary, music = manifest.bundles
    assert len([item for item in ordinary.files if item.identifier.startswith("cosy-")]) == 20
    assert sum(item.size_bytes for item in ordinary.files if item.identifier.startswith("cosy-")) == 9747516745
    assert music.license_review_required is True
    provenance = json.loads(Path("installer/video-capability-manifest.json").read_text(encoding="utf-8"))["provenance"]
    assert provenance["roformer"]["license_review_required"] is True
    assert provenance["seed_vc"]["overlap_frames_patch"] is True


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
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and installer.status()["bundles"][0]["state"] not in {"ready", "failed"}:
        time.sleep(0.01)
    status = installer.status()
    assert status["bundles"][0]["state"] == "ready"
    assert (installer.install_root / "ordinary_video" / "runtime" / "fixture.bin").read_bytes() == payload
    assert (installer.install_root / "ordinary_video" / ".ready.json").is_file()


def test_offline_directory_import_uses_the_same_verification_and_staging(tmp_path: Path) -> None:
    payload = b"offline bundle"
    spec = VideoFile("fixture", "runtime/fixture.bin", len(payload), hashlib.sha256(payload).hexdigest(), "MIT", {})
    bundle = VideoBundle("ordinary_video", "普通视频", "FIXED", False, (), (spec,))
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest("1.0.0", (bundle, VideoBundle("music_video", "音乐视频", "FIXED", False, (), ()))),
    )
    offline = tmp_path / "offline" / "runtime" / "fixture.bin"
    offline.parent.mkdir(parents=True)
    offline.write_bytes(payload)
    assert installer.import_offline(bundle_id="ordinary_video", offline_root=offline.parents[1]) == "APPLIED"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and installer.status()["bundles"][0]["state"] not in {"ready", "failed"}:
        time.sleep(0.01)
    assert installer.status()["bundles"][0]["state"] == "ready"


def test_private_assets_are_copied_only_from_explicitly_configured_files(tmp_path: Path) -> None:
    bundle = VideoBundle("ordinary_video", "普通视频", "FIXED", False, (), ())
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest("1.0.0", (bundle, VideoBundle("music_video", "音乐视频", "FIXED", False, (), ()))),
    )
    sources = {}
    for key, name in (
        ("OLIVIA_ORDINARY_ACTION_BASE", "action.mp4"),
        ("OLIVIA_OFFICIAL_REPLY_REFERENCE", "reply.mp4"),
        ("OLIVIA_MUSIC_PERFORMANCE_BASE", "performance.mp4"),
    ):
        path = tmp_path / name
        path.write_bytes(name.encode())
        sources[key] = str(path)
    assert installer.import_configured_assets(sources) == "APPLIED"
    private_root = installer.install_root / "private-assets"
    assert sorted(path.name for path in private_root.iterdir() if path.is_file() and path.name != ".ready.json") == [
        "music_performance_base.mp4",
        "official_reply_reference.mp4",
        "ordinary_action_base.mp4",
    ]


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

        def import_configured_assets(self, **_kwargs):
            return "APPLIED"

    async def calls():
        app = web.Application()
        mount_original_client_video_capability_api(
            app,
            FakeInstaller(),
            trusted_origins=(),
            authorize_session=lambda token: None if token == "session" else (_ for _ in ()).throw(ValueError()),
            environment={},
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
