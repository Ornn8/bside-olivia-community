"""Audit how the original Olivia frontend may launch its bundled players.

The report is structural and sanitized.  It reads ``feapp.dat``,
``feplayer.dat`` and ``webplayer.dat`` in memory, but never extracts, modifies,
or emits source.  Output is restricted to hashes, bounded counts, safe technical
query keys/literals, and context marker signatures.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import tempfile
from typing import Any, Iterable
import zipfile

from patch_feapp import patch_profile_for_members


BRIDGE_AUDIT_SCHEMA_VERSION = "p03.original-player-bridge-audit.v1"
PLAYER_ARCHIVES = ("feplayer.dat", "webplayer.dat")
MAX_ARCHIVE_MEMBERS = 100_000
MAX_TEXT_MEMBER_BYTES = 128 * 1024 * 1024
MAX_TOTAL_TEXT_BYTES = 256 * 1024 * 1024
MAX_LIST_VALUES = 200
MAX_CONTEXTS_PER_TOKEN = 32
_CONTEXT_RADIUS = 320

_SAFE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
_SAFE_TECH_LITERAL_RE = re.compile(r"^[A-Za-z0-9_./:@?&=+%{}-]{1,160}$")
_STRING_LITERAL_RE = re.compile(
    r"(?P<quote>[\"'])(?P<value>[^\"'\\\r\n]{1,160})(?P=quote)"
)
_QUERY_TEMPLATE_RE = re.compile(r"(?:\?|&)([A-Za-z][A-Za-z0-9_.:-]{0,63})=")
_URLSEARCHPARAMS_ALIAS_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_$])([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
    r"new\s+URLSearchParams\s*\(",
)
_SEARCHPARAMS_ALIAS_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_$])([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
    r"new\s+URL\s*\([^;]{0,512}?\)\.searchParams",
)
_DIRECT_QUERY_METHOD_RE = re.compile(
    r"(?:new\s+URLSearchParams\s*\([^;]{0,512}?\)|"
    r"[A-Za-z_$][A-Za-z0-9_$]*\.searchParams)\s*\.\s*"
    r"(?:get|has|set|append|delete)\s*\(\s*[\"']"
    r"([A-Za-z][A-Za-z0-9_.:-]{0,63})[\"']",
)

_PLAYER_REFERENCE_TOKENS = (
    "feplayer.dat",
    "webplayer.dat",
    "feplayer",
    "webplayer",
)
_LAUNCH_MARKERS = (
    "window.open",
    "loadURL",
    "loadFile",
    "BrowserWindow",
    "webview",
    "<iframe",
    "createElement(\"iframe\")",
    "createElement('iframe')",
    "URLSearchParams",
    "Object.fromEntries",
    "encodeURIComponent",
    "decodeURIComponent",
    "location.search",
    "ipcRenderer",
    "ipcMain",
    "postMessage",
    "invoke(",
)
_GENERIC_QUERY_MARKERS = (
    "Object.fromEntries",
    ".entries(",
    ".forEach(",
    ".keys(",
    "URLSearchParams",
    "location.search",
)
_CONTEXT_MARKERS = (
    "ye.Collection",
    "letter_status",
    "reply_content",
    "video",
    "player",
    "media",
    "URLSearchParams",
    "location.search",
    "window.open",
    "loadURL",
    "loadFile",
    "BrowserWindow",
    "webview",
    "iframe",
    "postMessage",
    "ipcRenderer",
    "invoke(",
    "encodeURIComponent",
)
_TECH_LITERAL_KEYWORDS = (
    "feplayer",
    "webplayer",
    "player",
    "video",
    "media",
    "letter",
    "reply",
    "collection",
)


class OriginalPlayerBridgeAuditError(RuntimeError):
    """Stable audit failure without original source or machine-specific paths."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _safe_member_name(value: str) -> bool:
    normalized = value.replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(normalized)
    return bool(normalized) and "\x00" not in normalized and not (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or any(part in {"", ".", ".."} for part in posix.parts)
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
    except OSError as exc:
        raise OriginalPlayerBridgeAuditError("BRIDGE_ARCHIVE_UNREADABLE") from exc
    return digest.hexdigest()


def _read_text(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    if info.is_dir() or info.file_size > MAX_TEXT_MEMBER_BYTES:
        raise OriginalPlayerBridgeAuditError("BRIDGE_TEXT_MEMBER_TOO_LARGE")
    try:
        with archive.open(info) as stream:
            raw = stream.read(MAX_TEXT_MEMBER_BYTES + 1)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise OriginalPlayerBridgeAuditError("BRIDGE_TEXT_MEMBER_UNREADABLE") from exc
    if len(raw) > MAX_TEXT_MEMBER_BYTES:
        raise OriginalPlayerBridgeAuditError("BRIDGE_TEXT_MEMBER_TOO_LARGE")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OriginalPlayerBridgeAuditError("BRIDGE_TEXT_MEMBER_ENCODING_INVALID") from exc


def _read_archive_texts(
    path: Path,
    *,
    only_member: str | None = None,
) -> tuple[str, dict[str, str]]:
    if not path.is_file():
        raise OriginalPlayerBridgeAuditError("BRIDGE_ARCHIVE_NOT_FOUND")
    archive_sha256 = _sha256_file(path)
    texts: dict[str, str] = {}
    total_text_bytes = 0
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if not members:
                raise OriginalPlayerBridgeAuditError("BRIDGE_ARCHIVE_EMPTY")
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise OriginalPlayerBridgeAuditError("BRIDGE_ARCHIVE_TOO_MANY_MEMBERS")
            if any(not _safe_member_name(info.filename) for info in members):
                raise OriginalPlayerBridgeAuditError("BRIDGE_ARCHIVE_UNSAFE")
            if only_member is not None:
                try:
                    members = [archive.getinfo(only_member)]
                except KeyError as exc:
                    raise OriginalPlayerBridgeAuditError(
                        "BRIDGE_MAIN_BUNDLE_MISSING"
                    ) from exc
            for info in members:
                suffix = PurePosixPath(info.filename).suffix.casefold()
                if suffix not in {".html", ".js"} or info.is_dir():
                    continue
                total_text_bytes += int(info.file_size)
                if total_text_bytes > MAX_TOTAL_TEXT_BYTES:
                    raise OriginalPlayerBridgeAuditError("BRIDGE_TOTAL_TEXT_TOO_LARGE")
                texts[info.filename] = _read_text(archive, info)
    except OriginalPlayerBridgeAuditError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise OriginalPlayerBridgeAuditError("BRIDGE_ARCHIVE_INVALID") from exc
    return archive_sha256, texts


def _read_feapp_texts(path: Path) -> tuple[str, dict[str, str]]:
    if not path.is_file():
        raise OriginalPlayerBridgeAuditError("BRIDGE_ARCHIVE_NOT_FOUND")
    try:
        with zipfile.ZipFile(path) as archive:
            profile = patch_profile_for_members(archive.namelist())
    except ValueError as exc:
        raise OriginalPlayerBridgeAuditError("BRIDGE_MAIN_BUNDLE_MISSING") from exc
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise OriginalPlayerBridgeAuditError("BRIDGE_ARCHIVE_INVALID") from exc
    return _read_archive_texts(path, only_member=profile.main_js)


def _application_texts(texts: dict[str, str]) -> dict[str, str]:
    selected: dict[str, str] = {}
    for name, text in texts.items():
        basename = PurePosixPath(name).name.casefold()
        if basename.startswith(("vendor-", "zh-cn-")):
            continue
        selected[name] = text
    return selected


def _query_keys(text: str) -> set[str]:
    keys = set(_DIRECT_QUERY_METHOD_RE.findall(text))
    aliases = set(_URLSEARCHPARAMS_ALIAS_RE.findall(text))
    aliases.update(_SEARCHPARAMS_ALIAS_RE.findall(text))
    for alias in aliases:
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_$]){re.escape(alias)}\s*\.\s*"
            r"(?:get|has|set|append|delete)\s*\(\s*[\"']"
            r"([A-Za-z][A-Za-z0-9_.:-]{0,63})[\"']"
        )
        keys.update(pattern.findall(text))
    keys.update(_QUERY_TEMPLATE_RE.findall(text))
    return {value for value in keys if _SAFE_KEY_RE.fullmatch(value)}


