"""Inject the personal-chat binding controls without changing the legacy settings JS."""
from __future__ import annotations

import hashlib
import html
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
import zipfile

from .setup_ui import PERSONAL_CHAT_SETUP_JAVASCRIPT


INDEX_MEMBER = "index.html"
PERSONAL_CHAT_MARKER = "data-olivia-personal-chat-bindings"
PERSONAL_CHAT_SCHEMA_VERSION = "v1"
_COMPANION_MARKER = "data-olivia-companion-settings"
_API_BASE_RE = re.compile(
    r"\bdata-api-base=([\"'])(?P<value>[^\"']+)\1",
    flags=re.IGNORECASE,
)
_PERSONAL_TAG_RE = re.compile(
    r"<script\b[^>]*\bdata-olivia-personal-chat-bindings="
    r"([\"'])v1\1[^>]*>.*?</script\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_COMPANION_TAG_RE = re.compile(
    r"<script\b[^>]*\bdata-olivia-companion-settings="
    r"([\"'])p03\.original-settings-shell\.v1\1[^>]*>\s*</script\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_PERSONAL_MARKER_RE = re.compile(
    r"\bdata-olivia-personal-chat-bindings\s*=",
    flags=re.IGNORECASE,
)


class PersonalChatSettingsPatchError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _validate_api_base(value: str | None) -> str:
    if not value:
        raise PersonalChatSettingsPatchError("COMPANION_API_BASE_REQUIRED")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise PersonalChatSettingsPatchError("COMPANION_API_BASE_INVALID") from exc
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
        raise PersonalChatSettingsPatchError("COMPANION_API_BASE_INVALID")
    return f"http://{parsed.hostname}:{port}/"


def _managed_tag(api_base: str) -> str:
    script = PERSONAL_CHAT_SETUP_JAVASCRIPT.strip()
    if not script or "</script" in script.casefold():
        raise PersonalChatSettingsPatchError(
            "COMPANION_PERSONAL_CHAT_SCRIPT_INVALID"
        )
    return (
        f'<script {PERSONAL_CHAT_MARKER}="{PERSONAL_CHAT_SCHEMA_VERSION}" '
        f'data-api-base="{html.escape(api_base, quote=True)}">\n'
        + script
        + "\n</script>"
    )


def _api_base_from_tag(tag: str) -> str:
    match = _API_BASE_RE.search(tag)
    if match is None:
        raise PersonalChatSettingsPatchError(
            "COMPANION_PERSONAL_CHAT_PATCH_INVALID"
        )
    return html.unescape(match.group("value"))


def _patch_index_text(source: str, api_base: str) -> tuple[str, str]:
    managed = _managed_tag(api_base)
    marker_count = len(_PERSONAL_MARKER_RE.findall(source))
    existing = list(_PERSONAL_TAG_RE.finditer(source))

    if marker_count:
        if marker_count != 1 or len(existing) != 1:
            raise PersonalChatSettingsPatchError(
                "COMPANION_PERSONAL_CHAT_PATCH_INVALID"
            )
        match = existing[0]
        if _api_base_from_tag(match.group(0)) != api_base:
            raise PersonalChatSettingsPatchError("COMPANION_API_BASE_MISMATCH")
        if _normalize_newlines(match.group(0)) == _normalize_newlines(managed):
            return source, "ALREADY_PATCHED"
        return source[: match.start()] + managed + source[match.end() :], "PATCHED"

    companion = list(_COMPANION_TAG_RE.finditer(source))
    if len(companion) != 1 or source.count(_COMPANION_MARKER) != 1:
        raise PersonalChatSettingsPatchError("COMPANION_PATCH_INCOMPLETE")
    anchor = companion[0]
    if _api_base_from_tag(anchor.group(0)) != api_base:
        raise PersonalChatSettingsPatchError("COMPANION_API_BASE_MISMATCH")
    return (
        source[: anchor.end()] + "\n  " + managed + source[anchor.end() :],
        "PATCHED",
    )


def _member_hashes(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise PersonalChatSettingsPatchError(
                    "COMPANION_PERSONAL_CHAT_ARCHIVE_INVALID"
                )
            for info in infos:
                values[info.filename] = hashlib.sha256(archive.read(info)).hexdigest()
    except PersonalChatSettingsPatchError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, KeyError) as exc:
        raise PersonalChatSettingsPatchError(
            "COMPANION_PERSONAL_CHAT_ARCHIVE_INVALID"
        ) from exc
    return values


def _read_index(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            matches = [info for info in archive.infolist() if info.filename == INDEX_MEMBER]
            if len(matches) != 1 or matches[0].is_dir():
                raise PersonalChatSettingsPatchError(
                    "COMPANION_PERSONAL_CHAT_ARCHIVE_INVALID"
                )
            return archive.read(matches[0]).decode("utf-8")
    except PersonalChatSettingsPatchError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, KeyError, UnicodeError) as exc:
        raise PersonalChatSettingsPatchError(
            "COMPANION_PERSONAL_CHAT_ARCHIVE_INVALID"
        ) from exc


def _write_index(path: Path, patched: str, before: dict[str, str]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.personal-chat-",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(path) as source, zipfile.ZipFile(
            temporary, "w"
        ) as destination:
            destination.comment = source.comment
            for info in source.infolist():
                payload = source.read(info) if not info.is_dir() else b""
                if info.filename == INDEX_MEMBER:
                    payload = patched.encode("utf-8")
                destination.writestr(info, payload)

        after = _member_hashes(temporary)
        if set(after) != set(before):
            raise PersonalChatSettingsPatchError(
                "COMPANION_PERSONAL_CHAT_PATCH_VERIFICATION_FAILED"
            )
        for name, digest in before.items():
            if name != INDEX_MEMBER and after.get(name) != digest:
                raise PersonalChatSettingsPatchError(
                    "COMPANION_PERSONAL_CHAT_PATCH_VERIFICATION_FAILED"
                )
        os.replace(temporary, path)
    except PersonalChatSettingsPatchError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise PersonalChatSettingsPatchError(
            "COMPANION_PERSONAL_CHAT_PATCH_WRITE_FAILED"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def patch_personal_chat_settings(
    feapp_path: str | os.PathLike[str], api_base: str | None
) -> str:
    """Add or refresh the inline binding UI while preserving all other members."""

    path = Path(feapp_path).expanduser().resolve()
    normalized = _validate_api_base(api_base)
    if not path.is_file():
        raise PersonalChatSettingsPatchError("COMPANION_ARCHIVE_NOT_FOUND")

    before = _member_hashes(path)
    source = _read_index(path)
    patched, status = _patch_index_text(source, normalized)
    if status == "ALREADY_PATCHED":
        return status
    _write_index(path, patched, before)

    verified = _read_index(path)
    matches = list(_PERSONAL_TAG_RE.finditer(verified))
    if (
        len(matches) != 1
        or len(_PERSONAL_MARKER_RE.findall(verified)) != 1
        or _api_base_from_tag(matches[0].group(0)) != normalized
        or _normalize_newlines(matches[0].group(0))
        != _normalize_newlines(_managed_tag(normalized))
    ):
        raise PersonalChatSettingsPatchError(
            "COMPANION_PERSONAL_CHAT_PATCH_VERIFICATION_FAILED"
        )
    return "PATCHED"


__all__ = [
    "PERSONAL_CHAT_MARKER",
    "PERSONAL_CHAT_SCHEMA_VERSION",
    "PersonalChatSettingsPatchError",
    "patch_personal_chat_settings",
]
