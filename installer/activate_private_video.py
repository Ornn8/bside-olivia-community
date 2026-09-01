from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, os.fspath(Path(os.path.abspath(__file__)).parents[1]))

from installer.component_update import ComponentUpdateError, _validate_relative_path
from installer.start_local import _load_fixed_video_assets_environment
from music_reply import video_reply_dependency_status
from runtime.media.media_paths import configured_media_path
from video_capability_install import (
    VideoCapabilityError,
    VideoCapabilityInstaller,
    VideoManifest,
    load_video_manifest,
)


OFFLINE_ROOT_NAME = "Olivia-video-offline-private"
RUNTIME_ARCHIVE_NAME = "Olivia-video-runtime-private.zip"
_ASSEMBLED_STATES = {"ready", "prerequisites_required"}
_ACTIVE_STATES = {"missing", "queued", "downloading", "verifying"}
_SETUP_PROGRESS_PREFIX = "OLIVIA_SETUP_PROGRESS="


class PrivateVideoActivationError(RuntimeError):
    """Stable private-video activation failure code."""


class _ManifestSnapshot:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read_text(self, *, encoding: str) -> str:
        return self._payload.decode(encoding)


def _report_progress(
    progress: Callable[[str, int, int], object] | None,
    phase: str,
    current: int,
    total: int,
) -> None:
    if progress is None:
        return
    try:
        progress(phase, max(0, current), max(0, total))
    except Exception:
        pass


