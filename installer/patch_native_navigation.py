"""Restore the native Olivia 0.0.9.627 navigation in a private client copy."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import uuid
import zipfile


CLIENT_VERSION = "0.0.9.627"
COMPATIBILITY_MANIFEST_NAME = "native-navigation-compatibility.json"
_MANIFEST_SCHEMA = "olivia.native-navigation-compatibility.v1"
_MAX_MANIFEST_BYTES = 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_EXPECTED_FILES = {
    "feapp": "resources/feapp.dat",
    "studio_ui": "plugins/Studio/NutStudioUI.dll",
    "container_plugin": "plugins/Container/NutContainerPlugin.dll",
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
    except OSError:
        raise NativeNavigationPatchError(error_code) from None


def _matches_registered_state(
    values: dict[str, bytes], specs: dict[str, dict[str, object]], state: str
) -> bool:
    return all(
        _matches_registered_file(specs[name], values[name], state)
        for name in values
    )


def _matches_registered_file(
    spec: dict[str, object], value: bytes, state: str
) -> bool:
    fingerprints = spec["fingerprints"]
    assert isinstance(fingerprints, dict)
    return _fingerprint(value) == fingerprints[state]


def _registered_file_state(spec: dict[str, object], value: bytes) -> str | None:
    for state in ("original", "patched"):
        if _matches_registered_file(spec, value, state):
            return state
    return None


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
    except (OSError, ValueError):
        raise NativeNavigationPatchError("NATIVE_NAV_UNSAFE_PATH") from None


def _object_keys(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError
    return value


def _file_fingerprint(value: object) -> tuple[int, str]:
    record = _object_keys(value, {"size_bytes", "sha256"})
    size = record["size_bytes"]
    digest = record["sha256"]
    if (
        type(size) is not int
        or not 1 <= size <= 1024 * 1024 * 1024
        or not isinstance(digest, str)
        or _SHA256_RE.fullmatch(digest) is None
    ):
        raise ValueError
    return size, digest


def _replacement_id(value: object) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ValueError
    return value


def _text_replacements(value: object) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ValueError
    result: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for raw in value:
        item = _object_keys(raw, {"id", "before", "after"})
        identifier = _replacement_id(item["id"])
        before = item["before"]
        after = item["after"]
        if (
            identifier in seen
            or not isinstance(before, str)
            or not isinstance(after, str)
            or not before
            or before == after
            or len(before.encode("utf-8")) > 16 * 1024
            or len(after.encode("utf-8")) > 16 * 1024
        ):
            raise ValueError
        seen.add(identifier)
        result.append((identifier, before, after))
    return tuple(result)


def _binary_replacements(
    value: object,
) -> tuple[tuple[str, bytes, int, bytes], ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ValueError
    result: list[tuple[str, bytes, int, bytes]] = []
    seen: set[str] = set()
    for raw in value:
        item = _object_keys(
            raw,
            {"id", "signature_hex", "patch_offset", "replacement_hex"},
        )
        identifier = _replacement_id(item["id"])
        signature_hex = item["signature_hex"]
        replacement_hex = item["replacement_hex"]
        offset = item["patch_offset"]
        if (
            identifier in seen
            or not isinstance(signature_hex, str)
            or not isinstance(replacement_hex, str)
            or type(offset) is not int
            or offset < 0
        ):
            raise ValueError
        signature = bytes.fromhex(signature_hex)
        replacement = bytes.fromhex(replacement_hex)
        if (
            not signature
            or not replacement
            or len(signature) > 1024
            or len(replacement) > 256
            or offset + len(replacement) > len(signature)
        ):
            raise ValueError
        seen.add(identifier)
        result.append((identifier, signature, offset, replacement))
    return tuple(result)


def parse_compatibility_manifest(
    value: object,
) -> dict[str, dict[str, object]]:
    """Validate private compatibility metadata without persisting its bytes."""

    manifest = _object_keys(
        value,
        {"schema_version", "client_version", "files"},
    )
    if (
        manifest["schema_version"] != _MANIFEST_SCHEMA
        or manifest["client_version"] != CLIENT_VERSION
        or not isinstance(manifest["files"], list)
        or len(manifest["files"]) != len(_EXPECTED_FILES)
    ):
        raise ValueError
    specs: dict[str, dict[str, object]] = {}
    for raw in manifest["files"]:
        if not isinstance(raw, dict):
            raise ValueError
        identifier = raw.get("id")
        relative = raw.get("relative_path")
        if (
            not isinstance(identifier, str)
            or identifier in specs
            or _EXPECTED_FILES.get(identifier) != relative
        ):
            raise ValueError
        common = {"id", "relative_path", "original", "patched"}
        original = _file_fingerprint(raw.get("original"))
        patched = _file_fingerprint(raw.get("patched"))
        if original == patched:
            raise ValueError
        spec: dict[str, object] = {
            "relative_path": relative,
            "fingerprints": {
                "original": original,
                "patched": patched,
            },
        }
        if identifier == "feapp":
            if set(raw) != common | {"archive_member", "text_replacements"}:
                raise ValueError
            member = raw["archive_member"]
            if (
                not isinstance(member, str)
                or not member
                or "\\" in member
                or member.startswith("/")
                or any(part in {"", ".", ".."} for part in member.split("/"))
                or len(member.encode("utf-8")) > 512
            ):
                raise ValueError
            spec["archive_member"] = member
            spec["text_replacements"] = _text_replacements(
                raw["text_replacements"]
            )
        else:
            if set(raw) != common | {"binary_replacements"}:
                raise ValueError
            spec["binary_replacements"] = _binary_replacements(
                raw["binary_replacements"]
            )
        specs[identifier] = spec
    if set(specs) != set(_EXPECTED_FILES):
        raise ValueError
    return specs


def _load_compatibility_manifest(
    work: Path,
) -> dict[str, dict[str, object]]:
    path = work / "local_backend" / "installer" / COMPATIBILITY_MANIFEST_NAME
    if not path.is_file():
        raise NativeNavigationPatchError("NATIVE_NAV_MANIFEST_REQUIRED")
    _validate_managed_paths(work, (path.parent.parent, path.parent, path))
    try:
        if path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise ValueError
        return parse_compatibility_manifest(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise NativeNavigationPatchError("NATIVE_NAV_MANIFEST_INVALID") from None


def _replace_unique(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise NativeNavigationPatchError(
            f"{label} signature must occur exactly once; found {count}"
        )
    return text.replace(old, new, 1)


def _patched_feapp(source: bytes, spec: dict[str, object]) -> bytes:
    member = spec["archive_member"]
    replacements = spec["text_replacements"]
    assert isinstance(member, str)
    assert isinstance(replacements, tuple)
    try:
        with zipfile.ZipFile(io.BytesIO(source)) as archive:
            infos = archive.infolist()
            if sum(info.filename == member for info in infos) != 1:
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
            if info.filename == member:
                try:
                    javascript = payload.decode("utf-8")
                except UnicodeError as exc:
                    raise NativeNavigationPatchError(
                        "supported frontend bundle is not UTF-8"
                    ) from exc
                for identifier, before, after in replacements:
                    javascript = _replace_unique(
                        javascript,
                        before,
                        after,
                        f"frontend {identifier}",
                    )
                payload = javascript.encode("utf-8")
            archive.writestr(info, payload)

    patched = output.getvalue()
    try:
        with zipfile.ZipFile(io.BytesIO(patched)) as archive:
            javascript = archive.read(member).decode("utf-8")
    except (OSError, UnicodeError, zipfile.BadZipFile, KeyError) as exc:
        raise NativeNavigationPatchError(
            "patched frontend archive verification failed"
        ) from exc
    if any(after not in javascript or before in javascript for _, before, after in replacements):
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


def _patched_binary(
    source: bytes,
    replacements: tuple[tuple[str, bytes, int, bytes], ...],
    label: str,
) -> bytes:
    offsets = {
        identifier: _unique_offset(source, signature, f"{label} {identifier}")
        for identifier, signature, _, _ in replacements
    }
    patched = bytearray(source)
    for identifier, _, offset, replacement in replacements:
        start = offsets[identifier] + offset
        patched[start : start + len(replacement)] = replacement
    value = bytes(patched)
    for identifier, signature, patch_offset, replacement in replacements:
        offset = offsets[identifier]
        expected = (
            signature[:patch_offset]
            + replacement
            + signature[patch_offset + len(replacement) :]
        )
        if signature in value or value[offset : offset + len(signature)] != expected:
            raise NativeNavigationPatchError(
                f"{label} {identifier} patch verification failed"
            )
    return value


def _unlink_paths(paths: list[Path] | tuple[Path, ...]) -> list[BaseException]:
    failures: list[BaseException] = []
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except BaseException as exc:
            failures.append(exc)
    return failures


def _cleanup_orphan_staging(paths: tuple[Path, ...], work: Path) -> None:
    orphans: list[Path] = []
    try:
        for path in paths:
            orphans.extend(path.parent.glob(path.name + ".native-nav-*.tmp"))
        for orphan in orphans:
            _validate_managed_paths(work, (orphan.parent, orphan))
    except (OSError, NativeNavigationPatchError):
        raise NativeNavigationPatchError("NATIVE_NAV_CLEANUP_FAILED") from None
    if _unlink_paths(orphans):
        raise NativeNavigationPatchError("NATIVE_NAV_CLEANUP_FAILED")


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
    except BaseException as exc:
        if _unlink_paths((temporary,)):
            raise NativeNavigationPatchError("NATIVE_NAV_CLEANUP_FAILED") from None
        if isinstance(exc, NativeNavigationPatchError):
            raise
        raise NativeNavigationPatchError("NATIVE_NAV_STAGE_FAILED") from None


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
                failures.extend(_unlink_paths((temporary,)))
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
    specs = _load_compatibility_manifest(work)
    feapp = root.joinpath(*str(specs["feapp"]["relative_path"]).split("/"))
    studio = root.joinpath(*str(specs["studio_ui"]["relative_path"]).split("/"))
    container = root.joinpath(
        *str(specs["container_plugin"]["relative_path"]).split("/")
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
    _cleanup_orphan_staging(
        tuple(path for _, path in paths) + tuple(backups.values()),
        work,
    )
    for _, path in paths:
        if not path.is_file():
            raise NativeNavigationPatchError("NATIVE_NAV_INPUT_MISSING")
    backup_states = {path: backups[path].is_file() for _, path in paths}
    has_any_backup = any(backup_states.values())
    has_all_backups = all(backup_states.values())
    live = {
        path: _read_bytes(path, "NATIVE_NAV_INPUT_READ_FAILED")
        for _, path in paths
    }
    live_by_name = {name: live[path] for name, path in paths}
    live_states = {
        name: _registered_file_state(specs[name], value)
        for name, value in live_by_name.items()
    }
    live_is_original = all(state == "original" for state in live_states.values())
    live_is_patched = all(state == "patched" for state in live_states.values())
    if not has_any_backup and not live_is_original:
        raise NativeNavigationPatchError("NATIVE_NAV_UNSUPPORTED_INPUT")
    # Recovery invariant: every backup is published before the first target.
    # A partial backup set is therefore safe only with all-original live files;
    # complete trusted backups can resume any original/patched live-file mix.
    if has_any_backup and not has_all_backups:
        for name, path in paths:
            if backup_states[path] and not _matches_registered_file(
                specs[name],
                _read_bytes(
                    backups[path], "NATIVE_NAV_BACKUP_READ_FAILED"
                ),
                "original",
            ):
                raise NativeNavigationPatchError("NATIVE_NAV_BACKUP_TAMPERED")
        if not live_is_original:
            raise NativeNavigationPatchError("NATIVE_NAV_RECOVERY_UNSAFE")
    originals = (
        {
            path: _read_bytes(
                backups[path], "NATIVE_NAV_BACKUP_READ_FAILED"
            )
            for _, path in paths
        }
        if has_all_backups
        else dict(live)
    )
    if has_all_backups and not _matches_registered_state(
        {name: originals[path] for name, path in paths}, specs, "original"
    ):
        raise NativeNavigationPatchError("NATIVE_NAV_BACKUP_TAMPERED")
    if has_all_backups and any(state is None for state in live_states.values()):
        raise NativeNavigationPatchError("NATIVE_NAV_LIVE_TAMPERED")
    patched = {
        feapp: _patched_feapp(originals[feapp], specs["feapp"]),
        studio: _patched_binary(
            originals[studio],
            specs["studio_ui"]["binary_replacements"],
            "NutStudioUI.dll offline call",
        ),
        container: _patched_binary(
            originals[container],
            specs["container_plugin"]["binary_replacements"],
            "NutContainerPlugin.dll lite-bar call",
        ),
    }
    if not _matches_registered_state(
        {name: patched[path] for name, path in paths}, specs, "patched"
    ):
        raise NativeNavigationPatchError("NATIVE_NAV_PATCH_MISMATCH")
    if has_all_backups:
        if live_is_patched:
            return _result(paths, originals, patched, "ALREADY_PATCHED")

    _validate_managed_paths(work, managed_paths)
    staged: list[Path] = []
    staged_backups: list[tuple[Path, Path]] = []
    staged_targets: list[tuple[Path, Path]] = []
    published: list[tuple[Path, bytes]] = []
    created_backups: list[Path] = []
    try:
        if not has_all_backups:
            for _, path in paths:
                if not backup_states[path]:
                    temporary = _stage_write(backups[path], originals[path], work)
                    staged.append(temporary)
                    staged_backups.append((temporary, backups[path]))
        for name, path in paths:
            if live_states[name] != "patched":
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
            specs,
            "patched",
        ) or not _matches_registered_state(
            {
                name: _read_bytes(
                    backups[path], "NATIVE_NAV_BACKUP_READ_FAILED"
                )
                for name, path in paths
            },
            specs,
            "original",
        ):
            raise NativeNavigationPatchError("NATIVE_NAV_PUBLISHED_TAMPERED")
    except BaseException as exc:
        rollback_failures = _restore_published(published, work)
        cleanup_failures: list[BaseException] = []
        if not rollback_failures:
            cleanup_failures.extend(_unlink_paths(created_backups))
        cleanup_failures.extend(_unlink_paths(staged))
        if rollback_failures:
            raise NativeNavigationPatchError("NATIVE_NAV_ROLLBACK_FAILED") from None
        if cleanup_failures:
            raise NativeNavigationPatchError("NATIVE_NAV_CLEANUP_FAILED") from None
        if isinstance(exc, NativeNavigationPatchError):
            raise
        if not isinstance(exc, Exception):
            raise
        raise NativeNavigationPatchError("NATIVE_NAV_PUBLICATION_FAILED") from None

    if _unlink_paths(staged):
        raise NativeNavigationPatchError("NATIVE_NAV_CLEANUP_FAILED")

    return _result(paths, originals, patched, "PATCHED")
