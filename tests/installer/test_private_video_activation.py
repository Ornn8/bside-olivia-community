from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from installer.activate_private_video import (
    PrivateVideoActivationError,
    _setup_progress_writer,
    activate_private_video,
    main as activate_private_video_main,
)
from video_capability_install import VideoCapabilityInstaller


def test_setup_progress_writer_throttles_large_and_repeated_updates() -> None:
    lines: list[str] = []
    emit = _setup_progress_writer(lines.append)
    total = 100 * 1024 * 1024 * 1024

    for current in range(0, total + 1, 1024 * 1024):
        emit("VERIFY_VIDEO_RUNTIME", min(current, total), total)
    emit("VERIFY_VIDEO_RUNTIME", total, total)
    for _ in range(1_000):
        emit("TEST_VIDEO_RUNTIME", 0, 0)

    assert lines[0] == f"OLIVIA_SETUP_PROGRESS=VERIFY_VIDEO_RUNTIME|0|{total}"
    assert lines[-2] == (
        f"OLIVIA_SETUP_PROGRESS=VERIFY_VIDEO_RUNTIME|{total}|{total}"
    )
    assert lines[-1] == "OLIVIA_SETUP_PROGRESS=TEST_VIDEO_RUNTIME|0|0"
    assert len(lines) < 1_100


