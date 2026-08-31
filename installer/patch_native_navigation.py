"""Restore the native Olivia 0.0.9.627 navigation in a private client copy."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import zipfile


CLIENT_VERSION = "0.0.9.627"
MAIN_MEMBER = "assets/main-31595bd3.js"
MAILBOX_DISABLED = "N3=!1,Ss=!1,wa=({onComplete"
MAILBOX_ENABLED = "N3=!0,Ss=!1,wa=({onComplete"
OFFLINE_WIDGETS_DISABLED = (
    "e.isOfflineMode&&(l.value.mailWidget!==!1&&(l.value.mailWidget=!1),"
    "l.value.musicWidget!==!1&&(l.value.musicWidget=!1))"
)
OFFLINE_WIDGETS_ENABLED = "l.value.mailWidget=!0,l.value.musicWidget=!0"


class NativeNavigationPatchError(ValueError):
    """The private client is not an exact supported 0.0.9.627 input."""


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _replace_unique(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise NativeNavigationPatchError(
            f"{label} signature must occur exactly once; found {count}"
        )
    return text.replace(old, new, 1)


def _patched_feapp(source: bytes) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(source)) as archive:
            infos = archive.infolist()
            if sum(info.filename == MAIN_MEMBER for info in infos) != 1:
                raise NativeNavigationPatchError(
                    "supported frontend bundle must occur exactly once"
                )
            members = [(info, archive.read(info)) for info in infos]
            comment = archive.comment
    except (OSError, zipfile.BadZipFile) as exc:
        raise NativeNavigationPatchError("feapp.dat is not a valid archive") from exc

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.comment = comment
        for info, payload in members:
            if info.filename == MAIN_MEMBER:
                try:
                    javascript = payload.decode("utf-8")
                except UnicodeError as exc:
                    raise NativeNavigationPatchError(
                        "supported frontend bundle is not UTF-8"
                    ) from exc
                javascript = _replace_unique(
                    javascript,
                    MAILBOX_DISABLED,
                    MAILBOX_ENABLED,
                    "mailbox entry",
                )
                javascript = _replace_unique(
                    javascript,
                    OFFLINE_WIDGETS_DISABLED,
                    OFFLINE_WIDGETS_ENABLED,
                    "offline widgets",
                )
                payload = javascript.encode("utf-8")
            archive.writestr(info, payload)

    patched = output.getvalue()
    try:
        with zipfile.ZipFile(io.BytesIO(patched)) as archive:
            javascript = archive.read(MAIN_MEMBER).decode("utf-8")
    except (OSError, UnicodeError, zipfile.BadZipFile, KeyError) as exc:
        raise NativeNavigationPatchError(
            "patched frontend archive verification failed"
        ) from exc
    if (
        MAILBOX_ENABLED not in javascript
        or MAILBOX_DISABLED in javascript
        or OFFLINE_WIDGETS_ENABLED not in javascript
        or OFFLINE_WIDGETS_DISABLED in javascript
    ):
        raise NativeNavigationPatchError(
            "patched frontend archive verification failed"
        )
    return patched


def _atomic_write(path: Path, value: bytes) -> None:
    temporary = path.with_name(path.name + ".native-nav.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def patch_native_navigation(
    client_version_root: str | os.PathLike[str],
    *,
    work_root: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    """Patch native widget visibility without changing the startup route."""

    root = Path(client_version_root).expanduser().resolve()
    if root.name != CLIENT_VERSION:
        raise NativeNavigationPatchError("unsupported client version")
    if work_root is not None and not Path(work_root).expanduser().resolve().is_dir():
        raise FileNotFoundError(work_root)
    feapp = root / "resources" / "feapp.dat"
    if not feapp.is_file():
        raise FileNotFoundError(feapp)

    source = feapp.read_bytes()
    patched = _patched_feapp(source)
    backup = feapp.with_name(feapp.name + ".native-nav.orig")
    created_backup = False
    try:
        if backup.exists():
            if backup.read_bytes() != source:
                raise NativeNavigationPatchError(
                    "frontend backup does not match the original input"
                )
        else:
            _atomic_write(backup, source)
            created_backup = True
        _atomic_write(feapp, patched)
    except Exception:
        if created_backup:
            backup.unlink(missing_ok=True)
        raise

    return {
        "status": "PATCHED",
        "client_version": CLIENT_VERSION,
        "files": {
            "feapp": {
                "path": str(feapp),
                "backup_path": str(backup),
                "source_sha256": _sha256(source),
                "backup_sha256": _sha256(backup.read_bytes()),
                "patched_sha256": _sha256(patched),
            }
        },
    }
