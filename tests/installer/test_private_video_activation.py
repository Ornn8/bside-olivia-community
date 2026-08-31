from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from installer.activate_private_video import (
    PrivateVideoActivationError,
    activate_private_video,
)


def _manifest_fixture(root: Path) -> tuple[Path, Path, str]:
    (root / "install").mkdir()
    offline = root / "Olivia-video-offline-private"
    bundles = []
    for bundle_id, relative, content in (
        ("ordinary_video", "ordinary.bin", b"ordinary"),
        ("music_video", "music.bin", b"music"),
    ):
        target = offline / bundle_id / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        bundles.append(
            {
                "id": bundle_id,
                "label": bundle_id,
                "status": "FIXED",
                "requires_gpu": True,
                "dependencies": [],
                "files": [
                    {
                        "id": f"{bundle_id}-fixture",
                        "path": relative,
                        "size_bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "license": "MIT",
                        "sources": {
                            "official": f"https://example.com/{relative}"
                        },
                    }
                ],
            }
        )
    manifest = root / "video-capability-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "olivia.video-capability-bom.v1",
                "version": "fixture-video",
                "bundles": bundles,
            }
        ),
        encoding="utf-8",
    )
    return manifest, offline, hashlib.sha256(manifest.read_bytes()).hexdigest()


class _FakeInstaller:
    def __init__(self, calls: list[tuple[str, str]], *, fail_bundle: str = "") -> None:
        self.calls = calls
        self.fail_bundle = fail_bundle
        self.states = {"ordinary_video": "missing", "music_video": "missing"}
        self.runtime_state = "idle"

    def status(self) -> dict[str, object]:
        bundles = [
            {"id": bundle_id, "state": state}
            for bundle_id, state in self.states.items()
        ]
        return {
            "status": (
                "READY"
                if all(state == "ready" for state in self.states.values())
                and self.runtime_state == "ready"
                else "UNAVAILABLE"
            ),
            "bundles": bundles,
            "runtime_import": {"state": self.runtime_state},
        }

    def import_offline(
        self,
        *,
        bundle_id: str,
        offline_root: Path,
        source_mode: str,
        accept_licenses: bool,
    ) -> str:
        assert offline_root.name == bundle_id
        assert source_mode == "official"
        assert accept_licenses is True
        self.calls.append(("offline", bundle_id))
        self.states[bundle_id] = (
            "failed" if bundle_id == self.fail_bundle else "prerequisites_required"
        )
        return "APPLIED"

    def import_runtime_archive(self, *, runtime_archive: Path) -> str:
        assert runtime_archive.name == "Olivia-video-runtime-private.zip"
        self.calls.append(("runtime", runtime_archive.name))
        self.runtime_state = "ready"
        self.states = {bundle_id: "ready" for bundle_id in self.states}
        return "APPLIED"


def test_private_activation_uses_existing_installer_in_strict_ready_order(
    tmp_path: Path,
) -> None:
    manifest, offline, manifest_sha256 = _manifest_fixture(tmp_path)
    runtime = tmp_path / "Olivia-video-runtime-private.zip"
    runtime.write_bytes(b"runtime")
    calls: list[tuple[str, str]] = []
    installer = _FakeInstaller(calls)

    result = activate_private_video(
        install_root=tmp_path / "install",
        offline_root=offline,
        runtime_archive=runtime,
        manifest_path=manifest,
        expected_manifest_version="fixture-video",
        expected_manifest_sha256=manifest_sha256,
        expected_file_count=2,
        expected_size_bytes=len(b"ordinary") + len(b"music"),
        installer_factory=lambda **_kwargs: installer,
        timeout_seconds=1,
    )

    assert calls == [
        ("offline", "ordinary_video"),
        ("offline", "music_video"),
        ("runtime", "Olivia-video-runtime-private.zip"),
    ]
    assert result["status"] == "READY"
    assert result["runtime_import"]["state"] == "ready"
    assert [item["state"] for item in result["bundles"]] == ["ready", "ready"]


def test_private_activation_rejects_tampered_or_extra_offline_files_before_import(
    tmp_path: Path,
) -> None:
    manifest, offline, manifest_sha256 = _manifest_fixture(tmp_path)
    runtime = tmp_path / "Olivia-video-runtime-private.zip"
    runtime.write_bytes(b"runtime")
    (offline / "ordinary_video/extra.bin").write_bytes(b"extra")

    with pytest.raises(
        PrivateVideoActivationError, match="VIDEO_PRIVATE_OFFLINE_INVALID"
    ):
        activate_private_video(
            install_root=tmp_path / "install",
            offline_root=offline,
            runtime_archive=runtime,
            manifest_path=manifest,
            expected_manifest_version="fixture-video",
            expected_manifest_sha256=manifest_sha256,
            expected_file_count=2,
            expected_size_bytes=len(b"ordinary") + len(b"music"),
            installer_factory=lambda **_kwargs: pytest.fail(
                "invalid sidecar must fail before installer creation"
            ),
            timeout_seconds=1,
        )


def test_private_activation_stops_before_runtime_when_bundle_fails(
    tmp_path: Path,
) -> None:
    manifest, offline, manifest_sha256 = _manifest_fixture(tmp_path)
    runtime = tmp_path / "Olivia-video-runtime-private.zip"
    runtime.write_bytes(b"runtime")
    calls: list[tuple[str, str]] = []
    installer = _FakeInstaller(calls, fail_bundle="music_video")

    with pytest.raises(
        PrivateVideoActivationError, match="VIDEO_BUNDLE_INSTALL_FAILED"
    ):
        activate_private_video(
            install_root=tmp_path / "install",
            offline_root=offline,
            runtime_archive=runtime,
            manifest_path=manifest,
            expected_manifest_version="fixture-video",
            expected_manifest_sha256=manifest_sha256,
            expected_file_count=2,
            expected_size_bytes=len(b"ordinary") + len(b"music"),
            installer_factory=lambda **_kwargs: installer,
            timeout_seconds=1,
        )

    assert calls == [
        ("offline", "ordinary_video"),
        ("offline", "music_video"),
    ]
