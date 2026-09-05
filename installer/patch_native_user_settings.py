"""Enable the original client's mailbox and music widgets safely."""

from __future__ import annotations

import hashlib
import os
import stat
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path


FALSE_WIDGET_PAIR = b"\x0amailWidget\x06\x05false\x0bmusicWidget\x06\x05false"
TRUE_WIDGET_PAIR = b"\x0amailWidget\x05\x04true\x0bmusicWidget\x05\x04true"
# Earlier releases shortened the string but left the enclosing value length at 6.
# Recognize only that exact malformed pair so an upgrade can repair it in place.
_LEGACY_TRUE_WIDGET_PAIR = b"\x0amailWidget\x06\x04true\x0bmusicWidget\x06\x04true"
_BACKUP_SUFFIX = ".native-nav.orig"
_REPARSE_POINT = 0x00000400


@dataclass(frozen=True)
class NativeUserSettingsPatchResult:
    """Path-free outcome safe to include in launcher diagnostics."""

    status: str
    original_sha256: str | None = None
    patched_sha256: str | None = None


@dataclass(frozen=True)
class _TargetSnapshot:
    device: int
    inode: int
    size: int
    modified_ns: int
    sha256: str


class NativeUserSettingsPatchError(RuntimeError):
    """Stable, path-free failure raised by the settings helper."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)


def _assert_safe_ancestors(path: Path) -> None:
    current = path.parent
    while True:
        metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise NativeUserSettingsPatchError("USER_SETTINGS_UNSAFE_PATH")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _safe_target_metadata(path: Path) -> os.stat_result:
    _assert_safe_ancestors(path)
    metadata = os.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
        or metadata.st_nlink != 1
    ):
        raise NativeUserSettingsPatchError("USER_SETTINGS_UNSAFE_PATH")
    return metadata


def _snapshot(path: Path) -> tuple[bytes, _TargetSnapshot]:
    before = _safe_target_metadata(path)
    payload = path.read_bytes()
    after = _safe_target_metadata(path)
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if before_identity != after_identity or len(payload) != after.st_size:
        raise NativeUserSettingsPatchError("USER_SETTINGS_TARGET_DRIFT")
    return payload, _TargetSnapshot(*after_identity, _sha256(payload))


def _matches_snapshot(path: Path, expected: _TargetSnapshot) -> bool:
    try:
        _payload, current = _snapshot(path)
    except FileNotFoundError:
        return False
    return current == expected


def _atomic_sidecar(path: Path, payload: bytes) -> None:
    _assert_safe_ancestors(path)
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _is_reparse(metadata)
            or metadata.st_nlink != 1
        ):
            raise NativeUserSettingsPatchError("USER_SETTINGS_UNSAFE_PATH")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_target(
    path: Path,
    payload: bytes,
    expected: _TargetSnapshot,
) -> None:
    if not _matches_snapshot(path, expected):
        raise NativeUserSettingsPatchError("USER_SETTINGS_TARGET_DRIFT")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if not _matches_snapshot(path, expected):
            raise NativeUserSettingsPatchError("USER_SETTINGS_TARGET_DRIFT")
        os.replace(temporary, path)
    except NativeUserSettingsPatchError:
        raise
    except OSError:
        raise NativeUserSettingsPatchError("USER_SETTINGS_REPLACE_FAILED") from None
    finally:
        temporary.unlink(missing_ok=True)


def _logical_bytes(original: bytes) -> tuple[int, bytes]:
    if len(original) < 4:
        raise NativeUserSettingsPatchError("USER_SETTINGS_HEADER_INVALID")
    payload_size = struct.unpack_from("<I", original)[0]
    logical_end = 4 + payload_size
    if payload_size == 0 or logical_end > len(original):
        raise NativeUserSettingsPatchError("USER_SETTINGS_HEADER_INVALID")
    if any(original[logical_end:]):
        raise NativeUserSettingsPatchError("USER_SETTINGS_PADDING_INVALID")
    return payload_size, original[:logical_end]


def _patched_payload(original: bytes, payload_size: int, logical: bytes, source: bytes) -> bytes:
    replaced = logical.replace(source, TRUE_WIDGET_PAIR, 1)
    patched_size = payload_size - (len(source) - len(TRUE_WIDGET_PAIR))
    patched_logical = struct.pack("<I", patched_size) + replaced[4:]
    return patched_logical + (b"\x00" * (len(original) - len(patched_logical)))


def _patch(settings_path: Path) -> NativeUserSettingsPatchResult:
    try:
        original, target_snapshot = _snapshot(settings_path)
    except FileNotFoundError:
        return NativeUserSettingsPatchResult(status="missing")

    payload_size, logical = _logical_bytes(original)
    false_count = logical.count(FALSE_WIDGET_PAIR)
    true_count = logical.count(TRUE_WIDGET_PAIR)
    legacy_count = logical.count(_LEGACY_TRUE_WIDGET_PAIR)
    original_sha256 = target_snapshot.sha256
    if false_count + true_count + legacy_count > 1:
        raise NativeUserSettingsPatchError("USER_SETTINGS_WIDGET_PAIR_AMBIGUOUS")
    if false_count == 0 and true_count == 1:
        return NativeUserSettingsPatchResult(
            status="already_enabled",
            original_sha256=original_sha256,
            patched_sha256=original_sha256,
        )
    if false_count + legacy_count != 1 or true_count != 0:
        raise NativeUserSettingsPatchError("USER_SETTINGS_WIDGET_PAIR_UNSUPPORTED")

    source = FALSE_WIDGET_PAIR if false_count else _LEGACY_TRUE_WIDGET_PAIR
    patched = _patched_payload(original, payload_size, logical, source)
    backup_path = settings_path.with_name(settings_path.name + _BACKUP_SUFFIX)
    try:
        _atomic_sidecar(backup_path, original)
    except NativeUserSettingsPatchError:
        raise
    except OSError:
        raise NativeUserSettingsPatchError("USER_SETTINGS_BACKUP_FAILED") from None
    _publish_target(settings_path, patched, target_snapshot)
    return NativeUserSettingsPatchResult(
        status="patched",
        original_sha256=original_sha256,
        patched_sha256=_sha256(patched),
    )


def patch_native_user_settings(settings_path: Path) -> NativeUserSettingsPatchResult:
    """Enable the unique widget pair without exposing the target path."""

    try:
        return _patch(Path(settings_path))
    except NativeUserSettingsPatchError:
        raise
    except OSError:
        raise NativeUserSettingsPatchError("USER_SETTINGS_IO_FAILED") from None


__all__ = [
    "FALSE_WIDGET_PAIR",
    "TRUE_WIDGET_PAIR",
    "NativeUserSettingsPatchError",
    "NativeUserSettingsPatchResult",
    "patch_native_user_settings",
]
