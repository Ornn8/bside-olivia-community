"""Select the existing non-third-party initialization path in a managed copy.

Only the exact tested 0.0.9.627 plugin is accepted. Signed configuration,
Steam tickets, account/license checks and the platform's return code are intact.
Adapted from the earlier local-login adapter; the branch here has actual
Steam-stopped acceptance evidence. Original installations are not eligible.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import stat
import tempfile

_ORIGINAL_SHA256 = "993c49dded0a2ca7afa0d32498027e58eab077320219fb96aab12fed8a05886a"
_PATCHED_SHA256 = "01af516e044daccca202afe1a76f38b265b1ef587995975608aab46b25c3c58e"
_LEGACY_SHA256 = "4dbec92c0eaea2e2b88c9a18507343fbc8eb4e7d73817db21ffe81f65114849e"
_LEGACY_OFFSET = 0x9A80
_LEGACY_BEFORE = bytes.fromhex("0f 84 e8 01 00 00")
_LEGACY_AFTER = bytes.fromhex("e9 e9 01 00 00 90")
_SIZE_BYTES = 654816
_OFFSET = 0x9C11
_BEFORE = bytes.fromhex("74 5b")
_AFTER = bytes.fromhex("eb 5b")
_RELATIVE = "app/0.0.9.627/plugins/Login/NutLoginPlugin.dll"
_FILE_ERROR = "LOCAL_LOGIN_FILE_UNAVAILABLE_CLOSE_CLIENT_AND_RETRY"


class LocalLoginPatchError(ValueError):
    """Stable compatibility failure without paths or native content."""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _original(data: bytes) -> bool:
    return (len(data) == _SIZE_BYTES and _digest(data) == _ORIGINAL_SHA256
            and data[_OFFSET:_OFFSET + len(_BEFORE)] == _BEFORE)


def _patched(data: bytes) -> bool:
    return (len(data) == _SIZE_BYTES and _digest(data) == _PATCHED_SHA256
            and data[_OFFSET:_OFFSET + len(_AFTER)] == _AFTER)


def _legacy_original(data: bytes) -> bytes | None:
    if (len(data) != _SIZE_BYTES or _digest(data) != _LEGACY_SHA256
            or data[_LEGACY_OFFSET:_LEGACY_OFFSET + len(_LEGACY_AFTER)] != _LEGACY_AFTER):
        return None
    original = (data[:_LEGACY_OFFSET] + _LEGACY_BEFORE
                + data[_LEGACY_OFFSET + len(_LEGACY_AFTER):])
    return original if _original(original) else None


def _safe(path: Path) -> None:
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise LocalLoginPatchError("LOCAL_LOGIN_UNSAFE_PATH")


def _read(path: Path, invalid_code: str) -> bytes:
    if path.stat().st_size != _SIZE_BYTES:
        raise LocalLoginPatchError(invalid_code)
    return path.read_bytes()


def _atomic_replace(path: Path, replacement: bytes, *, source: Path, expected: bytes,
                    create_only: bool = False) -> None:
    fd, name = tempfile.mkstemp(prefix=".local-login-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(replacement)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.read_bytes() != replacement:
            raise LocalLoginPatchError(_FILE_ERROR)
        for candidate in (source, path):
            _safe(candidate)
        if source.read_bytes() != expected:
            raise LocalLoginPatchError("LOCAL_LOGIN_PLUGIN_CHANGED")
        if create_only:
            # Publish the complete backup without replacing a concurrent file.
            try:
                if os.name == "nt":
                    os.rename(temporary, path)
                else:
                    os.link(temporary, path)
            except FileExistsError:
                _safe(path)
                if _read(path, "LOCAL_LOGIN_BACKUP_INVALID") != replacement:
                    raise LocalLoginPatchError("LOCAL_LOGIN_BACKUP_INVALID") from None
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply(root: Path, *, restore: bool = False) -> str:
    """Patch only the managed native copy, preserving a verified original backup."""
    try:
        root = Path(os.path.abspath(root))
        target = root / _RELATIVE
        backup = target.with_name(target.name + ".local-login.orig")
        marker = root / "local_backend/local_server.py"
        for path in (marker, target, backup):
            _safe(path)
        if not marker.is_file():
            raise LocalLoginPatchError("LOCAL_LOGIN_MANAGED_INSTALL_REQUIRED")
        source = _read(target, "LOCAL_LOGIN_UNSUPPORTED_PLUGIN")
        is_original, is_patched = _original(source), _patched(source)
        original = source if is_original else _legacy_original(source)
        if original is None and not is_patched:
            raise LocalLoginPatchError("LOCAL_LOGIN_UNSUPPORTED_PLUGIN")
        trusted_backup = None
        if backup.exists():
            trusted_backup = _read(backup, "LOCAL_LOGIN_BACKUP_INVALID")
            if not _original(trusted_backup):
                raise LocalLoginPatchError("LOCAL_LOGIN_BACKUP_INVALID")
        if is_patched and trusted_backup is None:
            raise LocalLoginPatchError("LOCAL_LOGIN_BACKUP_INVALID")
        if trusted_backup is None and original is not None and not is_original:
            _atomic_replace(backup, original, source=target, expected=source, create_only=True)
            trusted_backup = _read(backup, "LOCAL_LOGIN_BACKUP_INVALID")
            if not _original(trusted_backup):
                raise LocalLoginPatchError("LOCAL_LOGIN_BACKUP_INVALID")
        if restore:
            if is_original:
                return "ALREADY_ORIGINAL"
            replacement = trusted_backup
        else:
            if is_patched:
                return "ALREADY_APPLIED"
            if trusted_backup is None:
                assert original is not None
                _atomic_replace(backup, original, source=target, expected=source, create_only=True)
            if not _original(_read(backup, "LOCAL_LOGIN_BACKUP_INVALID")):
                raise LocalLoginPatchError("LOCAL_LOGIN_BACKUP_INVALID")
            assert original is not None
            replacement = original[:_OFFSET] + _AFTER + original[_OFFSET + len(_BEFORE):]
            if not _patched(replacement):
                raise LocalLoginPatchError("LOCAL_LOGIN_UNSUPPORTED_PLUGIN")
        assert replacement is not None
        _atomic_replace(target, replacement, source=target, expected=source)
        if target.read_bytes() != replacement:
            raise LocalLoginPatchError(_FILE_ERROR)
        return "RESTORED" if restore else "APPLIED"
    except OSError:
        raise LocalLoginPatchError(_FILE_ERROR) from None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("install", type=Path)
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    try:
        print(apply(args.install, restore=args.restore))
    except LocalLoginPatchError as exc:
        print(str(exc))
        raise SystemExit(2)
