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
    "media-provider-tail.jsonl",
)
MAX_BUNDLE_BYTES = 1 << 20
MAX_CHECKS = 32
HISTORY_IMPORT_STAGES = frozenset({
    "idle", "preflight", "listing", "memory", "relationship", "importing", "completed", "failed", "unknown",
})
BREEZE_INSTALL_DIAGNOSTIC_CODES = frozenset({
    "BREEZE_PIP_DISK_FULL", "BREEZE_PIP_MISSING_PIP", "BREEZE_PIP_UNSUPPORTED_WHEEL",
    "BREEZE_PIP_HASH_MISMATCH", "BREEZE_PIP_WHEEL_UNAVAILABLE", "BREEZE_PIP_ACCESS_DENIED",
    "BREEZE_PIP_TIMEOUT", "BREEZE_PIP_FAILED", "BREEZE_PIP_PATH_TOO_LONG",
})
MAX_TAIL_RECORDS = 200
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_STATUS_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")
_EVENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TOKEN_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,159}$")
_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "POST"})
_REPLY_MODES = frozenset(
    {"text", "video", "text_letter", "normal_video", "music_video", "spoken_video", "musical_video", "live"}
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
        "running_version",
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
        if name == "running_version" and not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", item):
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
        if name == "history_import":
            projected[name] = project_history_import(check)
            continue
        entry: dict[str, object] = {"state": _status(check.get("state"))}
        if "error_code" in check:
            entry["error_code"] = _code(check["error_code"])
        if name == "video_offline_action":
            from original_client_video_capability_api import OFFLINE_ACTION_ERROR_CODES, OFFLINE_ACTION_STAGES
            if check.get("error_code") not in OFFLINE_ACTION_ERROR_CODES or check.get("stage") not in OFFLINE_ACTION_STAGES:
                raise _invalid()
            entry["stage"] = check["stage"]
        if name in {"video_ordinary", "video_music", "video_runtime"}:
            diagnostic = check.get("diagnostic_code")
            if isinstance(diagnostic, str) and diagnostic in BREEZE_INSTALL_DIAGNOSTIC_CODES:
                entry["diagnostic_code"] = diagnostic
            for field in ("downloaded_bytes", "total_bytes", "remaining_bytes", "checked_bytes"):
                if field in check:
                    count = check[field]
                    if type(count) is not int or not 0 <= count <= 10**15:
                        raise _invalid()
                    entry[field] = count
        if name == "memory_worker":
            for field in ("pending_count", "attempt_count", "terminal_count"):
                if field in check:
                    value = check[field]
                    if type(value) is not int or not 0 <= value <= 1_000_000_000:
                        raise _invalid()
                    entry[field] = value
            if "pending_error_counts" in check:
                counts = _mapping(check["pending_error_counts"])
                if len(counts) > 16:
                    raise _invalid()
                safe_counts = {}
                for error, count in counts.items():
                    if type(count) is not int or not 0 <= count <= 1_000_000_000:
                        raise _invalid()
                    safe_counts[_code(error)] = count
                entry["pending_error_counts"] = safe_counts
            if "worker_running" in check:
                if type(check["worker_running"]) is not bool:
                    raise _invalid()
                entry["worker_running"] = check["worker_running"]
        projected[name] = entry
    return {"checks": projected, "status": _status(source.get("status"))}


def project_history_import(value: object) -> dict[str, object]:
    """Discard unknown or malformed import metadata at both export boundaries."""
    source = value if isinstance(value, Mapping) else {}
    state = source.get("state")
    stage = source.get("stage")
    result = {
        "state": state if isinstance(state, str) and state in {"idle", "running", "completed", "failed", "unavailable", "unknown"} else "unknown",
        "stage": stage if isinstance(stage, str) and stage in HISTORY_IMPORT_STAGES else "unknown",
    }
    for field in ("total", "processed"):
        count = source.get(field)
        if type(count) is int and 0 <= count <= 1_000_000_000:
            result[field] = count
    if type(source.get("task_running")) is bool:
        result["task_running"] = source["task_running"]
    error = source.get("error_code")
    if isinstance(error, str) and _CODE_RE.fullmatch(error):
        result["error_code"] = error
    return result


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
    for name in ("status", "health"):
        if name in source:
            value = source[name]
            if not isinstance(value, str):
                raise _invalid()
            record[name] = _status(value.strip().lower())
    if "error_code" in source:
        record["error_code"] = _code(source["error_code"])
    if runtime:
        for name, maximum in (("elapsed_ms", 86_400_000), ("recorded_at_ms", 10_000_000_000_000)):
            if name in source:
                value = source[name]
                if type(value) is not int or not 0 <= value <= maximum:
                    raise _invalid()
                record[name] = value
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


def _project_media_tail(value: object) -> bytes:
    """Extract structured failure evidence; never export diagnostic strings."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise _invalid()
    categories = {
        'process_timeout', 'process_start_failure', 'process_management_failure',
        'cuda_out_of_memory', 'runtime_dependency_missing', 'python_module_missing',
        'configured_path_missing', 'cuda_runtime_failure', 'external_process_failure',
    }
    exceptions = {
        'ReplyMediaError', 'LatentSyncReplyError', 'MusicReplyError', 'VoiceDirectionError',
        'GatewayError', 'TimeoutExpired', 'TimeoutError', 'ValueError', 'TypeError',
        'OSError', 'FileNotFoundError', 'PermissionError', 'CalledProcessError',
        'RuntimeError', 'ProcessManagementError',
    }
    records = []
    for source in value[-MAX_TAIL_RECORDS:]:
        if not isinstance(source, Mapping):
            continue
        record = {}
        code = source.get('error_code')
        if isinstance(code, str) and _CODE_RE.fullmatch(code):
            record['error_code'] = code
        timestamp = source.get('timestamp')
        if type(timestamp) is int and 0 <= timestamp <= 10_000_000_000:
            record['timestamp'] = timestamp
        if isinstance(source.get('provider'), str) and source['provider'] in {'latentsync', 'breeze', 'minimax', 'soulx', 'roformer', 'ffmpeg'}:
            record['provider'] = source['provider']
        raw = source.get('diagnostic')
        if isinstance(raw, str) and len(raw) <= 4096:
            try:
                detail = json.loads(raw)
            except (ValueError, TypeError):
                detail = dict(part.strip().split('=', 1) for part in raw.split(';') if '=' in part)
            if isinstance(detail, Mapping):
                if isinstance(detail.get('stage'), str) and detail['stage'] in {'prepare', 'voice_plan', 'render', 'publish'}:
                    record['stage'] = detail['stage']
                for field in ('candidate_code', 'cause_candidate_code'):
                    code = detail.get(field)
                    if isinstance(code, str) and _CODE_RE.fullmatch(code):
                        record[field] = code
                for field in ('exception_type', 'cause_exception_type'):
                    if isinstance(detail.get(field), str) and detail[field] in exceptions:
                        record[field] = detail[field]
                chain = detail.get('exception_types')
                if isinstance(chain, str):
                    record['exception_types'] = [name for name in chain.split('>')[:8] if name in exceptions]
                category = detail.get('stderr_category')
                if isinstance(category, str) and category in categories:
                    record['stderr_category'] = category
                elif detail.get('stderr') == 'process timeout':
                    record['stderr_category'] = 'process_timeout'
                returncode = str(detail.get('returncode', ''))
                if re.fullmatch(r'-?[0-9]{1,10}', returncode):
                    record['returncode'] = int(returncode)
        if record:
            records.append(record)
    return b''.join(_json_bytes(record) + b'\n' for record in records)


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
        "media-provider-tail.jsonl": _project_media_tail(values.get("media_provider_tail", ())),
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
