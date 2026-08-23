"""Audit original Olivia player archives without extracting or modifying them.

The report is intentionally metadata-only. It never emits JavaScript or HTML
source, arbitrary string literals, absolute paths, credentials, user content,
or full URLs. It records only hashes, bounded archive metadata, safe relative
entrypoint names, allowlisted field/transport counts, local ``/toy`` paths, and
known player-library markers.
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
from urllib.parse import urlsplit
import zipfile


PLAYER_AUDIT_SCHEMA_VERSION = "p03.original-player-audit.v1"
PLAYER_ARCHIVES = ("feplayer.dat", "webplayer.dat")
MAX_ARCHIVE_MEMBERS = 100_000
MAX_TEXT_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_TEXT_BYTES = 192 * 1024 * 1024
MAX_LIST_VALUES = 200

_TEXT_SUFFIXES = frozenset({".html", ".js"})
_MEDIA_SUFFIXES = frozenset(
    {".mp4", ".webm", ".m3u8", ".mp3", ".wav", ".ogg", ".flac"}
)
_SAFE_METADATA_PATH_RE = re.compile(r"^[A-Za-z0-9._/@+-]{1,200}$")
_LOCAL_API_PATH_RE = re.compile(
    r"[\"'](/toy/[A-Za-z0-9_./{}:-]{1,160})[\"']"
)
_EXTERNAL_URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{1,512}")
_RELATIVE_ASSET_RE = re.compile(
    r"(?:src|href)\s*=\s*[\"']([^\"']{1,200}\.(?:js|css))(?:\?[^\"']*)?[\"']",
    flags=re.IGNORECASE,
)
_ACTION_RE = re.compile(
    r"(?:\.action===|[\"']action[\"']\s*:)"
    r"[\"']([A-Za-z][A-Za-z0-9_.:-]{0,95})[\"']"
)
_QUERY_GET_RE = re.compile(
    r"(?:URLSearchParams\([^)]*\)|searchParams)\.get\([\"']"
    r"([A-Za-z][A-Za-z0-9_.:-]{0,63})[\"']\)"
)

_ALLOWLIST_FIELDS = (
    "reply_video_url",
    "replyVideoUrl",
    "video_url",
    "videoUrl",
    "media_url",
    "mediaUrl",
    "audio_url",
    "audioUrl",
    "poster",
    "autoplay",
    "controls",
    "muted",
    "loop",
    "currentTime",
    "duration",
)
_ALLOWLIST_QUERY_KEYS = frozenset(
    {
        "url",
        "src",
        "video",
        "videoUrl",
        "video_url",
        "audio",
        "audioUrl",
        "audio_url",
        "poster",
        "autoplay",
        "loop",
        "muted",
        "id",
        "letter_id",
        "letterId",
        "mode",
        "token",
    }
)
_TRANSPORT_MARKERS = (
    "postMessage",
    "addEventListener(\"message\"",
    "addEventListener('message'",
    "onmessage",
    "URLSearchParams",
    "location.search",
    "location.hash",
    "window.name",
    "localStorage",
    "sessionStorage",
)
_PLAYER_MARKERS = (
    "HTMLVideoElement",
    "document.createElement(\"video\")",
    "document.createElement('video')",
    "<video",
    "Hls",
    "hls.js",
    "mpegts",
    "flv.js",
    "dashjs",
    "videojs",
    "Video.js",
    "Artplayer",
    "DPlayer",
    "Plyr",
    "Howler",
)
_MEDIA_TEXT_MARKERS = (".mp4", ".webm", ".m3u8", ".mp3", ".wav", ".ogg", ".flac")


class OriginalPlayerAuditError(RuntimeError):
    """Stable audit failure without a machine-specific path or source excerpt."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
    except OSError as exc:
        raise OriginalPlayerAuditError("PLAYER_ARCHIVE_UNREADABLE") from exc
    return digest.hexdigest()


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


def _safe_metadata_path(value: str) -> str | None:
    normalized = value.replace("\\", "/")
    if not _safe_member_name(normalized) or not _SAFE_METADATA_PATH_RE.fullmatch(normalized):
        return None
    return normalized


