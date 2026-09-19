"""Produce a sanitized, read-only audit of an original Olivia ``feapp.dat``.

The tool never extracts or modifies the archive and never prints JavaScript
source, absolute paths, credentials, user data, or arbitrary string literals.
It reports only hashes, bounded counts, known route references, local API paths,
and the exact anchors required by the existing safe patcher.
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
from typing import Any
import zipfile

from patch_feapp import patch_profile_for_members


AUDIT_SCHEMA_VERSION = "p03.original-client-audit.v1"
MAX_ARCHIVE_MEMBERS = 100_000
MAX_MAIN_JS_BYTES = 128 * 1024 * 1024
MAX_DISCOVERED_VALUES = 200

_ROUTE_CALL_RE = re.compile(
    r"\.(?:push|replace)\(\{name:"
    r"([A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)?"
    r"|\"[^\"\\]{1,80}\"|'[^'\\]{1,80}')"
)
_TOY_API_PATH_RE = re.compile(
    r"[\"'](/toy/[A-Za-z0-9_./{}:-]{1,160})[\"']"
)
_ACTION_RE = re.compile(
    r"(?:\.action===|[\"']action[\"']:)"
    r"[\"']([A-Za-z][A-Za-z0-9_.:-]{0,95})[\"']"
)
_HEX_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_KNOWN_UI_TERMS = (
    "Collection",
    "Home",
    "Settings",
    "Setting",
    "Profile",
    "LetterDetail",
    "设置",
    "个人",
    "信箱",
    "信件",
    "回信",
    "视频",
    "音乐",
)
_KNOWN_WIRE_FIELDS = (
    "letter_status",
    "reply_content",
    "reply_text",
    "reply_video_url",
    "reply_mode",
    "media_status",
    "media_error_code",
)


class OriginalClientAuditError(RuntimeError):
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
        raise OriginalClientAuditError("CLIENT_ARCHIVE_UNREADABLE") from exc
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


def _bounded_sorted(values: set[str]) -> list[str]:
    return sorted(values)[:MAX_DISCOVERED_VALUES]


def _normalize_route_reference(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _read_main_javascript(
    archive: zipfile.ZipFile,
) -> tuple[str, zipfile.ZipInfo, Any]:
    try:
        profile = patch_profile_for_members(archive.namelist())
        info = archive.getinfo(profile.main_js)
    except (KeyError, ValueError) as exc:
        raise OriginalClientAuditError("CLIENT_MAIN_BUNDLE_MISSING") from exc
    if info.is_dir() or info.file_size > MAX_MAIN_JS_BYTES:
        raise OriginalClientAuditError("CLIENT_MAIN_BUNDLE_TOO_LARGE")
    try:
        with archive.open(info) as stream:
            raw = stream.read(MAX_MAIN_JS_BYTES + 1)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise OriginalClientAuditError("CLIENT_MAIN_BUNDLE_UNREADABLE") from exc
    if len(raw) > MAX_MAIN_JS_BYTES:
        raise OriginalClientAuditError("CLIENT_MAIN_BUNDLE_TOO_LARGE")
    try:
        return raw.decode("utf-8"), info, profile
    except UnicodeDecodeError as exc:
        raise OriginalClientAuditError("CLIENT_MAIN_BUNDLE_ENCODING_INVALID") from exc


def _client_version_hint(path: Path) -> str | None:
    try:
        if path.parent.name.casefold() == "resources":
            value = path.parent.parent.name
            if re.fullmatch(r"[0-9A-Za-z._-]{1,64}", value):
                return value
    except (OSError, RuntimeError):
        pass
    return None


def audit_original_client(
    archive_path: str | os.PathLike[str],
    *,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Inspect one original client archive without extracting or modifying it."""

    path = Path(archive_path).expanduser()
    if not path.is_file():
        raise OriginalClientAuditError("CLIENT_ARCHIVE_NOT_FOUND")
    if expected_sha256 is not None and not _HEX_SHA256_RE.fullmatch(expected_sha256):
        raise OriginalClientAuditError("CLIENT_EXPECTED_HASH_INVALID")

    archive_sha256 = _sha256_file(path)
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if not members:
                raise OriginalClientAuditError("CLIENT_ARCHIVE_EMPTY")
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise OriginalClientAuditError("CLIENT_ARCHIVE_TOO_MANY_MEMBERS")
            if any(not _safe_member_name(info.filename) for info in members):
                raise OriginalClientAuditError("CLIENT_ARCHIVE_UNSAFE")
            javascript, main_info, profile = _read_main_javascript(archive)
    except OriginalClientAuditError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise OriginalClientAuditError("CLIENT_ARCHIVE_INVALID") from exc

    extensions = Counter()
    total_compressed = 0
    total_uncompressed = 0
    for info in members:
        total_compressed += max(0, int(info.compress_size))
        total_uncompressed += max(0, int(info.file_size))
        suffix = PurePosixPath(info.filename).suffix.casefold() or "<none>"
        extensions[suffix] += 1

    route_references = {
        _normalize_route_reference(match)
        for match in _ROUTE_CALL_RE.findall(javascript)
    }
    toy_api_paths = set(_TOY_API_PATH_RE.findall(javascript))
    action_names = set(_ACTION_RE.findall(javascript))
    anchor_counts = {
        "response_dispatch": javascript.count(profile.bridge_anchor),
        "local_endpoint_injection": javascript.count(profile.inject_anchor),
        "mailbox_original": javascript.count(profile.mailbox_anchor),
        "mailbox_patched": javascript.count(profile.mailbox_replacement),
    }
    expected_match = (
        None
        if expected_sha256 is None
        else archive_sha256.casefold() == expected_sha256.casefold()
    )
    official_patch_ready = (
        anchor_counts["response_dispatch"] == 1
        and anchor_counts["local_endpoint_injection"] == 1
        and anchor_counts["mailbox_original"] == 1
        and anchor_counts["mailbox_patched"] == 0
        and expected_match is not False
    )
    already_patched = (
        anchor_counts["mailbox_original"] == 0
        and anchor_counts["mailbox_patched"] == 1
        and "toyApiUrl" in javascript
        and "toyWsUrl" in javascript
    )

    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "status": "AUDITED",
        "source": {
            "name": path.name,
            "client_version_hint": _client_version_hint(path),
            "archive_sha256": archive_sha256,
            "expected_sha256_match": expected_match,
        },
        "archive": {
            "member_count": len(members),
            "compressed_bytes": total_compressed,
            "uncompressed_bytes": total_uncompressed,
            "extension_counts": dict(sorted(extensions.items())),
        },
        "main_bundle": {
            "member": profile.main_js,
            "sha256": hashlib.sha256(javascript.encode("utf-8")).hexdigest(),
            "bytes": int(main_info.file_size),
        },
        "patch_contract": {
            "anchor_counts": anchor_counts,
            "state": (
                "official_patch_ready"
                if official_patch_ready
                else "already_patched"
                if already_patched
                else "manual_review_required"
            ),
            "safe_to_apply_existing_patch": official_patch_ready,
        },
        "navigation_evidence": {
            "route_references": _bounded_sorted(route_references),
            "has_home": any(value.endswith(".Home") or value == "Home" for value in route_references),
            "has_collection": any(
                value.endswith(".Collection") or value == "Collection"
                for value in route_references
            ),
        },
        "local_contract_evidence": {
            "toy_api_paths": _bounded_sorted(toy_api_paths),
            "action_names": _bounded_sorted(action_names),
            "wire_field_counts": {
                field: javascript.count(field) for field in _KNOWN_WIRE_FIELDS
            },
        },
        "surface_keyword_counts": {
            term: javascript.count(term) for term in _KNOWN_UI_TERMS
        },
        "limitations": [
            "No route or component is considered reusable from a keyword alone.",
            "The report contains no JavaScript source or arbitrary UI strings.",
            "Screens, dialogs, player bindings and stable patch anchors require manual review of the audited version.",
        ],
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        path = path.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )
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
        raise OriginalClientAuditError("CLIENT_AUDIT_OUTPUT_FAILED") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feapp", type=Path, help="path to the original feapp.dat")
    parser.add_argument("--expected-sha256")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = audit_original_client(
            args.feapp,
            expected_sha256=args.expected_sha256,
        )
        if args.output is not None:
            _atomic_write_json(args.output, report)
            print(
                json.dumps(
                    {
                        "schema_version": AUDIT_SCHEMA_VERSION,
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
    except OriginalClientAuditError as exc:
        print(
            json.dumps(
                {
                    "schema_version": AUDIT_SCHEMA_VERSION,
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