def test_private_activation_cli_bootstraps_payload_root_under_isolated_python() -> None:
    script = Path(__file__).parents[2] / "installer" / "activate_private_video.py"

    result = subprocess.run(
        [sys.executable, "-I", str(script), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    return_code = result.returncode
    output = result.stdout

    assert return_code == 0
    assert "--install-root" in output


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
                "runtime_environment": (
                    {
                        "OLIVIA_BREEZE_TTS_PYTHON": relative,
                        "OLIVIA_LATENTSYNC_PYTHON": relative,
                    }
                    if bundle_id == "ordinary_video"
                    else {
                        "OLIVIA_MINIMAX_COMFY_PYTHON": relative,
                        "OLIVIA_ROFORMER_PYTHON": relative,
                    }
                ),
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
        self.runtime_state = "required"

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


class _UnavailableHostInstaller(_FakeInstaller):
    def import_runtime_archive(self, *, runtime_archive: Path) -> str:
        result = super().import_runtime_archive(runtime_archive=runtime_archive)
        self.states = {bundle_id: "prerequisites_required" for bundle_id in self.states}
        return result

    def status(self) -> dict[str, object]:
        result = super().status()
        result["status"] = "UNAVAILABLE"
        result["bundles"] = [
            {**item, "reason_code": "VIDEO_RUNTIME_HOST_UNAVAILABLE"}
            for item in result["bundles"]
        ]
        return result


class _SplitReadyInstaller(_FakeInstaller):
    def import_offline(
        self,
        *,
        bundle_id: str,
        offline_root: Path,
        source_mode: str,
        accept_licenses: bool,
    ) -> str:
        result = super().import_offline(
            bundle_id=bundle_id,
            offline_root=offline_root,
            source_mode=source_mode,
            accept_licenses=accept_licenses,
        )
        self.states[bundle_id] = "ready"
        if all(state == "ready" for state in self.states.values()):
            self.runtime_state = "ready"
        return result


def test_private_activation_keeps_split_runtime_ready_without_legacy_archive(
    tmp_path: Path,
) -> None:
    manifest, offline, manifest_sha256 = _manifest_fixture(tmp_path)
    calls: list[tuple[str, str]] = []
    installer = _SplitReadyInstaller(calls)

    result = activate_private_video(
        install_root=tmp_path / "install",
        offline_root=offline,
        runtime_archive=None,
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
    assert result["status"] == "READY"
    assert result["runtime_import"]["state"] == "ready"


def test_clean_private_activation_uses_real_split_installer_without_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class InlineThread:
        def __init__(
            self,
            *,
            target: Callable[..., object],
            args: tuple[object, ...] = (),
            kwargs: dict[str, object] | None = None,
            **_ignored: object,
        ) -> None:
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}
            self._alive = False

        def start(self) -> None:
            self._alive = True
            try:
                self._target(*self._args, **self._kwargs)
            finally:
                self._alive = False

        def is_alive(self) -> bool:
            return self._alive

    manifest, offline, manifest_sha256 = _manifest_fixture(tmp_path)
    monkeypatch.setattr("video_capability_install.threading.Thread", InlineThread)
    monkeypatch.setattr(
        VideoCapabilityInstaller, "_runtime_artifacts_ready", lambda *_: True
    )
    monkeypatch.setattr(
        VideoCapabilityInstaller,
        "_configure_embedded_python",
        staticmethod(lambda path: path),
    )

    def installer_factory(**kwargs: object) -> VideoCapabilityInstaller:
        install_root = Path(kwargs["install_root"])
        return VideoCapabilityInstaller(
            data_root=install_root / "data",
            manifest=kwargs["manifest"],
            readiness_probe=lambda _environment: {
                "ordinary_missing_dependencies": [],
                "music_ready": True,
            },
            runtime_progress=kwargs["runtime_progress"],
            runtime_package_runner=lambda *_args: None,
            runtime_package_verifier=lambda *_args: True,
        )

    result = activate_private_video(
        install_root=tmp_path / "install",
        offline_root=offline,
        runtime_archive=None,
        manifest_path=manifest,
        expected_manifest_version="fixture-video",
        expected_manifest_sha256=manifest_sha256,
        expected_file_count=2,
        expected_size_bytes=len(b"ordinary") + len(b"music"),
        installer_factory=installer_factory,
        timeout_seconds=5,
    )

    assert result["status"] == "READY"
    assert result["runtime_import"]["state"] == "ready"
    assert not list(tmp_path.rglob("Olivia-video-runtime-*.zip"))

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


def test_private_activation_reports_verified_bytes_and_named_install_stages(
    tmp_path: Path,
) -> None:
    manifest, offline, manifest_sha256 = _manifest_fixture(tmp_path)
    runtime = tmp_path / "Olivia-video-runtime-private.zip"
    runtime.write_bytes(b"runtime")
    installer = _FakeInstaller([])
    progress: list[tuple[str, int, int]] = []

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
        progress=lambda phase, current, total: progress.append(
            (phase, current, total)
        ),
    )

    assert progress[0] == (
        "VERIFY_VIDEO_OFFLINE",
        0,
        len(b"ordinary") + len(b"music"),
    )
    assert (
        "VERIFY_VIDEO_OFFLINE",
        len(b"ordinary") + len(b"music"),
        len(b"ordinary") + len(b"music"),
    ) in progress
    assert ("INSTALL_ORDINARY_VIDEO", 1, 1) in progress
    assert ("INSTALL_MUSIC_VIDEO", 1, 1) in progress
    assert ("EXTRACT_VIDEO_RUNTIME", 0, 0) in progress


def test_private_activation_accepts_verified_runtime_with_unavailable_host(
    tmp_path: Path,
) -> None:
    manifest, offline, manifest_sha256 = _manifest_fixture(tmp_path)
    runtime = tmp_path / "Olivia-video-runtime-private.zip"
    runtime.write_bytes(b"runtime")
    installer = _UnavailableHostInstaller([])

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

    assert result["status"] == "UNAVAILABLE"
    assert result["runtime_import"]["state"] == "ready"
    assert [item["state"] for item in result["bundles"]] == [
        "prerequisites_required",
        "prerequisites_required",
    ]


def test_private_activation_parses_the_exact_manifest_bytes_that_were_hashed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, offline, manifest_sha256 = _manifest_fixture(tmp_path)
    runtime = tmp_path / "Olivia-video-runtime-private.zip"
    runtime.write_bytes(b"runtime")
    original_payload = json.loads(manifest.read_text(encoding="utf-8"))
    replacement_payload = json.loads(manifest.read_text(encoding="utf-8"))
    for bundle in replacement_payload["bundles"]:
        bundle["label"] = "replacement"
    replacement_bytes = json.dumps(replacement_payload).encode("utf-8")
    original_open = Path.open
    replaced = False

    class _ReplaceAfterRead:
        def __init__(self, stream: object) -> None:
            self._stream = stream

        def __enter__(self) -> object:
            return self._stream.__enter__()

        def __exit__(self, *args: object) -> object:
            result = self._stream.__exit__(*args)
            with original_open(manifest, "wb") as replacement:
                replacement.write(replacement_bytes)
            return result

    def racing_open(path: Path, *args: object, **kwargs: object) -> object:
        nonlocal replaced
        stream = original_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == manifest and "r" in str(mode) and not replaced:
            replaced = True
            return _ReplaceAfterRead(stream)
        return stream

    monkeypatch.setattr(Path, "open", racing_open)
    calls: list[tuple[str, str]] = []
    installer = _FakeInstaller(calls)
    observed_labels: list[str] = []

    def installer_factory(**kwargs: object) -> _FakeInstaller:
        observed_labels.extend(
            bundle.label for bundle in kwargs["manifest"].bundles
        )
        return installer

    activate_private_video(
        install_root=tmp_path / "install",
        offline_root=offline,
        runtime_archive=runtime,
        manifest_path=manifest,
        expected_manifest_version="fixture-video",
        expected_manifest_sha256=manifest_sha256,
        expected_file_count=2,
        expected_size_bytes=len(b"ordinary") + len(b"music"),
        installer_factory=installer_factory,
        timeout_seconds=1,
    )

    assert replaced is True
    assert observed_labels == [
        bundle["label"] for bundle in original_payload["bundles"]
    ]


def test_private_activation_cli_normalizes_manifest_io_failure_without_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest, offline, manifest_sha256 = _manifest_fixture(tmp_path)
    runtime = tmp_path / "Olivia-video-runtime-private.zip"
    runtime.write_bytes(b"runtime")
    original_open = Path.open

    def failing_open(path: Path, *args: object, **kwargs: object) -> object:
        if path == manifest:
            raise OSError(f"cannot read private manifest at {manifest}")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)

    exit_code = activate_private_video_main(
        [
            "--install-root",
            str(tmp_path / "install"),
            "--offline-root",
            str(offline),
            "--runtime-archive",
            str(runtime),
            "--manifest",
            str(manifest),
            "--manifest-version",
            "fixture-video",
            "--manifest-sha256",
            manifest_sha256,
            "--expected-file-count",
            "2",
            "--expected-size-bytes",
            str(len(b"ordinary") + len(b"music")),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert json.loads(captured.out) == {
        "status": "ERROR",
        "code": "VIDEO_PRIVATE_MANIFEST_INVALID",
    }
    assert captured.err == ""
    assert str(manifest) not in captured.out


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_private_activation_rejects_a_reparse_data_root_before_installer_creation(
    tmp_path: Path,
) -> None:
    manifest, offline, manifest_sha256 = _manifest_fixture(tmp_path)
    runtime = tmp_path / "Olivia-video-runtime-private.zip"
    runtime.write_bytes(b"runtime")
    outside = tmp_path / "outside"
    outside.mkdir()
    data_root = tmp_path / "install" / "data"
    linked = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(data_root), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if linked.returncode != 0:
        pytest.skip("Windows junction creation unavailable")

    with pytest.raises(
        PrivateVideoActivationError,
        match="^VIDEO_PRIVATE_INSTALL_ROOT_INVALID$",
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
                "reparse data root must fail before installer creation"
            ),
            timeout_seconds=1,
        )

    assert list(outside.iterdir()) == []


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
