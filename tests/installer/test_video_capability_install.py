from __future__ import annotations

import asyncio
import json
import hashlib
from pathlib import Path
import shutil
import time
import tomllib
import zipfile

import pytest
from jsonschema import Draft202012Validator

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from video_capability_install import (
    _extract_zip_safely,
    apply_seed_vc_overlap_frames_patch,
    load_video_manifest,
    load_video_runtime_environment,
    VideoCapabilityError,
)
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
    assert ordinary.runtime_environment == {
        "OLIVIA_FFMPEG_EXE": "ffmpeg/runtime/bin/ffmpeg.exe",
        "OLIVIA_LATENTSYNC_ROOT": "latentsync/runtime",
    }
    seed_source = next(item for item in music.files if item.identifier == "seed-vc-code")
    assert seed_source.install is not None
    assert seed_source.install.destination == "seed_vc/runtime"
    provenance = json.loads(Path("installer/video-capability-manifest.json").read_text(encoding="utf-8"))["provenance"]
    assert provenance["roformer"]["license_review_required"] is True
    assert provenance["seed_vc"]["overlap_frames_patch"] == "installer/seed-vc-overlap-frames.patch"
    assert provenance["seed_vc"]["overlap_frames_patch_sha256"] == hashlib.sha256(
        Path("installer/seed-vc-overlap-frames.patch").read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()
    assert provenance["seed_vc"]["weights_redistributable"] is False

    schema = json.loads(
        Path("contracts/video_capability_manifest.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(
        json.loads(
            Path("installer/video-capability-manifest.json").read_text(
                encoding="utf-8"
            )
        )
    )


def test_python_distribution_contains_video_installer_api_and_manifest() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert "original_client_video_capability_api" in project["tool"]["setuptools"]["py-modules"]
    assert set(project["tool"]["setuptools"]["package-data"]["installer"]) >= {
        "video-capability-manifest.json",
        "seed-vc-overlap-frames.patch",
    }


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
    deadline = time.monotonic() + 2
    while (
        time.monotonic() < deadline
        and installer.status()["bundles"][0]["state"] != "failed"
    ):
        time.sleep(0.01)

    assert installer.status()["bundles"][0]["state"] == "failed"
    assert (previous / "previous.txt").read_text(encoding="utf-8") == "preserve me"
    assert not (previous / "runtime" / "fixture.bin").exists()


def test_bundle_ready_requires_safe_archive_assembly_and_persisted_runtime_wiring(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "offline" / "sources" / "runtime.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as payload:
        payload.writestr("upstream/scripts/inference.py", "# pinned runtime\n")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest_path = tmp_path / "video.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "olivia.video-capability-bom.v1",
                "version": "1.0.0",
                "bundles": [
                    {
                        "id": "ordinary_video",
                        "label": "ordinary",
                        "status": "FIXED",
                        "requires_gpu": True,
                        "dependencies": [],
                        "runtime_environment": {
                            "OLIVIA_LATENTSYNC_ROOT": "latentsync/runtime"
                        },
                        "files": [
                            {
                                "id": "runtime",
                                "path": "sources/runtime.zip",
                                "size_bytes": archive.stat().st_size,
                                "sha256": digest,
                                "license": "MIT",
                                "sources": {},
                                "install": {
                                    "kind": "zip",
                                    "destination": "latentsync/runtime",
                                    "strip_components": 1,
                                },
                            }
                        ],
                    },
                    {
                        "id": "music_video",
                        "label": "music",
                        "status": "FIXED",
                        "requires_gpu": True,
                        "dependencies": [],
                        "files": [],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=load_video_manifest(manifest_path),
    )

    assert installer.import_offline(
        bundle_id="ordinary_video", offline_root=archive.parents[1]
    ) == "APPLIED"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and installer.status()["bundles"][0]["state"] not in {"ready", "failed"}:
        time.sleep(0.01)

    assert installer.status()["bundles"][0]["state"] == "ready"
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


def test_seed_vc_overlap_frames_patch_is_applied_and_verified(tmp_path: Path) -> None:
    seed_root = tmp_path / "seed-vc"
    seed_root.mkdir()
    inference = seed_root / "inference.py"
    inference.write_text(
        "    overlap_frame_len = 16\n"
        "    parser.add_argument(\"--fp16\", type=str2bool, default=True)\n",
        encoding="utf-8",
    )

    apply_seed_vc_overlap_frames_patch(
        seed_root, Path("installer/seed-vc-overlap-frames.patch")
    )

    patched = inference.read_text(encoding="utf-8")
    assert "overlap_frame_len = args.overlap_frames" in patched
    assert 'parser.add_argument("--overlap-frames", type=int, default=16)' in patched
    marker = json.loads(
        (seed_root / ".olivia-overlap-frames-patched.json").read_text(encoding="utf-8")
    )
    assert marker["schema_version"] == "olivia.seed-vc-patch.v1"
    assert len(marker["patch_sha256"]) == 64


def test_safe_archive_rejects_windows_case_collisions(tmp_path: Path) -> None:
    archive = tmp_path / "collision.zip"
    with zipfile.ZipFile(archive, "w") as payload:
        payload.writestr("Runtime/inference.py", "first")
        payload.writestr("runtime/inference.py", "second")

    with pytest.raises(VideoCapabilityError, match="VIDEO_ARCHIVE_DUPLICATE_PATH"):
        _extract_zip_safely(archive, tmp_path / "runtime", strip_components=0)


def test_license_review_required_bundle_fails_closed(tmp_path: Path) -> None:
    ordinary = VideoBundle("ordinary_video", "ordinary", "FIXED", False, (), ())
    music = VideoBundle(
        "music_video", "music", "FIXED", False, (), (), True
    )
    installer = VideoCapabilityInstaller(
        data_root=(tmp_path / "data").resolve(),
        manifest=VideoManifest("1.0.0", (ordinary, music)),
    )

    with pytest.raises(VideoCapabilityError, match="VIDEO_LICENSE_REVIEW_REQUIRED"):
        installer.start(bundle_id="music_video")

    assert installer.start(
        bundle_id="music_video", accept_licenses=True
    ) == "APPLIED"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and installer.status()["bundles"][1]["state"] not in {"license_review_required", "failed"}:
        time.sleep(0.01)
    assert installer.status()["bundles"][1] == {
        "id": "music_video",
        "state": "license_review_required",
        "downloaded_bytes": 0,
        "total_bytes": 0,
        "remaining_bytes": 0,
        "reason_code": "VIDEO_LICENSED_DEPENDENCIES_REQUIRED",
    }


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
            environment={},
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


def test_video_capability_api_forwards_explicit_license_acceptance() -> None:
    observed = {}

    class FakeInstaller:
        def status(self):
            return {"status": "UNAVAILABLE", "capability": "video", "bundles": []}

        def start(self, **kwargs):
            observed.update(kwargs)
            return "APPLIED"

    async def call():
        app = web.Application()
        mount_original_client_video_capability_api(
            app,
            FakeInstaller(),
            trusted_origins=(),
            authorize_session=lambda _token: None,
            environment={},
        )
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/toy/capabilities/video/action",
                json={
                    "action": "install",
                    "bundle_id": "music_video",
                    "source": "auto",
                    "accept_licenses": True,
                },
                headers={
                    "Origin": "http://localhost:3000",
                    "X-Olivia-Capability-Action": "confirmed",
                    "X-Olivia-Setup-Session": "session",
                },
            )
            return response.status

    assert asyncio.run(call()) == 200
    assert observed == {
        "bundle_id": "music_video",
        "source_mode": "auto",
        "accept_licenses": True,
    }
