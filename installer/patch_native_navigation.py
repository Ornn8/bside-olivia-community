"""Restore the native Olivia 0.0.9.627 navigation in a private client copy."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import uuid
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
OFFLINE_CALL_PATCH = bytes((0x33, 0xC0, 0x90, 0x90, 0x90, 0x90))
STUDIO_SIGNATURES = (
    bytes.fromhex("CB E8 D2 37 08 00 EB 1E FF 15 B2 EC 08 00 48 8D 8F A8"),
    bytes.fromhex("CB E8 72 34 08 00 EB 1E FF 15 52 E9 08 00 48 8D 8F A8"),
    bytes.fromhex("CB E8 B2 1F 08 00 EB 2B FF 15 92 D4 08 00 84 C0 75 14"),
    bytes.fromhex("CB E8 FF 1D 08 00 EB 1C FF 15 DF D2 08 00 48 8D 4F 38"),
)
CONTAINER_SIGNATURE = bytes.fromhex(
    "48 8B DA 48 8B F9 FF 15 61 A4 04 00 84 C0 0F 85"
)


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


def _unique_offset(source: bytes, signature: bytes, label: str) -> int:
    offsets: list[int] = []
    start = 0
    while True:
        offset = source.find(signature, start)
        if offset < 0:
            break
        offsets.append(offset)
        start = offset + 1
    if len(offsets) != 1:
        raise NativeNavigationPatchError(
            f"{label} signature must occur exactly once; found {len(offsets)}"
        )
    return offsets[0]


def _patched_dll(
    source: bytes,
    signatures: tuple[bytes, ...],
    call_offset: int,
    label: str,
) -> bytes:
    offsets = [
        _unique_offset(source, signature, f"{label} #{index}")
        for index, signature in enumerate(signatures, start=1)
    ]
    patched = bytearray(source)
    for offset in offsets:
        start = offset + call_offset
        patched[start : start + len(OFFLINE_CALL_PATCH)] = OFFLINE_CALL_PATCH
    value = bytes(patched)
    for index, (signature, offset) in enumerate(
        zip(signatures, offsets, strict=True),
        start=1,
    ):
        expected = (
            signature[:call_offset]
            + OFFLINE_CALL_PATCH
            + signature[call_offset + len(OFFLINE_CALL_PATCH) :]
        )
        if signature in value or value[offset : offset + len(signature)] != expected:
            raise NativeNavigationPatchError(
                f"{label} #{index} patch verification failed"
            )
    return value


def _stage_write(path: Path, value: bytes) -> Path:
    temporary = path.with_name(
        path.name + f".native-nav-{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.read_bytes() != value:
            raise NativeNavigationPatchError("staged file verification failed")
        return temporary
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _restore_published(
    published: list[tuple[Path, bytes]],
) -> list[BaseException]:
    failures: list[BaseException] = []
    for path, original in reversed(published):
        temporary: Path | None = None
        try:
            temporary = _stage_write(path, original)
            os.replace(temporary, path)
        except BaseException as exc:
            failures.append(exc)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return failures


def _result(
    paths: tuple[tuple[str, Path], ...],
    originals: dict[Path, bytes],
    patched: dict[Path, bytes],
    status: str,
) -> dict[str, object]:
    return {
        "status": status,
        "client_version": CLIENT_VERSION,
        "files": {
            name: {
                "path": str(path),
                "backup_path": str(
                    path.with_name(path.name + ".native-nav.orig")
                ),
                "source_sha256": _sha256(originals[path]),
                "backup_sha256": _sha256(originals[path]),
                "patched_sha256": _sha256(patched[path]),
            }
            for name, path in paths
        },
    }


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
    studio = root / "plugins" / "Studio" / "NutStudioUI.dll"
    container = (
        root / "plugins" / "Container" / "NutContainerPlugin.dll"
    )
    paths = (
        ("feapp", feapp),
        ("studio_ui", studio),
        ("container_plugin", container),
    )
    for _, path in paths:
        if not path.is_file():
            raise FileNotFoundError(path)

    backups = {
        path: path.with_name(path.name + ".native-nav.orig")
        for _, path in paths
    }
    backup_states = [backup.is_file() for backup in backups.values()]
    if any(backup_states) and not all(backup_states):
        raise NativeNavigationPatchError(
            "native navigation backups are incomplete"
        )
    live = {path: path.read_bytes() for _, path in paths}
    originals = (
        {path: backups[path].read_bytes() for _, path in paths}
        if all(backup_states)
        else dict(live)
    )
    patched = {
        feapp: _patched_feapp(originals[feapp]),
        studio: _patched_dll(
            originals[studio],
            STUDIO_SIGNATURES,
            8,
            "NutStudioUI.dll offline call",
        ),
        container: _patched_dll(
            originals[container],
            (CONTAINER_SIGNATURE,),
            6,
            "NutContainerPlugin.dll lite-bar call",
        ),
    }
    if all(backup_states):
        if all(live[path] == patched[path] for _, path in paths):
            return _result(paths, originals, patched, "ALREADY_PATCHED")
        if not all(live[path] == originals[path] for _, path in paths):
            raise NativeNavigationPatchError(
                "private client files do not match original backups"
            )

    staged: list[Path] = []
    staged_backups: list[tuple[Path, Path]] = []
    staged_targets: list[tuple[Path, Path]] = []
    published: list[tuple[Path, bytes]] = []
    created_backups: list[Path] = []
    try:
        if not all(backup_states):
            for _, path in paths:
                temporary = _stage_write(backups[path], originals[path])
                staged.append(temporary)
                staged_backups.append((temporary, backups[path]))
        for _, path in paths:
            temporary = _stage_write(path, patched[path])
            staged.append(temporary)
            staged_targets.append((temporary, path))

        for temporary, backup in staged_backups:
            os.replace(temporary, backup)
            created_backups.append(backup)
        for temporary, path in staged_targets:
            os.replace(temporary, path)
            published.append((path, originals[path]))
    except Exception as exc:
        rollback_failures = _restore_published(published)
        if not rollback_failures:
            for backup in created_backups:
                backup.unlink(missing_ok=True)
        if rollback_failures:
            raise NativeNavigationPatchError(
                "native navigation rollback failed"
            ) from exc
        raise
    finally:
        for temporary in staged:
            temporary.unlink(missing_ok=True)

    return _result(paths, originals, patched, "PATCHED")
