"""Create a deterministic, allowlisted diagnostic support bundle in memory."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import io
import json
import re
import zipfile


DIAGNOSTIC_BUNDLE_SCHEMA = "olivia.diagnostic-bundle.v1"
DIAGNOSTIC_BUNDLE_MEMBERS = (
    "manifest.json",
    "summary.json",
    "health.json",
    "install.json",
    "tasks.json",
    "launcher-tail.jsonl",
    "runtime-tail.jsonl",
)
MAX_BUNDLE_BYTES = 1 << 20
MAX_CHECKS = 32
MAX_TAIL_RECORDS = 200
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_STATUS_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")
_EVENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TOKEN_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,159}$")
_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "POST"})
_REPLY_MODES = frozenset(
    {"text", "video", "text_letter", "normal_video", "music_video", "live"}
)
_TASK_STAGES = frozenset(
    {
        "cancelled",
        "completed",
        "failed",
        "media_generation",
        "reply_generation",
        "waiting",
        "unknown",
    }
)
_ELAPSED_BUCKETS = frozenset(
    {"under_1m", "1m_5m", "5m_15m", "15m_1h", "1h_6h", "over_6h", "unknown"}
)


class DiagnosticBundleError(RuntimeError):
    """Stable export failure code without any collected diagnostic content."""


def _invalid() -> DiagnosticBundleError:
    return DiagnosticBundleError("DIAGNOSTIC_BUNDLE_INPUT_INVALID")


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _invalid()
    return value


def _status(value: object) -> str:
    if not isinstance(value, str) or not _STATUS_RE.fullmatch(value):
        raise _invalid()
    return value


def _code(value: object) -> str:
    if not isinstance(value, str) or not _CODE_RE.fullmatch(value):
        raise _invalid()
    return value


def _project_summary(value: object) -> dict[str, object]:
    source = _mapping(value)
    result: dict[str, object] = {"status": _status(source.get("status"))}
    for name in (
        "contract_version",
        "python_version",
        "os_name",
        "os_release",
        "architecture",
    ):
        if name not in source:
            continue
        item = source[name]
        if not isinstance(item, str) or not _TOKEN_RE.fullmatch(item):
            raise _invalid()
        result[name] = item
    return result


def _project_health(value: object) -> dict[str, object]:
    source = _mapping(value)
    checks = _mapping(source.get("checks"))
    if len(checks) > MAX_CHECKS:
        raise _invalid()
    projected: dict[str, object] = {}
    for name in sorted(checks):
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise _invalid()
        check = _mapping(checks[name])
        entry: dict[str, object] = {"state": _status(check.get("state"))}
        if "error_code" in check:
            entry["error_code"] = _code(check["error_code"])
        projected[name] = entry
    return {"checks": projected, "status": _status(source.get("status"))}


def _project_install(value: object) -> dict[str, object]:
    source = _mapping(value)
    result: dict[str, object] = {"status": _status(source.get("status"))}
    for name in ("setup_completed", "key_configured"):
        if name in source:
            item = source[name]
            if type(item) is not bool:
                raise _invalid()
            result[name] = item
    if "error_code" in source:
        result["error_code"] = _code(source["error_code"])
    return result


def _project_tasks(value: object) -> dict[str, object]:
    source = _mapping(value)
    pending = source.get("pending")
    if type(pending) is not int or not 0 <= pending <= 100_000:
        raise _invalid()
    raw_items = source.get("items", ())
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes, bytearray)):
        raise _invalid()
    if len(raw_items) > 20:
        raise _invalid()
    items: list[dict[str, object]] = []
    for index, value in enumerate(raw_items, start=1):
        item = _mapping(value)
        projected: dict[str, object] = {"index": index, "status": _status(item.get("status"))}
        if "error_code" in item:
            projected["error_code"] = _code(item["error_code"])
        if "media_status" in item:
            projected["media_status"] = _status(item["media_status"])
        if "media_error_code" in item:
            projected["media_error_code"] = _code(item["media_error_code"])
        if "reply_mode" in item:
            reply_mode = item["reply_mode"]
            if reply_mode not in _REPLY_MODES:
                raise _invalid()
            projected["reply_mode"] = reply_mode
        stage = item.get("stage")
        if stage not in _TASK_STAGES:
            raise _invalid()
        projected["stage"] = stage
        elapsed_bucket = item.get("elapsed_bucket")
        if elapsed_bucket not in _ELAPSED_BUCKETS:
            raise _invalid()
        projected["elapsed_bucket"] = elapsed_bucket
        for name in ("retryable", "media_retryable"):
            if name in item:
                flag = item[name]
                if type(flag) is not bool:
                    raise _invalid()
                projected[name] = flag
        items.append(projected)
    result: dict[str, object] = {"items": items, "pending": pending, "status": _status(source.get("status"))}
    if "error_code" in source:
        result["error_code"] = _code(source["error_code"])
    return result


def _project_tail_record(value: object, *, runtime: bool) -> dict[str, object]:
    source = _mapping(value)
    event = source.get("event")
    if not isinstance(event, str) or not _EVENT_RE.fullmatch(event):
        raise _invalid()
    record: dict[str, object] = {"event": event}
    if "attempt" in source:
        attempt = source["attempt"]
        if type(attempt) is not int or not 1 <= attempt <= 10:
            raise _invalid()
        record["attempt"] = attempt
    if "exit_code" in source:
        exit_code = source["exit_code"]
        if exit_code is not None:
            if type(exit_code) is not int or not -(1 << 31) <= exit_code <= 0xFFFFFFFF:
                raise _invalid()
            record["exit_code"] = exit_code
    for name in ("status", "error_code", "health"):
        if name in source:
            record[name] = _code(source[name])
    if runtime:
        if "reply_mode" in source:
            reply_mode = source["reply_mode"]
            if reply_mode not in _REPLY_MODES:
                raise _invalid()
            record["reply_mode"] = reply_mode
        if "method" in source:
            method = source["method"]
            if method not in _METHODS:
                raise _invalid()
            record["method"] = method
    return record


def _project_tail(value: object, *, runtime: bool) -> bytes:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise _invalid()
    if len(value) > MAX_TAIL_RECORDS:
        raise _invalid()
    records = [_project_tail_record(item, runtime=runtime) for item in value]
    return b"".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        for record in records
    )


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _zip_member(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    member = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    member.compress_type = zipfile.ZIP_DEFLATED
    member.external_attr = 0o600 << 16
    archive.writestr(member, payload)


def build_diagnostic_bundle(source: Mapping[str, object]) -> bytes:
    """Return the complete fixed archive, or fail before returning any bytes."""

    values = _mapping(source)
    required = {"summary", "health", "install", "tasks", "launcher_tail", "runtime_tail"}
    if not required.issubset(values):
        raise _invalid()
    summary = _project_summary(values["summary"])
    health = _project_health(values["health"])
    install = _project_install(values["install"])
    tasks = _project_tasks(values["tasks"])
    payloads = {
        "manifest.json": _json_bytes({
            "members": list(DIAGNOSTIC_BUNDLE_MEMBERS),
            "schema_version": DIAGNOSTIC_BUNDLE_SCHEMA,
        }),
        "summary.json": _json_bytes(summary),
        "health.json": _json_bytes(health),
        "install.json": _json_bytes(install),
        "tasks.json": _json_bytes(tasks),
        "launcher-tail.jsonl": _project_tail(values["launcher_tail"], runtime=False),
        "runtime-tail.jsonl": _project_tail(values["runtime_tail"], runtime=True),
    }
    if tuple(payloads) != DIAGNOSTIC_BUNDLE_MEMBERS or sum(map(len, payloads.values())) > MAX_BUNDLE_BYTES:
        raise DiagnosticBundleError("DIAGNOSTIC_BUNDLE_TOO_LARGE")
    output = io.BytesIO()
    try:
        with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in DIAGNOSTIC_BUNDLE_MEMBERS:
                _zip_member(archive, name, payloads[name])
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise DiagnosticBundleError("DIAGNOSTIC_BUNDLE_UNAVAILABLE") from exc
    result = output.getvalue()
    if len(result) > MAX_BUNDLE_BYTES:
        raise DiagnosticBundleError("DIAGNOSTIC_BUNDLE_TOO_LARGE")
    return result


__all__ = [
    "DIAGNOSTIC_BUNDLE_MEMBERS",
    "DIAGNOSTIC_BUNDLE_SCHEMA",
    "MAX_BUNDLE_BYTES",
    "DiagnosticBundleError",
    "build_diagnostic_bundle",
]
