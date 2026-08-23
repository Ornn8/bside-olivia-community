"""Add a bounded companion panel to the supported Olivia settings view.

The patch only changes a staged ``feapp.dat`` archive. It keeps the client main
module byte-for-byte intact, inserts one repository-owned local script into
``index.html``, and rolls the archive back if any validation step fails.
"""

from __future__ import annotations

import hashlib
import html
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import tempfile
from urllib.parse import urlsplit
import zipfile

from original_client_settings_ui import (
    BOOTSTRAP_JAVASCRIPT,
    SETTINGS_UI_VERSION,
)


INDEX_MEMBER = "index.html"
MAIN_MODULE_MEMBER = "assets/main-917d29fc.js"
BOOTSTRAP_MEMBER = "assets/olivia-companion-settings.js"
PATCH_MARKER = "data-olivia-companion-settings"
PATCH_SCHEMA_VERSION = "p03.original-settings-shell.v1"
MAX_ARCHIVE_MEMBERS = 100_000
MAX_TEXT_MEMBER_BYTES = 64 * 1024 * 1024

_MODULE_SCRIPT_RE = re.compile(
    r"<script\b"
    r"(?=[^>]*\btype\s*=\s*([\"'])module\1)"
    r"(?=[^>]*\bsrc\s*=\s*([\"'])\./assets/main-917d29fc\.js\2)"
    r"[^>]*>\s*</script>",
    flags=re.IGNORECASE,
)
_MARKER_TAG_RE = re.compile(
    r"<script\b[^>]*\bdata-olivia-companion-settings="
    r"([\"'])p03\.original-settings-shell\.v1\1[^>]*>",
    flags=re.IGNORECASE,
)
_API_BASE_RE = re.compile(
    r"\bdata-api-base=([\"'])(?P<value>[^\"']+)\1",
    flags=re.IGNORECASE,
)
_BOOTSTRAP_SOURCE_RE = re.compile(
    r"\bsrc=([\"'])\./assets/olivia-companion-settings\.js\1",
    flags=re.IGNORECASE,
)
_UI_VERSION_RE = re.compile(
    r"\bdata-ui-version=([\"'])(?P<value>[^\"']+)\1",
    flags=re.IGNORECASE,
)