def _technical_literals(text: str) -> set[str]:
    result: set[str] = set()
    for match in _STRING_LITERAL_RE.finditer(text):
        value = match.group("value")
        lowered = value.casefold()
        if lowered.startswith(("http://", "https://")):
            continue
        if not _SAFE_TECH_LITERAL_RE.fullmatch(value):
            continue
        if any(keyword in lowered for keyword in _TECH_LITERAL_KEYWORDS):
            result.add(value)
    return result


def _bounded(values: Iterable[str]) -> list[str]:
    return sorted(set(values))[:MAX_LIST_VALUES]


def _context_signatures(text: str, token: str) -> dict[str, object]:
    locations = [match.start() for match in re.finditer(re.escape(token), text)]
    signatures: dict[str, tuple[str, ...]] = {}
    for offset in locations[:MAX_CONTEXTS_PER_TOKEN]:
        start = max(0, offset - _CONTEXT_RADIUS)
        end = min(len(text), offset + len(token) + _CONTEXT_RADIUS)
        window = text[start:end]
        digest = hashlib.sha256(window.encode("utf-8")).hexdigest()
        markers = tuple(
            marker for marker in _CONTEXT_MARKERS if marker.casefold() in window.casefold()
        )
        signatures[digest] = markers
    return {
        "count": len(locations),
        "contexts": [
            {"sha256": digest, "markers": list(markers)}
            for digest, markers in sorted(signatures.items())
        ],
    }