def _bounded(values: Iterable[str]) -> list[str]:
    return sorted(set(values))[:MAX_LIST_VALUES]


def _read_text_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    if info.is_dir() or info.file_size > MAX_TEXT_MEMBER_BYTES:
        raise OriginalPlayerAuditError("PLAYER_TEXT_MEMBER_TOO_LARGE")
    try:
        with archive.open(info) as stream:
            raw = stream.read(MAX_TEXT_MEMBER_BYTES + 1)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise OriginalPlayerAuditError("PLAYER_TEXT_MEMBER_UNREADABLE") from exc
    if len(raw) > MAX_TEXT_MEMBER_BYTES:
        raise OriginalPlayerAuditError("PLAYER_TEXT_MEMBER_TOO_LARGE")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OriginalPlayerAuditError("PLAYER_TEXT_MEMBER_ENCODING_INVALID") from exc


def _external_hosts(text: str) -> set[str]:
    hosts: set[str] = set()
    for value in _EXTERNAL_URL_RE.findall(text):
        try:
            host = urlsplit(value).hostname
        except ValueError:
            host = None
        if host and re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
            hosts.add(host.casefold())
    return hosts


def _version_hint(resources: Path) -> str | None:
    value = resources.parent.name
    if re.fullmatch(r"[0-9A-Za-z._-]{1,64}", value):
        return value
    return None