def _setup_progress_writer(
    write_line: Callable[[str], object] | None = None,
) -> Callable[[str, int, int], None]:
    if write_line is None:
        write_line = lambda line: print(line, flush=True)
    reported: dict[str, tuple[int, int]] = {}

    def emit(phase: str, current: int, total: int) -> None:
        previous = reported.get(phase)
        if previous == (current, total):
            return
        if total > 0 and current not in {0, total}:
            minimum_delta = max(64 * 1024 * 1024, total // 1_000)
            if previous is not None and current - previous[0] < minimum_delta:
                return
        reported[phase] = (current, total)
        write_line(f"{_SETUP_PROGRESS_PREFIX}{phase}|{current}|{total}")

    return emit


def _sha256(path: Path, *, progress: Callable[[int], object] | None = None) -> str:
    digest = hashlib.sha256()
    checked = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
            checked += len(block)
            if progress is not None:
                progress(checked)
    return digest.hexdigest()


def _is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _absolute_path(path: Path, *, code: str) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise PrivateVideoActivationError(code)
    try:
        candidate = Path(os.path.abspath(candidate))
        for current in reversed((candidate, *candidate.parents)):
            if (current.is_symlink() or current.exists()) and _is_reparse_point(current):
                raise PrivateVideoActivationError(code)
        return candidate
    except PrivateVideoActivationError:
        raise
    except (OSError, RuntimeError) as exc:
        raise PrivateVideoActivationError(code) from exc


def _verify_manifest(
    path: Path, *, expected_version: str, expected_sha256: str
) -> VideoManifest:
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise PrivateVideoActivationError("VIDEO_PRIVATE_MANIFEST_INVALID")
    manifest_path = _absolute_path(path, code="VIDEO_PRIVATE_MANIFEST_INVALID")
    try:
        if not manifest_path.is_file() or _is_reparse_point(manifest_path):
            raise PrivateVideoActivationError("VIDEO_PRIVATE_MANIFEST_INVALID")
        payload = manifest_path.read_bytes()
    except PrivateVideoActivationError:
        raise
    except OSError as exc:
        raise PrivateVideoActivationError("VIDEO_PRIVATE_MANIFEST_INVALID") from exc
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise PrivateVideoActivationError("VIDEO_PRIVATE_MANIFEST_INVALID")
    try:
        manifest = load_video_manifest(_ManifestSnapshot(payload))  # type: ignore[arg-type]
    except VideoCapabilityError as exc:
        raise PrivateVideoActivationError("VIDEO_PRIVATE_MANIFEST_INVALID") from exc
    if manifest.version != expected_version:
        raise PrivateVideoActivationError("VIDEO_PRIVATE_MANIFEST_INVALID")
    return manifest


def _verify_offline_root(
    root: Path,
    manifest: VideoManifest,
    *,
    expected_file_count: int,
    expected_size_bytes: int,
    progress: Callable[[str, int, int], object] | None = None,
) -> Path:
    candidate = _absolute_path(root, code="VIDEO_PRIVATE_OFFLINE_INVALID")
    if candidate.name != OFFLINE_ROOT_NAME or not candidate.is_dir() or _is_reparse_point(candidate):
        raise PrivateVideoActivationError("VIDEO_PRIVATE_OFFLINE_INVALID")
    expected: dict[str, tuple[str, int, str]] = {}
    for bundle in manifest.bundles:
        for item in bundle.files:
            try:
                relative = _validate_relative_path(item.relative_path)
            except ComponentUpdateError as exc:
                raise PrivateVideoActivationError("VIDEO_PRIVATE_OFFLINE_INVALID") from exc
            path = f"{bundle.identifier}/{relative}"
            folded = path.casefold()
            if folded in expected:
                raise PrivateVideoActivationError("VIDEO_PRIVATE_OFFLINE_INVALID")
            expected[folded] = (path, item.size_bytes, item.sha256)
    if (
        expected_file_count < 1
        or expected_size_bytes < 1
        or len(expected) != expected_file_count
        or sum(size for _, size, _ in expected.values()) != expected_size_bytes
    ):
        raise PrivateVideoActivationError("VIDEO_PRIVATE_OFFLINE_INVALID")

    actual: dict[str, Path] = {}
    actual_directories: set[str] = set()
    try:
        for current, directories, filenames in os.walk(candidate, followlinks=False):
            current_path = Path(current)
            if _is_reparse_point(current_path):
                raise PrivateVideoActivationError("VIDEO_PRIVATE_OFFLINE_INVALID")
            for name in directories:
                directory = current_path / name
                if _is_reparse_point(directory):
                    raise PrivateVideoActivationError("VIDEO_PRIVATE_OFFLINE_INVALID")
                actual_directories.add(
                    directory.relative_to(candidate).as_posix().casefold()
                )
            for name in filenames:
                path = current_path / name
                if not path.is_file() or _is_reparse_point(path):
                    raise PrivateVideoActivationError("VIDEO_PRIVATE_OFFLINE_INVALID")
                relative = path.relative_to(candidate).as_posix()
                folded = relative.casefold()
                if folded in actual:
                    raise PrivateVideoActivationError("VIDEO_PRIVATE_OFFLINE_INVALID")
                actual[folded] = path
    except OSError as exc:
        raise PrivateVideoActivationError("VIDEO_PRIVATE_OFFLINE_INVALID") from exc
    expected_directories = {
        parent.as_posix().casefold()
        for relative, _, _ in expected.values()
        for parent in PurePosixPath(relative).parents
        if parent.as_posix() != "."
    }
    if set(actual) != set(expected) or actual_directories - expected_directories:
        raise PrivateVideoActivationError("VIDEO_PRIVATE_OFFLINE_INVALID")
    checked_bytes = 0
    _report_progress(
        progress, "VERIFY_VIDEO_OFFLINE", checked_bytes, expected_size_bytes
    )
    for folded, (_, size, digest) in expected.items():
        path = actual[folded]
        try:
            if path.stat().st_size != size or _sha256(
                path,
                progress=lambda current, base=checked_bytes: _report_progress(
                    progress,
                    "VERIFY_VIDEO_OFFLINE",
                    base + current,
                    expected_size_bytes,
                ),
            ) != digest:
                raise PrivateVideoActivationError("VIDEO_PRIVATE_OFFLINE_INVALID")
        except OSError as exc:
            raise PrivateVideoActivationError("VIDEO_PRIVATE_OFFLINE_INVALID") from exc
        checked_bytes += size
        _report_progress(
            progress, "VERIFY_VIDEO_OFFLINE", checked_bytes, expected_size_bytes
        )
    return candidate


def _create_installer(
    *,
    install_root: Path,
    manifest: VideoManifest,
    runtime_progress: Callable[[str, int, int], object] | None = None,
) -> VideoCapabilityInstaller:
    data_root = install_root / "data"
    environment = _load_fixed_video_assets_environment(
        {
            **os.environ,
            "OLIVIA_INSTALL_ROOT": str(install_root),
            "OLIVIA_PROJECT_ROOT": str(install_root / "app"),
            "OLIVIA_LOCAL_DATA_ROOT": str(data_root),
            "OLIVIA_PROVIDER_CACHE_ROOT": str(data_root / "provider-cache"),
        },
        install_root,
    )
    os.environ.update(environment)

    def readiness(runtime_environment: Mapping[str, str]) -> Mapping[str, object]:
        values = dict(environment)
        values.update(runtime_environment)
        return video_reply_dependency_status(
            values,
            performance_video_path=configured_media_path(
                values, "OLIVIA_MUSIC_PERFORMANCE_BASE"
            ),
        )

    return VideoCapabilityInstaller(
        data_root=data_root,
        manifest=manifest,
        readiness_probe=readiness,
        runtime_environment_applier=os.environ.update,
        runtime_progress=runtime_progress,
    )


def _bundle_state(status: Mapping[str, object], bundle_id: str) -> Mapping[str, object]:
    bundles = status.get("bundles")
    if not isinstance(bundles, list):
        raise PrivateVideoActivationError("VIDEO_PRIVATE_ACTIVATION_FAILED")
    for item in bundles:
        if isinstance(item, Mapping) and item.get("id") == bundle_id:
            return item
    raise PrivateVideoActivationError("VIDEO_PRIVATE_ACTIVATION_FAILED")


def _wait_for_bundle(
    installer: Any,
    bundle_id: str,
    *,
    deadline: float,
    clock: Callable[[], float],
    sleep: Callable[[float], object],
    phase: str,
    progress: Callable[[str, int, int], object] | None,
) -> None:
    while True:
        item = _bundle_state(installer.status(), bundle_id)
        state = item.get("state")
        if state in _ASSEMBLED_STATES:
            _report_progress(progress, phase, 1, 1)
            return
        if state not in _ACTIVE_STATES:
            reason = item.get("reason_code")
            code = reason if isinstance(reason, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{3,95}", reason) else "VIDEO_BUNDLE_INSTALL_FAILED"
            raise PrivateVideoActivationError(code)
        if clock() >= deadline:
            raise PrivateVideoActivationError("VIDEO_PRIVATE_ACTIVATION_TIMEOUT")
        current = item.get("downloaded_bytes")
        total = item.get("total_bytes")
        _report_progress(
            progress,
            phase,
            current if type(current) is int else 0,
            total if type(total) is int else 0,
        )
        sleep(0.1)


def activate_private_video(
    *,
    install_root: Path,
    offline_root: Path,
    runtime_archive: Path | None,
    manifest_path: Path,
    expected_manifest_version: str,
    expected_manifest_sha256: str,
    expected_file_count: int,
    expected_size_bytes: int,
    timeout_seconds: float = 14_400,
    installer_factory: Callable[..., Any] = _create_installer,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
    progress: Callable[[str, int, int], object] | None = None,
) -> dict[str, object]:
    if timeout_seconds <= 0:
        raise PrivateVideoActivationError("VIDEO_PRIVATE_ACTIVATION_TIMEOUT")
    root = _absolute_path(install_root, code="VIDEO_PRIVATE_INSTALL_ROOT_INVALID")
    if not root.is_dir() or _is_reparse_point(root):
        raise PrivateVideoActivationError("VIDEO_PRIVATE_INSTALL_ROOT_INVALID")
    _absolute_path(root / "data", code="VIDEO_PRIVATE_INSTALL_ROOT_INVALID")
    manifest = _verify_manifest(
        manifest_path,
        expected_version=expected_manifest_version,
        expected_sha256=expected_manifest_sha256,
    )
    offline = _verify_offline_root(
        offline_root,
        manifest,
        expected_file_count=expected_file_count,
        expected_size_bytes=expected_size_bytes,
        progress=progress,
    )
    try:
        def report_runtime(state: str, current: int, total: int) -> None:
            phase = {
                "queued": "EXTRACT_VIDEO_RUNTIME",
                "extracting": "EXTRACT_VIDEO_RUNTIME",
                "checking": "VERIFY_VIDEO_RUNTIME",
                "testing": "TEST_VIDEO_RUNTIME",
                "ready": "TEST_VIDEO_RUNTIME",
            }.get(state)
            if phase is not None:
                _report_progress(progress, phase, current, total)

        installer = installer_factory(
            install_root=root,
            manifest=manifest,
            runtime_progress=report_runtime,
        )
        deadline = clock() + timeout_seconds
        for bundle_id in ("ordinary_video", "music_video"):
            phase = (
                "INSTALL_ORDINARY_VIDEO"
                if bundle_id == "ordinary_video"
                else "INSTALL_MUSIC_VIDEO"
            )
            current = _bundle_state(installer.status(), bundle_id)
            if current.get("state") not in _ASSEMBLED_STATES:
                _report_progress(progress, phase, 0, 0)
                installer.import_offline(
                    bundle_id=bundle_id,
                    offline_root=offline / bundle_id,
                    source_mode="official",
                    accept_licenses=True,
                )
                _wait_for_bundle(
                    installer,
                    bundle_id,
                    deadline=deadline,
                    clock=clock,
                    sleep=sleep,
                    phase=phase,
                    progress=progress,
                )
            else:
                _report_progress(progress, phase, 1, 1)
        final = installer.status()
        runtime_import = final.get("runtime_import")
        if (
            isinstance(runtime_import, Mapping)
            and runtime_import.get("state") == "required"
        ):
            if runtime_archive is None:
                raise PrivateVideoActivationError("VIDEO_RUNTIME_ARCHIVE_REQUIRED")
            runtime = _absolute_path(
                runtime_archive, code="VIDEO_RUNTIME_ARCHIVE_INVALID"
            )
            if (
                runtime.name != RUNTIME_ARCHIVE_NAME
                or not runtime.is_file()
                or _is_reparse_point(runtime)
            ):
                raise PrivateVideoActivationError("VIDEO_RUNTIME_ARCHIVE_INVALID")
            _report_progress(progress, "EXTRACT_VIDEO_RUNTIME", 0, 0)
            installer.import_runtime_archive(runtime_archive=runtime)
            final = installer.status()
    except PrivateVideoActivationError:
        raise
    except VideoCapabilityError as exc:
        code = str(exc)
        if re.fullmatch(r"[A-Z][A-Z0-9_]{3,95}", code):
            raise PrivateVideoActivationError(code) from exc
        raise PrivateVideoActivationError("VIDEO_PRIVATE_ACTIVATION_FAILED") from exc
    except Exception as exc:
        raise PrivateVideoActivationError("VIDEO_PRIVATE_ACTIVATION_FAILED") from exc
    runtime_import = final.get("runtime_import")
    runtime_ready = isinstance(runtime_import, Mapping) and runtime_import.get("state") == "ready"
    bundle_items = [_bundle_state(final, bundle_id) for bundle_id in ("ordinary_video", "music_video")]
    ready = final.get("status") == "READY" and all(item.get("state") == "ready" for item in bundle_items)
    unavailable = final.get("status") == "UNAVAILABLE" and all(
        item.get("state") == "prerequisites_required"
        and item.get("reason_code") == "VIDEO_RUNTIME_HOST_UNAVAILABLE"
        for item in bundle_items
    )
    if not runtime_ready or not (ready or unavailable):
        raise PrivateVideoActivationError("VIDEO_PRIVATE_NOT_READY")
    return dict(final)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="activate-private-video")
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--offline-root", type=Path, required=True)
    parser.add_argument("--runtime-archive", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-version", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--expected-file-count", type=int, required=True)
    parser.add_argument("--expected-size-bytes", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=14_400)
    args = parser.parse_args(argv)

    emit_progress = _setup_progress_writer()

    try:
        result = activate_private_video(
            install_root=args.install_root,
            offline_root=args.offline_root,
            runtime_archive=args.runtime_archive,
            manifest_path=args.manifest,
            expected_manifest_version=args.manifest_version,
            expected_manifest_sha256=args.manifest_sha256,
            expected_file_count=args.expected_file_count,
            expected_size_bytes=args.expected_size_bytes,
            timeout_seconds=args.timeout_seconds,
            progress=emit_progress,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except PrivateVideoActivationError as exc:
        print(json.dumps({"status": "ERROR", "code": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
