"""Restore the native Olivia 0.0.9.627 navigation in a private client copy."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import stat
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
OFFLINE_REQUEST_BLOCKED = "if(t.isOfflineMode)throw new Ol(e)"
OFFLINE_REQUEST_ALLOWED = "if(!1)throw new Ol(e)"
MAIL_FETCH_SKIPPED = "He(()=>{p.value||d.fetchMailList(!0)})"
MAIL_FETCH_ALLOWED = "He(()=>{d.fetchMailList(!0)})"
OFFLINE_POLL_SKIPPED = (
    "s.isOfflineMode||(s.appMode===Se.PRO?Lt().proRestoreFromApi():"
    "s.appMode===Se.LITE&&(Lt().liteStartPoll(),uo().startPolling()))"
)
OFFLINE_POLL_ALLOWED = (
    "s.appMode===Se.PRO?Lt().proRestoreFromApi():"
    "s.appMode===Se.LITE&&(s.isOfflineMode?uo().startPolling():"
    "(Lt().liteStartPoll(),uo().startPolling()))"
)
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

# The only managed 0.0.9.627 inputs this patcher may mutate.  The frontend
# "original" is the deterministic endpoint/settings-patched staging input;
# the DLL originals are the byte-exact client copies.  Sizes reject truncation
# before archive/signature inspection.  No private client bytes are distributed.
_SUPPORTED_INPUT_FINGERPRINTS = {
    "feapp": {
        "original": (
            27_769_992,
            "2abf4bc1208d3f7f39fbd2b4556c980ce5d641c75cee8863c3ca69e6029f7dcf",
        ),
        "patched": (
            27_769_978,
            "71c30b40dbbbf9d6949828425d5b093ad32aaf2d7b3c53b3f1c5a4a42643cc42",
        ),
    },
    "studio_ui": {
        "original": (
            1_297_376,
            "3756767fc01c2a1c034a56c1ae2920651f13021a1b4cc0c3ed291fc92a9728e1",
        ),
        "patched": (
            1_297_376,
            "294dcfe023c84bc83bdd531d8431bf76b2b7a1fbc0941a250ae8f4cf2ed8fa99",
        ),
    },
    "container_plugin": {
        "original": (
            498_144,
            "53b61d8e9766c5b1cf2af29ed1a4ac7985052db65c37aa1829c71416050e31d1",
        ),
        "patched": (
            498_144,
            "d78112ca218f805d437d2b03fe4c772c7cb279848dccce16570cbd466fe66ab4",
        ),
    },
}


class NativeNavigationPatchError(ValueError):
    """The private client is not an exact supported 0.0.9.627 input."""


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fingerprint(value: bytes) -> tuple[int, str]:
    return len(value), _sha256(value)


def _read_bytes(path: Path, error_code: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise NativeNavigationPatchError(error_code) from exc


def _matches_registered_state(
    values: dict[str, bytes], state: str
) -> bool:
    return all(
        _fingerprint(values[name])
        == _SUPPORTED_INPUT_FINGERPRINTS[name][state]
        for name in values
    )


def _absolute(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _is_reparse_point(path: Path) -> bool:
    try:
        value = os.lstat(path)
    except FileNotFoundError:
        return False
    attributes = getattr(value, "st_file_attributes", 0)
    return stat.S_ISLNK(value.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _validate_managed_paths(work: Path, candidates: tuple[Path, ...]) -> None:
    try:
        for component in (*reversed(work.parents), work):
            if _is_reparse_point(component):
                raise NativeNavigationPatchError("NATIVE_NAV_UNSAFE_PATH")
        for candidate in (work, *candidates):
            relative = candidate.relative_to(work)
            current = work
            for part in relative.parts:
                current = current / part
                if _is_reparse_point(current):
                    raise NativeNavigationPatchError("NATIVE_NAV_UNSAFE_PATH")
    except (OSError, ValueError) as exc:
        raise NativeNavigationPatchError("NATIVE_NAV_UNSAFE_PATH") from exc


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
                javascript = _replace_unique(
                    javascript,
                    OFFLINE_REQUEST_BLOCKED,
                    OFFLINE_REQUEST_ALLOWED,
                    "offline request",
                )
                javascript = _replace_unique(
                    javascript,
                    MAIL_FETCH_SKIPPED,
                    MAIL_FETCH_ALLOWED,
                    "offline mail fetch",
                )
                javascript = _replace_unique(
                    javascript,
                    OFFLINE_POLL_SKIPPED,
                    OFFLINE_POLL_ALLOWED,
                    "offline mail polling",
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


def _stage_write(path: Path, value: bytes, work: Path) -> Path:
    _validate_managed_paths(work, (path.parent, path))
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
    work: Path,
) -> list[BaseException]:
    failures: list[BaseException] = []
    for path, original in reversed(published):
        temporary: Path | None = None
        try:
            temporary = _stage_write(path, original, work)
            _validate_managed_paths(work, (temporary, path.parent, path))
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
                "source_size": len(originals[path]),
                "source_sha256": _sha256(originals[path]),
                "backup_size": len(originals[path]),
                "backup_sha256": _sha256(originals[path]),
                "patched_size": len(patched[path]),
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

    if work_root is None:
        raise NativeNavigationPatchError("NATIVE_NAV_UNMANAGED_ROOT")
    work = _absolute(work_root)
    root = _absolute(client_version_root)
    if (
        not work.is_dir()
        or root != work / "app" / CLIENT_VERSION
        or root.name != CLIENT_VERSION
    ):
        raise NativeNavigationPatchError("NATIVE_NAV_UNMANAGED_ROOT")
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
    backups = {
        path: path.with_name(path.name + ".native-nav.orig")
        for _, path in paths
    }
    managed_paths = (
        root,
        root / "resources",
        root / "plugins",
        studio.parent,
        container.parent,
        *(path for _, path in paths),
        *backups.values(),
    )
    _validate_managed_paths(work, managed_paths)
    for _, path in paths:
        if not path.is_file():
            raise NativeNavigationPatchError("NATIVE_NAV_INPUT_MISSING")
    backup_states = [backup.is_file() for backup in backups.values()]
    if any(backup_states) and not all(backup_states):
        raise NativeNavigationPatchError("NATIVE_NAV_BACKUPS_INCOMPLETE")
    live = {
        path: _read_bytes(path, "NATIVE_NAV_INPUT_READ_FAILED")
        for _, path in paths
    }
    live_by_name = {name: live[path] for name, path in paths}
    live_is_original = _matches_registered_state(live_by_name, "original")
    live_is_patched = _matches_registered_state(live_by_name, "patched")
    if not all(backup_states) and not live_is_original:
        raise NativeNavigationPatchError("NATIVE_NAV_UNSUPPORTED_INPUT")
    originals = (
        {
            path: _read_bytes(
                backups[path], "NATIVE_NAV_BACKUP_READ_FAILED"
            )
            for _, path in paths
        }
        if all(backup_states)
        else dict(live)
    )
    if all(backup_states) and not _matches_registered_state(
        {name: originals[path] for name, path in paths}, "original"
    ):
        raise NativeNavigationPatchError("NATIVE_NAV_BACKUP_TAMPERED")
    if all(backup_states) and not (live_is_original or live_is_patched):
        raise NativeNavigationPatchError("NATIVE_NAV_LIVE_TAMPERED")
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
    if not _matches_registered_state(
        {name: patched[path] for name, path in paths}, "patched"
    ):
        raise NativeNavigationPatchError("NATIVE_NAV_PATCH_MISMATCH")
    if all(backup_states):
        if live_is_patched:
            return _result(paths, originals, patched, "ALREADY_PATCHED")

    _validate_managed_paths(work, managed_paths)
    staged: list[Path] = []
    staged_backups: list[tuple[Path, Path]] = []
    staged_targets: list[tuple[Path, Path]] = []
    published: list[tuple[Path, bytes]] = []
    created_backups: list[Path] = []
    try:
        if not all(backup_states):
            for _, path in paths:
                temporary = _stage_write(backups[path], originals[path], work)
                staged.append(temporary)
                staged_backups.append((temporary, backups[path]))
        for _, path in paths:
            temporary = _stage_write(path, patched[path], work)
            staged.append(temporary)
            staged_targets.append((temporary, path))

        for temporary, backup in staged_backups:
            _validate_managed_paths(work, (temporary, backup.parent, backup))
            os.replace(temporary, backup)
            created_backups.append(backup)
        for temporary, path in staged_targets:
            _validate_managed_paths(work, (temporary, path.parent, path))
            os.replace(temporary, path)
            published.append((path, originals[path]))
        _validate_managed_paths(work, managed_paths)
        if not _matches_registered_state(
            {
                name: _read_bytes(path, "NATIVE_NAV_PUBLISHED_READ_FAILED")
                for name, path in paths
            },
            "patched",
        ) or not _matches_registered_state(
            {
                name: _read_bytes(
                    backups[path], "NATIVE_NAV_BACKUP_READ_FAILED"
                )
                for name, path in paths
            },
            "original",
        ):
            raise NativeNavigationPatchError("NATIVE_NAV_PUBLISHED_TAMPERED")
    except Exception as exc:
        rollback_failures = _restore_published(published, work)
        if not rollback_failures:
            for backup in created_backups:
                backup.unlink(missing_ok=True)
        if rollback_failures:
            raise NativeNavigationPatchError("NATIVE_NAV_ROLLBACK_FAILED") from exc
        if isinstance(exc, NativeNavigationPatchError):
            raise
        raise NativeNavigationPatchError("NATIVE_NAV_PUBLICATION_FAILED") from exc
    finally:
        for temporary in staged:
            temporary.unlink(missing_ok=True)

    return _result(paths, originals, patched, "PATCHED")