class CompanionSettingsPatchError(RuntimeError):
    """Stable archive patch failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
    except OSError as exc:
        raise CompanionSettingsPatchError("COMPANION_ARCHIVE_UNREADABLE") from exc
    return digest.hexdigest()


def validate_api_base(value: str | None) -> str:
    if not value:
        raise CompanionSettingsPatchError("COMPANION_API_BASE_REQUIRED")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise CompanionSettingsPatchError("COMPANION_API_BASE_INVALID") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or port is None
        or not 1 <= port <= 65535
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise CompanionSettingsPatchError("COMPANION_API_BASE_INVALID")
    return f"http://{parsed.hostname}:{port}/"


def _safe_member_path(root: Path, member_name: str) -> Path:
    normalized = member_name.replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(normalized)
    if (
        not normalized
        or "\x00" in normalized
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise CompanionSettingsPatchError("COMPANION_ARCHIVE_UNSAFE")
    target = (root / Path(*posix.parts)).resolve()
    if os.path.commonpath([str(root), str(target)]) != str(root):
        raise CompanionSettingsPatchError("COMPANION_ARCHIVE_UNSAFE")
    return target


def _validate_archive(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if not members:
                raise CompanionSettingsPatchError("COMPANION_ARCHIVE_EMPTY")
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise CompanionSettingsPatchError("COMPANION_ARCHIVE_TOO_MANY_MEMBERS")
            for info in members:
                _safe_member_path(path.parent.resolve(), info.filename)
    except CompanionSettingsPatchError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise CompanionSettingsPatchError("COMPANION_ARCHIVE_INVALID") from exc


def _safe_extract(archive: zipfile.ZipFile, root: Path) -> None:
    for info in archive.infolist():
        target = _safe_member_path(root, info.filename)
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with archive.open(info) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
        except OSError as exc:
            raise CompanionSettingsPatchError("COMPANION_ARCHIVE_UNREADABLE") from exc


def _member_hashes(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                result[info.filename] = hashlib.sha256(archive.read(info)).hexdigest()
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise CompanionSettingsPatchError("COMPANION_ARCHIVE_UNREADABLE") from exc
    return result


def _atomic_copy(source: Path, destination: Path) -> None:
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    except OSError as exc:
        raise CompanionSettingsPatchError("COMPANION_BACKUP_FAILED") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _ensure_backup(feapp: Path) -> Path:
    backup = Path(str(feapp) + ".companion.orig")
    if backup.exists():
        _validate_archive(backup)
    else:
        _atomic_copy(feapp, backup)
    return backup


def _read_text(path: Path, code: str) -> str:
    try:
        if path.stat().st_size > MAX_TEXT_MEMBER_BYTES:
            raise CompanionSettingsPatchError(code)
        return path.read_text(encoding="utf-8")
    except CompanionSettingsPatchError:
        raise
    except (OSError, UnicodeError) as exc:
        raise CompanionSettingsPatchError(code) from exc


def _managed_tag(api_base: str) -> str:
    return (
        '<script src="./assets/olivia-companion-settings.js" '
        f'{PATCH_MARKER}="{PATCH_SCHEMA_VERSION}" '
        f'data-ui-version="{SETTINGS_UI_VERSION}" '
        f'data-api-base="{html.escape(api_base, quote=True)}"></script>'
    )


def _patch_existing(
    source: str,
    bootstrap: Path,
    api_base: str,
) -> str:
    if not bootstrap.is_file():
        raise CompanionSettingsPatchError("COMPANION_PATCH_INCOMPLETE")
    tag = _MARKER_TAG_RE.search(source)
    api_match = _API_BASE_RE.search(tag.group(0) if tag else "")
    source_match = _BOOTSTRAP_SOURCE_RE.search(tag.group(0) if tag else "")
    if not tag or not api_match or not source_match:
        raise CompanionSettingsPatchError("COMPANION_PATCH_INCOMPLETE")
    if html.unescape(api_match.group("value")) != api_base:
        raise CompanionSettingsPatchError("COMPANION_API_BASE_MISMATCH")

    current_script = _read_text(
        bootstrap,
        "COMPANION_BOOTSTRAP_UNREADABLE",
    )
    ui_match = _UI_VERSION_RE.search(tag.group(0))
    if (
        current_script == BOOTSTRAP_JAVASCRIPT
        and ui_match
        and html.unescape(ui_match.group("value")) == SETTINGS_UI_VERSION
    ):
        return "ALREADY_PATCHED"

    managed = _managed_tag(api_base)
    updated = source[: tag.start()] + managed + source[tag.end() :]
    try:
        (bootstrap.parent.parent / INDEX_MEMBER).write_text(
            updated,
            encoding="utf-8",
        )
        bootstrap.write_text(BOOTSTRAP_JAVASCRIPT, encoding="utf-8")
    except OSError as exc:
        raise CompanionSettingsPatchError("COMPANION_PATCH_WRITE_FAILED") from exc
    return "PATCHED"


def _patch_index(root: Path, api_base: str) -> str:
    index = root / INDEX_MEMBER
    main_module = root / MAIN_MODULE_MEMBER
    bootstrap = root / BOOTSTRAP_MEMBER
    if not index.is_file():
        raise CompanionSettingsPatchError("COMPANION_INDEX_MISSING")
    if not main_module.is_file():
        raise CompanionSettingsPatchError("COMPANION_MAIN_MODULE_MISSING")
    source = _read_text(index, "COMPANION_INDEX_UNREADABLE")

    marker_count = source.count(PATCH_MARKER)
    if marker_count == 1:
        return _patch_existing(source, bootstrap, api_base)
    if marker_count or bootstrap.exists():
        raise CompanionSettingsPatchError("COMPANION_PATCH_INCOMPLETE")

    matches = list(_MODULE_SCRIPT_RE.finditer(source))
    if len(matches) != 1:
        raise CompanionSettingsPatchError("COMPANION_MODULE_ANCHOR_INVALID")
    match = matches[0]
    patched = (
        source[: match.end()]
        + "\n  "
        + _managed_tag(api_base)
        + source[match.end() :]
    )
    if patched.count(PATCH_MARKER) != 1:
        raise CompanionSettingsPatchError("COMPANION_PATCH_VERIFICATION_FAILED")
    bootstrap.parent.mkdir(parents=True, exist_ok=True)
    try:
        index.write_text(patched, encoding="utf-8")
        bootstrap.write_text(BOOTSTRAP_JAVASCRIPT, encoding="utf-8")
    except OSError as exc:
        raise CompanionSettingsPatchError("COMPANION_PATCH_WRITE_FAILED") from exc
    return "PATCHED"


def _repack(root: Path, destination: Path) -> None:
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
        _validate_archive(temporary)
        os.replace(temporary, destination)
    except CompanionSettingsPatchError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise CompanionSettingsPatchError("COMPANION_REPACK_FAILED") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _verify_archive(
    path: Path,
    *,
    api_base: str,
    original_hashes: dict[str, str],
) -> None:
    patched_hashes = _member_hashes(path)
    if set(patched_hashes) != set(original_hashes) | {BOOTSTRAP_MEMBER}:
        raise CompanionSettingsPatchError("COMPANION_PATCH_VERIFICATION_FAILED")
    for name, digest in original_hashes.items():
        if name in {INDEX_MEMBER, BOOTSTRAP_MEMBER}:
            continue
        if patched_hashes.get(name) != digest:
            raise CompanionSettingsPatchError("COMPANION_PATCH_VERIFICATION_FAILED")
    try:
        with zipfile.ZipFile(path) as archive:
            index = archive.read(INDEX_MEMBER).decode("utf-8")
            bootstrap = archive.read(BOOTSTRAP_MEMBER).decode("utf-8")
    except (KeyError, OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise CompanionSettingsPatchError("COMPANION_PATCH_VERIFICATION_FAILED") from exc
    required = (
        'data-olivia-companion-settings="p03.original-settings-shell.v1"',
        f'data-ui-version="{SETTINGS_UI_VERSION}"',
        f'data-api-base="{html.escape(api_base, quote=True)}"',
    )
    bootstrap_required = (
        'const STATUS_PATH = "/toy/companion/status";',
        'const MEMORY_PATH = "/toy/companion/memory";',
        'const PRIVATE_WORLD_PATH = "/toy/companion/private-world";',
        'const CANDIDATES_PATH = "/toy/companion/private-world/candidates";',
        "data-olivia-companion-settings-root",
        "panel.dataset.oliviaCompanionPanel",
        "长期记忆",
        "私人世界",
        "待确认的关系建议",
        "new MutationObserver",
        "replaceChildren",
    )
    forbidden = (
        "<iframe",
        "window.open",
        "innerHTML",
        'method: "POST"',
        'method: "PUT"',
        'method: "DELETE"',
        "eval(",
    )
    if (
        any(value not in index for value in required)
        or any(value not in bootstrap for value in bootstrap_required)
        or any(value in bootstrap for value in forbidden)
        or bootstrap != BOOTSTRAP_JAVASCRIPT
    ):
        raise CompanionSettingsPatchError("COMPANION_PATCH_VERIFICATION_FAILED")


def patch_companion_settings(
    feapp_path: str | os.PathLike[str],
    api_base: str | None,
    *,
    work_root: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """Patch one staged client archive and preserve every existing asset."""

    feapp = Path(feapp_path).expanduser().resolve()
    normalized_api_base = validate_api_base(api_base)
    if not feapp.is_file():
        raise CompanionSettingsPatchError("COMPANION_ARCHIVE_NOT_FOUND")
    _validate_archive(feapp)
    original_hashes = _member_hashes(feapp)
    source_sha256 = sha256_file(feapp)
    backup = _ensure_backup(feapp)
    backup_sha256 = sha256_file(backup)
    sandbox = Path(work_root or feapp.parent).expanduser().resolve()
    if not sandbox.is_dir():
        raise CompanionSettingsPatchError("COMPANION_WORK_ROOT_NOT_FOUND")

    with tempfile.TemporaryDirectory(
        prefix=".patch-companion-settings-",
        dir=sandbox,
    ) as name:
        temporary_root = Path(name)
        rollback = temporary_root / "rollback.dat"
        _atomic_copy(feapp, rollback)
        try:
            unpacked = temporary_root / "unpacked"
            with zipfile.ZipFile(feapp) as archive:
                _safe_extract(archive, unpacked)
            status = _patch_index(unpacked, normalized_api_base)
            if status == "PATCHED":
                output = temporary_root / "patched.dat"
                _repack(unpacked, output)
                os.replace(output, feapp)
            _verify_archive(
                feapp,
                api_base=normalized_api_base,
                original_hashes=original_hashes,
            )
        except Exception:
            _atomic_copy(rollback, feapp)
            raise

    return {
        "schema_version": PATCH_SCHEMA_VERSION,
        "ui_version": SETTINGS_UI_VERSION,
        "status": status,
        "source_sha256": source_sha256,
        "backup_sha256": backup_sha256,
        "patched_sha256": sha256_file(feapp),
        "backup_name": backup.name,
    }


__all__ = [
    "BOOTSTRAP_MEMBER",
    "CompanionSettingsPatchError",
    "INDEX_MEMBER",
    "MAIN_MODULE_MEMBER",
    "PATCH_MARKER",
    "PATCH_SCHEMA_VERSION",
    "patch_companion_settings",
    "sha256_file",
    "validate_api_base",
]