def _scan_bundle(texts: dict[str, str], *, include_contexts: bool) -> dict[str, Any]:
    application = _application_texts(texts)
    combined = "\n".join(application.values())
    query_keys: set[str] = set()
    technical_literals: set[str] = set()
    for text in application.values():
        query_keys.update(_query_keys(text))
        technical_literals.update(_technical_literals(text))

    result: dict[str, Any] = {
        "text_members": [
            {
                "member": name,
                "bytes": len(text.encode("utf-8")),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
            for name, text in sorted(application.items())
        ],
        "query_keys": _bounded(query_keys),
        "technical_literals": _bounded(technical_literals),
        "launch_marker_counts": {
            marker: combined.count(marker) for marker in _LAUNCH_MARKERS
        },
        "generic_query_marker_counts": {
            marker: combined.count(marker) for marker in _GENERIC_QUERY_MARKERS
        },
    }
    if include_contexts:
        result["player_reference_counts"] = {
            token: combined.casefold().count(token.casefold())
            for token in _PLAYER_REFERENCE_TOKENS
        }
        result["player_reference_contexts"] = {
            token: _context_signatures(combined.casefold(), token.casefold())
            for token in _PLAYER_REFERENCE_TOKENS
            if token.casefold() in combined.casefold()
        }
    return result


def audit_original_player_bridge(
    resources_path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Audit the original main bundle and both bundled players together."""

    resources = Path(resources_path).expanduser()
    if not resources.is_dir():
        raise OriginalPlayerBridgeAuditError("BRIDGE_RESOURCES_NOT_FOUND")

    feapp_sha256, feapp_texts = _read_feapp_texts(resources / "feapp.dat")
    player_archives: dict[str, dict[str, Any]] = {}
    for archive_name in PLAYER_ARCHIVES:
        archive_sha256, texts = _read_archive_texts(resources / archive_name)
        player_archives[archive_name.removesuffix(".dat")] = {
            "archive_sha256": archive_sha256,
            **_scan_bundle(texts, include_contexts=False),
        }

    main = {
        "archive_sha256": feapp_sha256,
        **_scan_bundle(feapp_texts, include_contexts=True),
    }
    main_keys = set(main["query_keys"])
    correlations: dict[str, object] = {}
    for name, player in player_archives.items():
        player_keys = set(player["query_keys"])
        correlations[name] = {
            "shared_query_keys": sorted(main_keys & player_keys),
            "shared_technical_literals": sorted(
                set(main["technical_literals"]) & set(player["technical_literals"])
            )[:MAX_LIST_VALUES],
        }

    literal_references = sum(main["player_reference_counts"].values())
    shared_key_count = sum(
        len(value["shared_query_keys"]) for value in correlations.values()
    )
    if literal_references and shared_key_count:
        bridge_state = "literal_reference_with_query_contract_candidate"
    elif literal_references:
        bridge_state = "literal_reference_without_query_contract"
    else:
        bridge_state = "no_literal_main_bridge_reference"

    version_hint = resources.parent.name
    if not re.fullmatch(r"[0-9A-Za-z._-]{1,64}", version_hint):
        version_hint = None
    return {
        "schema_version": BRIDGE_AUDIT_SCHEMA_VERSION,
        "status": "AUDITED",
        "source": {
            "client_version_hint": version_hint,
            "archive_names": ["feapp.dat", *PLAYER_ARCHIVES],
        },
        "main_bundle": main,
        "players": player_archives,
        "correlation": correlations,
        "bridge_state": bridge_state,
        "limitations": [
            "A technical literal or marker count is evidence of presence, not runtime behavior.",
            "Context hashes and marker sets do not contain source and cannot alone define a patch anchor.",
            "No player or original-client file is modified or extracted by this audit.",
            "If the main bundle has no literal player reference, native-process or runtime tracing is still required.",
        ],
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        path = path.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            stream.write(encoded)
            temporary = Path(stream.name)
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise OriginalPlayerBridgeAuditError("BRIDGE_AUDIT_OUTPUT_FAILED") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "resources",
        type=Path,
        help="path to the original versioned resources directory",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = audit_original_player_bridge(args.resources)
        if args.output is not None:
            _atomic_write_json(args.output, report)
            print(
                json.dumps(
                    {
                        "schema_version": BRIDGE_AUDIT_SCHEMA_VERSION,
                        "status": "AUDITED",
                        "output_name": args.output.name,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except OriginalPlayerBridgeAuditError as exc:
        print(
            json.dumps(
                {
                    "schema_version": BRIDGE_AUDIT_SCHEMA_VERSION,
                    "status": "FAILED",
                    "error_code": exc.code,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