def _audit_archive(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise OriginalPlayerAuditError("PLAYER_ARCHIVE_NOT_FOUND")
    archive_sha256 = _sha256_file(path)
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if not members:
                raise OriginalPlayerAuditError("PLAYER_ARCHIVE_EMPTY")
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise OriginalPlayerAuditError("PLAYER_ARCHIVE_TOO_MANY_MEMBERS")
            if any(not _safe_member_name(info.filename) for info in members):
                raise OriginalPlayerAuditError("PLAYER_ARCHIVE_UNSAFE")

            extension_counts: Counter[str] = Counter()
            direct_media_counts: Counter[str] = Counter()
            html_entrypoints: list[str] = []
            javascript_bundles: list[dict[str, object]] = []
            relative_assets: set[str] = set()
            local_api_paths: set[str] = set()
            action_names: set[str] = set()
            query_keys: set[str] = set()
            external_hosts: set[str] = set()
            field_counts = Counter({field: 0 for field in _ALLOWLIST_FIELDS})
            transport_counts = Counter({marker: 0 for marker in _TRANSPORT_MARKERS})
            player_marker_counts = Counter({marker: 0 for marker in _PLAYER_MARKERS})
            media_text_counts = Counter({marker: 0 for marker in _MEDIA_TEXT_MARKERS})
            total_compressed = 0
            total_uncompressed = 0
            total_text_bytes = 0

            for info in members:
                total_compressed += max(0, int(info.compress_size))
                total_uncompressed += max(0, int(info.file_size))
                suffix = PurePosixPath(info.filename).suffix.casefold() or "<none>"
                extension_counts[suffix] += 1
                if suffix in _MEDIA_SUFFIXES:
                    direct_media_counts[suffix] += 1
                safe_name = _safe_metadata_path(info.filename)
                if suffix == ".html" and safe_name is not None:
                    html_entrypoints.append(safe_name)
                if suffix not in _TEXT_SUFFIXES or info.is_dir():
                    continue
                total_text_bytes += int(info.file_size)
                if total_text_bytes > MAX_TOTAL_TEXT_BYTES:
                    raise OriginalPlayerAuditError("PLAYER_TOTAL_TEXT_TOO_LARGE")
                text = _read_text_member(archive, info)
                if suffix == ".js" and safe_name is not None:
                    javascript_bundles.append(
                        {
                            "member": safe_name,
                            "bytes": int(info.file_size),
                            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        }
                    )
                relative_assets.update(
                    value
                    for match in _RELATIVE_ASSET_RE.findall(text)
                    if (value := _safe_metadata_path(match)) is not None
                )
                local_api_paths.update(_LOCAL_API_PATH_RE.findall(text))
                action_names.update(_ACTION_RE.findall(text))
                query_keys.update(
                    value for value in _QUERY_GET_RE.findall(text) if value in _ALLOWLIST_QUERY_KEYS
                )
                external_hosts.update(_external_hosts(text))
                for field in _ALLOWLIST_FIELDS:
                    field_counts[field] += text.count(field)
                for marker in _TRANSPORT_MARKERS:
                    transport_counts[marker] += text.count(marker)
                for marker in _PLAYER_MARKERS:
                    player_marker_counts[marker] += text.count(marker)
                lowered = text.casefold()
                for marker in _MEDIA_TEXT_MARKERS:
                    media_text_counts[marker] += lowered.count(marker)
    except OriginalPlayerAuditError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise OriginalPlayerAuditError("PLAYER_ARCHIVE_INVALID") from exc

    return {
        "name": path.name,
        "archive_sha256": archive_sha256,
        "archive": {
            "member_count": len(members),
            "compressed_bytes": total_compressed,
            "uncompressed_bytes": total_uncompressed,
            "extension_counts": dict(sorted(extension_counts.items())),
            "direct_media_counts": dict(sorted(direct_media_counts.items())),
        },
        "entrypoints": {
            "html_members": _bounded(html_entrypoints),
            "relative_js_css_assets": _bounded(relative_assets),
            "javascript_bundles": sorted(
                javascript_bundles,
                key=lambda value: str(value["member"]),
            )[:MAX_LIST_VALUES],
        },
        "binding_evidence": {
            "local_api_paths": _bounded(local_api_paths),
            "action_names": _bounded(action_names),
            "query_keys": _bounded(query_keys),
            "field_counts": dict(field_counts),
            "transport_marker_counts": dict(transport_counts),
            "player_marker_counts": dict(player_marker_counts),
            "media_text_marker_counts": dict(media_text_counts),
            "external_origin_hosts": _bounded(external_hosts),
        },
    }


def audit_original_players(resources_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Audit both original player archives under one versioned resources folder."""

    resources = Path(resources_path).expanduser()
    if not resources.is_dir():
        raise OriginalPlayerAuditError("PLAYER_RESOURCES_NOT_FOUND")
    players = {
        name.removesuffix(".dat"): _audit_archive(resources / name)
        for name in PLAYER_ARCHIVES
    }
    return {
        "schema_version": PLAYER_AUDIT_SCHEMA_VERSION,
        "status": "AUDITED",
        "source": {
            "client_version_hint": _version_hint(resources),
            "archive_names": list(PLAYER_ARCHIVES),
        },
        "players": players,
        "comparison": {
            "shared_html_members": sorted(
                set(players["feplayer"]["entrypoints"]["html_members"])
                & set(players["webplayer"]["entrypoints"]["html_members"])
            ),
            "shared_local_api_paths": sorted(
                set(players["feplayer"]["binding_evidence"]["local_api_paths"])
                & set(players["webplayer"]["binding_evidence"]["local_api_paths"])
            ),
            "shared_query_keys": sorted(
                set(players["feplayer"]["binding_evidence"]["query_keys"])
                & set(players["webplayer"]["binding_evidence"]["query_keys"])
            ),
        },
        "limitations": [
            "A marker count proves only that the token exists, not how the player uses it.",
            "The report contains no HTML or JavaScript source and no arbitrary string dump.",
            "Exact letter-detail wiring still requires review of original-client behavior and bounded patch anchors.",
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
        raise OriginalPlayerAuditError("PLAYER_AUDIT_OUTPUT_FAILED") from exc


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
        report = audit_original_players(args.resources)
        if args.output is not None:
            _atomic_write_json(args.output, report)
            print(
                json.dumps(
                    {
                        "schema_version": PLAYER_AUDIT_SCHEMA_VERSION,
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
    except OriginalPlayerAuditError as exc:
        print(
            json.dumps(
                {
                    "schema_version": PLAYER_AUDIT_SCHEMA_VERSION,
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
