"""Create a deterministic, allowlisted diagnostic support bundle in memory."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
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
    "idle", "preflight", "memory_wait", "listing", "memory", "relationship", "importing", "completed", "failed", "unknown",
})
_HISTORY_RELATIONSHIP_FIXED_ERROR_CODES = frozenset({
    'HISTORY_RELATIONSHIP_FAILED', 'HISTORY_RELATIONSHIP_UNAVAILABLE',
    'HISTORY_RELATIONSHIP_STORAGE_UNAVAILABLE',
    'PRIVATE_WORLD_HISTORY_UNAVAILABLE', 'PRIVATE_WORLD_HISTORY_INITIALIZATION_FAILED',
    'PRIVATE_WORLD_HISTORY_RESULT_INVALID', 'PRIVATE_WORLD_VERSION_CONFLICT',
    'PRIVATE_WORLD_HISTORY_LOOKUP_FAILED', 'PRIVATE_WORLD_HISTORY_PREPARE_FAILED',
    'PRIVATE_WORLD_HISTORY_WRITE_FAILED',
    *(f'PRIVATE_WORLD_COMMAND_{suffix}' for suffix in (
        'INVALID', 'AUDIT_INVALID', 'SOURCE_FORBIDDEN', 'APPROVAL_REQUIRED',
        'EVIDENCE_REQUIRED', 'STORAGE_UNAVAILABLE', 'IDENTITY_CONFLICT', 'EVIDENCE_INVALID',
    )),
    *(f'PRIVATE_WORLD_HISTORY_LLM_{suffix}' for suffix in (
        'INPUT_TOO_LONG', 'QUOTA_EXHAUSTED', 'TIMEOUT', 'PROTOCOL', 'UNAVAILABLE',
        'RETRYABLE', 'REJECTED', 'AUTH_FAILED', 'USAGE_PENDING', 'REQUEST_DUPLICATE',
        'INVALID_INPUT', 'FAILED', 'RATE_LIMITED',
    )),
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
_PHASE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}(?::[a-z_][a-z0-9_]{0,47})?$")
_TOKEN_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,159}$")
_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "POST"})
_REPLY_MODES = frozenset(
    {"text", "video", "text_letter", "normal_video", "music_video", "spoken_video", "musical_video", "live",
     "voice_reply", "singing_video", "voice_song_video"}
)
_TASK_STAGES = frozenset(
    {
        "cancelled",
        "completed",
        "failed",
        "media_generation",
        "image_generation",
        "reply_generation",
        "waiting",
        "unknown",
    }
)
_ELAPSED_BUCKETS = frozenset(
    {"under_1m", "1m_5m", "5m_15m", "15m_1h", "1h_6h", "over_6h", "unknown"}
)
_QUALITY_VIOLATION_CODES = frozenset({
    'OUTPUT_LIMIT_EXCEEDED', 'VIDEO_REPLY_LENGTH_OUT_OF_RANGE',
    'STAGE_DIRECTION_IN_SPOKEN_TEXT', 'INTERNAL_CONTROL_MARKUP', 'PRIVATE_STATE_EXPOSED',
    'PERMANENT_AVAILABILITY_PROMISE', 'EXCLUSIVE_RELATIONSHIP_PROMISE',
    'UNAUTHORIZED_SHARED_HISTORY', 'UNSOLICITED_INTIMACY', 'INTIMACY_EXCEEDS_GRANT',
    'IDENTITY_DRIFT', 'BOUNDARY_BREACH', 'STAGE_DRIFT', 'ACKNOWLEDGED_FEELING_REWRITE',
    'INTIMACY_VIOLATION', 'RELATIONSHIP_RETRACTION', 'STYLE_DRIFT',
    'GENERIC_COUNSELOR', 'MEMORY_FABRICATION', 'FOCUS_REVIEW_UNAVAILABLE', 'AUTONOMY_REVIEW_UNAVAILABLE',
    'OFF_TURN_REPLY',
})


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
        if name == "running_version" and not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:[-+][0-9A-Za-z.-]+)?", item):
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
        if name == "memory_install":
            projected[name] = project_memory_install(check)
            continue
        if name == "history_import":
            projected[name] = project_history_import(check)
            continue
        entry: dict[str, object] = {"state": _status(check.get("state"))}
        if name == 'daily_life':
            from runtime.diagnostics.failure_context import project_daily_life_failure
            entry.update(project_daily_life_failure(check))
        if "error_code" in check:
            entry["error_code"] = _code(check["error_code"])
        if name == "video_offline_action":
            from original_client_video_capability_api import OFFLINE_ACTION_ERROR_CODES, OFFLINE_ACTION_STAGES
            if check.get("error_code") not in OFFLINE_ACTION_ERROR_CODES or check.get("stage") not in OFFLINE_ACTION_STAGES:
                raise _invalid()
            entry["stage"] = check["stage"]
        if name in {"video_ordinary", "video_music", "video_runtime", "media_component_install"}:
            from runtime.diagnostics.install_failure import project_install_failure
            details = project_install_failure(check.get('failure_details'))
            if details:
                entry['failure_details'] = details
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


def project_memory_install(value: object) -> dict[str, object]:
    from mem0_capability_install import MEM0_INSTALL_FAILURE_CODES
    source = _mapping(value)
    state = source.get("state")
    result = {"state": state if isinstance(state, str) and state in {
        "missing", "queued", "downloading", "verifying", "ready", "paused",
        "repair", "incompatible", "unavailable", "unknown"} else "unavailable"}
    for name, allowed in (
        ("phase", {"idle", "queued", "preflight", "package", "runtime", "model", "verification", "complete", "uninstall"}),
        ("source", {"offline", "auto", "official", "offline-package"}),
    ):
        if isinstance(source.get(name), str) and source[name] in allowed:
            result[name] = source[name]
    reason = source.get("reason_code", source.get("error_code"))
    if isinstance(reason, str) and reason in MEM0_INSTALL_FAILURE_CODES | {"MEM0_DIAGNOSTIC_BUSY", "MEM0_DIAGNOSTIC_NOT_CHECKED"}:
        result["error_code"] = reason
    for name in ("downloaded_bytes", "total_bytes", "remaining_bytes", "installed_bytes"):
        count = source.get(name)
        if type(count) is int and 0 <= count <= 10**15:
            result[name] = count
    return result


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


def project_history_relationship_failure(value: object) -> dict[str, object]:
    """Export the current failed durable batch without its private payload."""
    from .history_relationship import JEV_ERROR_CODES, project_history_failure_context
    source = value if isinstance(value, Mapping) else {}
    if source.get('status') not in ('FAILED', 'failed'):
        return {}
    result = {'event': 'history_relationship_failed', 'status': 'FAILED'}
    code = source.get('error_code')
    known = isinstance(code, str) and (
        code in _HISTORY_RELATIONSHIP_FIXED_ERROR_CODES or
        code.startswith('PRIVATE_WORLD_HISTORY_') and code[len('PRIVATE_WORLD_HISTORY_'):] in JEV_ERROR_CODES
    )
    result['error_code'] = code if known else 'HISTORY_RELATIONSHIP_FAILED'
    for name in ('total', 'processed'):
        count = source.get(name)
        if type(count) is int and 0 <= count <= 1_000_000_000:
            result[name] = count
    if 'failure_context' in source:
        result['failure_context'] = project_history_failure_context(source['failure_context'])
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


def project_reply_quality(value: Mapping[str, object]) -> dict[str, object]:
    """Finite metadata shared by letter and chat diagnostics, without drafts."""
    from .failure_context import REWRITE_ERROR_CODES
    result = {}
    degraded = value.get('degraded_stages')
    if isinstance(degraded, dict):
        from runtime.reply.reply_pipeline import _TEXT_RECOVERY_CODES
        result['degraded_stages'] = {stage: code for stage, code in degraded.items()
            if stage in {'world', 'decision'} and isinstance(code, str) and code in _TEXT_RECOVERY_CODES}
    if value.get('quality_status') in ('not_checked', 'accepted', 'accepted_degraded', 'accepted_with_warnings', 'blocked'):
        result['quality_status'] = value['quality_status']
    from runtime.personal_chat.decision import NEUTRAL_METADATA, _CONTROL_REASONS
    if value.get('decision_dropped_media') == 'UNREQUESTED_SPEECH':
        result['decision_dropped_media'] = 'UNREQUESTED_SPEECH'
    dropped_controls = value.get('decision_dropped_controls')
    if isinstance(dropped_controls, str) and dropped_controls in _CONTROL_REASONS | {'VALUE_TYPE_OR_TIME'}:
        result['decision_dropped_controls'] = dropped_controls
    for key, allowed in (('decision_defaulted_fields', set(NEUTRAL_METADATA)),
                         ('decision_warning_codes', {'REPEATED_REPLY'})):
        values = value.get(key)
        if isinstance(values, (list, tuple)):
            result[key] = list(dict.fromkeys(item for item in values[:16]
                                            if isinstance(item, str) and item in allowed))
    for field, maximum in (('reviewer_calls', 2), ('rewrite_calls', 1)):
        count = value.get(field)
        if type(count) is int and 0 <= count <= maximum:
            result[field] = count
    codes = value.get('quality_violation_codes')
    if isinstance(codes, (list, tuple)):
        result['quality_violation_codes'] = list(dict.fromkeys(
            code for code in codes[:32]
            if isinstance(code, str) and code in _QUALITY_VIOLATION_CODES
        ))
    code = value.get('quality_error_code')
    if isinstance(code, str) and code in REWRITE_ERROR_CODES | {
            'REPLY_QUALITY_BLOCKED', 'REVIEW_FAILED', 'REVIEWER_UNAVAILABLE', 'REVIEWER_RESPONSE_INVALID',
            'REVIEWER_DISABLED', 'FRESH_INTIMACY_CLAIMS_REQUIRED',
            'INTIMACY_CLAIM_SOURCE_CONFLICT', 'INTIMACY_REQUEST_INCONSISTENT'}:
        result['quality_error_code'] = code
    stage = value.get('quality_failure_stage')
    if isinstance(stage, str) and stage in ('review', 'rewrite_evidence', 'rewrite', 'rewrite_validation', 'final_review'):
        result['quality_failure_stage'] = stage
    elif result.get('quality_status') == 'blocked':
        result['quality_failure_stage'] = (
            'rewrite_evidence' if code == 'REWRITE_EVIDENCE_INVALID' else
            'rewrite_validation' if isinstance(code, str) and code in {'REWRITE_OUTPUT_EMPTY', 'REWRITE_OUTPUT_INVALID'} else
            'rewrite' if isinstance(code, str) and code in REWRITE_ERROR_CODES else
            'final_review' if result.get('reviewer_calls') == 2 else 'review')
    # Numeric phase evidence is shared by letters and chat. Private cached text
    # and keys never enter diagnostic bundles through these nested maps.
    timing = value.get('stage_timing_seconds')
    if isinstance(timing, Mapping):
        safe = {name: round(float(timing[name]), 4)
                for name in ('world', 'emotion', 'interpretation', 'history', 'decision', 'writer',
                             'silence_authorization', 'quality', 'total')
                if type(timing.get(name)) in (int, float) and 0 <= timing[name] <= 86400}
        if safe:
            result['stage_timing_seconds'] = safe
    for field in ('stage_cache_hits', 'stage_actual_calls'):
        counts = value.get(field)
        if isinstance(counts, Mapping):
            safe = {name: counts[name] for name in ('writer', 'reviewer', 'rewriter')
                    if type(counts.get(name)) is int and 0 <= counts[name] <= 100}
            if safe:
                result[field] = safe
    return result


def project_chat_task(value: Mapping[str, object]) -> dict[str, object]:
    result = {}
    for field, allowed in {
        'candidate_analysis_status': {'COMPLETED', 'FAILED'},
        'candidate_analysis_retry_status': {'TERMINAL_REJECTION', 'EXHAUSTED'},
        'generation_failure_notice': {'SENDING', 'DELIVERED', 'UNKNOWN'},
    }.items():
        status = value.get(field)
        if isinstance(status, str) and status in allowed:
            result[field] = status
    attempts = value.get('candidate_analysis_attempts')
    if type(attempts) is int and 0 <= attempts <= 100_000:
        result['candidate_analysis_attempts'] = attempts
    reason = value.get('candidate_analysis_failure_reason')
    if isinstance(reason, str) and reason in {
        'PRIVATE_WORLD_CANDIDATE_ANALYSIS_UNAVAILABLE', 'PRIVATE_WORLD_CANDIDATE_CACHE_INVALID',
    }:
        result['candidate_analysis_failure_reason'] = reason
    result.update(project_reply_quality(value))
    if value.get('voice_prepare_status') in {'running', 'completed', 'timeout', 'failed', 'cancelled'}:
        result['voice_prepare_status'] = value['voice_prepare_status']
    for field in ('voice_prepare_seconds', 'voice_prepare_timeout_seconds'):
        seconds = value.get(field)
        if type(seconds) in (int, float) and 0 <= seconds <= 86400:
            result[field] = round(float(seconds), 4)
    if value.get('channel') in ('qq', 'wechat'):
        result['channel'] = value['channel']
        if value.get('generation_interrupted') is True:
            result['generation_interrupted'] = True
        if type(value.get('generation_retryable')) is bool:
            result['generation_retryable'] = value['generation_retryable']
        context = value.get('generation_failure_context')
        if isinstance(context, Mapping):
            from .failure_context import provider_failure_context
            safe_context = provider_failure_context(context)
            if safe_context:
                result['generation_failure_context'] = safe_context
        failures = value.get('generation_failures')
        if isinstance(failures, list):
            from .failure_context import provider_failure_context
            safe_failures = []
            for receipt in failures[:2]:
                if not isinstance(receipt, Mapping) or type(receipt.get('generation_attempt')) is not int or receipt['generation_attempt'] not in (1, 2):
                    continue
                safe = provider_failure_context(receipt)
                if not safe:
                    continue
                safe['generation_attempt'] = receipt['generation_attempt']
                if type(receipt.get('retryable')) is bool:
                    safe['retryable'] = receipt['retryable']
                safe_failures.append(safe)
            if safe_failures:
                result['generation_failures'] = safe_failures
        timings = {'now', 'close_turn', 'wait_user', 'defer', 'no_reply'}
        if isinstance(value.get('companion_timing'), str) and value['companion_timing'] in timings:
            result['companion_timing'] = value['companion_timing']
        proposed = value.get('companion_proposed_timing')
        record = value.get('companion_decision')
        if proposed is None and isinstance(record, Mapping):
            plan = record.get('plan')
            proposal = plan.get('proposal') if isinstance(plan, Mapping) else None
            proposed = proposal.get('timing') if isinstance(proposal, Mapping) else None
        if isinstance(proposed, str) and proposed in timings:
            result['companion_proposed_timing'] = proposed
        if isinstance(value.get('skip_reason'), str) and value['skip_reason'] in {'USER_REQUESTED_WAIT', 'USER_REQUESTED_NO_REPLY',
                'MERGED_RECEIPT', 'STALE_REPLY', 'USER_PRIORITY', 'DUPLICATE_CONTENT',
                'CONTACT_SUPERSEDED', 'PROACTIVE_NO_REPLY'}:
            result['skip_reason'] = value['skip_reason']
        if type(value.get('user_controls_applied')) is bool:
            result['user_controls_applied'] = value['user_controls_applied']
    if value.get('delivery_status') in ('RECEIVED', 'GENERATING', 'GENERATED', 'MEDIA_PENDING', 'SENDING', 'DELIVERY_UNCONFIRMED', 'DELIVERED', 'FAILED', 'SKIPPED'):
        result['delivery_status'] = value['delivery_status']
    code = value.get('consumer_error_code')
    if isinstance(code, str) and _CODE_RE.fullmatch(code):
        result['consumer_error_code'] = code
    if 'channel' in result:
        if value.get('decision_rejection_reason') in {
                'FIELDS', 'JSON_SYNTAX', 'VALUE_TYPE_OR_TIME', 'TEXT_OR_SKIP_TYPE',
                'DELIVERY_OR_LISTENING', 'PREFERENCES', 'EVIDENCE_TYPE',
                'EMPTY_OR_SKIPPED_REPLY', 'CONTROL_MARKER', 'REPEATED_REPLY',
                'SILENCE_INVALID', 'SILENCE_UNSUPPORTED', 'SILENCE_NOT_AUTHORIZED', 'MEDIA_WITHOUT_PLAN',
                'RECOVERY_ACTION_WITHOUT_PLAN'}:
            result['decision_rejection_reason'] = value['decision_rejection_reason']
        if type(value.get('voice_ready')) is bool:
            result['voice_ready'] = value['voice_ready']
        if value.get('delivery_basis') in ('AUXILIARY_TEXT_RECOVERY', 'QQ_DEFAULT_VOICE', 'SPEAKER_UNAVAILABLE', 'VERBATIM_TEXT',
                'JEV_MEDIA_PLAN', 'PROACTIVE_MEDIA_PLAN', 'WECHAT_TEXT', 'TRANSPORT_UNAVAILABLE',
                'PROVIDER_UNAVAILABLE', 'WRITER_SELECTION', 'VOICE_RENDER_FAILED', 'VOICE_RENDER_TIMEOUT'):
            result['delivery_basis'] = value['delivery_basis']
        if value.get('requested_format') in ('text', 'voice'):
            result['requested_format'] = value['requested_format']
        if value.get('delivered_format') in ('text', 'audio'):
            result['delivered_format'] = value['delivered_format']
        # The exchange ID is already an application hash, never a QQ account or
        # platform message ID. Domain-separate it again for support correlation.
        identifier = value.get('letter_id')
        reference = value.get('turn_ref')
        if isinstance(identifier, str) and re.fullmatch(r'im-[0-9a-f]{64}', identifier):
            result['turn_ref'] = 'chat-' + hashlib.sha256(
                ('diagnostic-chat:' + identifier).encode()).hexdigest()[:24]
        elif isinstance(reference, str) and re.fullmatch(r'chat-[0-9a-f]{24}', reference):
            result['turn_ref'] = reference
        attempts = value.get('generation_attempts')
        if type(attempts) is int and 0 <= attempts <= 2:
            result['generation_attempts'] = attempts
        projected_times = value.get('timeline')
        if not isinstance(projected_times, Mapping):
            projected_times = {}
        timeline = {}
        for target, source in (('platform_sent_at', 'user_sent_at'),
                               ('processing_started_at', 'life_received_at'),
                               ('delivered_at', 'private_world_occurred_at')):
            if target == 'delivered_at' and result.get('delivery_status') != 'DELIVERED':
                continue
            raw = value.get(source, projected_times.get(target))
            if not isinstance(raw, str) or len(raw) > 40:
                continue
            try:
                stamp = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                if stamp.tzinfo is None:
                    continue
                stamp = stamp.astimezone(timezone.utc)
            except (ValueError, OverflowError):
                continue
            if 2000 <= stamp.year <= 2100:
                timeline[target] = stamp.isoformat().replace('+00:00', 'Z')
        if timeline:
            result['timeline'] = timeline
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
        projected.update(project_chat_task(item))
        from .photo import project_photo
        projected.update(project_photo(item))
        if item.get('reply_capability_tier') in ('text', 'audio', 'video'):
            projected['reply_capability_tier'] = item['reply_capability_tier']
        if type(item.get('display_status')) is int and -1 <= item['display_status'] <= 10:
            projected['display_status'] = item['display_status']
        for field in ('audio_available', 'video_available', 'text_available'):
            if type(item.get(field)) is bool:
                projected[field] = item[field]
        duration = item.get('audio_duration_seconds')
        if type(duration) in (int, float) and 0 <= duration <= 86400:
            projected['audio_duration_seconds'] = duration
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
        for name in ("video_reply_enabled", "reply_video_enabled"):
            if type(item.get(name)) is bool:
                projected[name] = item[name]
        for name in ("reply_routes", "reply_route_videos"):
            flags = item.get(name)
            if isinstance(flags, Mapping):
                projected[name] = {key: flags[key] for key in ("voice_reply", "singing_video", "voice_song_video") if type(flags.get(key)) is bool}
        contexts = item.get("explicit_requests")
        if isinstance(contexts, (list, tuple)):
            projected["explicit_requests"] = [key for key in ("explicit_voice_reply_request", "explicit_video_reply_request", "explicit_video_output_request",
                "explicit_performance_or_adaptation_request", "explicit_voice_and_song_request") if key in contexts]
        if isinstance(item.get("route_reason"), str) and item["route_reason"] in {"explicit_media_requested", "media_components_required", "reply_route_disabled", "media_not_warranted"}:
            projected["route_reason"] = item["route_reason"]
        if isinstance(item.get("request_disposition"), str) and item["request_disposition"] in {"none", "discuss", "fulfill", "refuse", "defer"}:
            projected["request_disposition"] = item["request_disposition"]
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
    if runtime and event in {'daily_life_failed', 'daily_life_frontend_failed'}:
        from runtime.diagnostics.failure_context import project_daily_life_failure
        return {'event': event, **project_daily_life_failure(source)}
    if runtime and event == 'history_relationship_failed':
        record = project_history_relationship_failure({**source, 'status': 'FAILED'})
        record['status'] = 'failed'
        return record
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
    # Startup timing: a bounded step name and durations, never paths or content.
    if "phase" in source:
        phase = source["phase"]
        if isinstance(phase, str) and _PHASE_RE.fullmatch(phase):
            record["phase"] = phase
    # Why the launcher retried or replaced something: a bounded token only.
    reason = source.get("reason")
    if isinstance(reason, str) and _PHASE_RE.fullmatch(reason):
        record["reason"] = reason
    for name in ("elapsed_seconds", "preparation_seconds"):
        value = source.get(name)
        if type(value) in (int, float) and 0 <= value <= 3600:
            record[name] = round(float(value), 3)
    if runtime:
        if event in {'personal_chat_transport_closed', 'personal_chat_transport_state', 'personal_chat_exchange_cancelled',
                     'personal_chat_decision_normalized', 'personal_chat_decision_warning'}:
            if source.get('channel') in {'qq', 'wechat'}:
                record['channel'] = source['channel']
        if event == 'personal_chat_transport_closed':
            if type(source.get('processing')) is bool:
                record['processing'] = source['processing']
            for key in ('pending_actions', 'response_queue', 'intake_queue', 'control_queue'):
                if type(source.get(key)) is int and 0 <= source[key] <= 64:
                    record[key] = source[key]
            if type(source.get('close_code')) is int and 1000 <= source['close_code'] <= 4999:
                record['close_code'] = source['close_code']
            if source.get('transport_error') in {'NONE', 'TIMEOUT', 'CONNECTION', 'CLIENT', 'OTHER'}:
                record['transport_error'] = source['transport_error']
        from runtime.diagnostics.failure_context import project_failure_context
        record.update(project_failure_context(source))
        record.update(project_reply_quality(source))
        if event == "history_recall":
            from runtime.diagnostics.recall_trace import project
            record.update(project(dict(source)))
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
        if isinstance(source.get('provider'), str) and source['provider'] in {'latentsync', 'breeze', 'minimax', 'soulx', 'roformer', 'ffmpeg', 'ace_step_xl', 'whisper'}:
            record['provider'] = source['provider']
        if source.get('provider') == 'latentsync':
            from runtime.media.latentsync_reply import project_failure_context
            record.update(project_failure_context(source))
        raw = source.get('diagnostic')
        if isinstance(raw, str) and len(raw) <= 4096:
            try:
                detail = json.loads(raw)
            except (ValueError, TypeError):
                detail = dict(part.strip().split('=', 1) for part in raw.split(';') if '=' in part)
            if isinstance(detail, Mapping):
                if "worker" in detail:
                    from tts.external_breeze_worker import project_worker_status
                    worker = project_worker_status(detail["worker"])
                    if worker:
                        record["worker"] = worker
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
                codes = detail.get('exception_codes')
                if isinstance(codes, str):
                    record['exception_codes'] = [
                        code.strip() for code in codes.split('>')[:8]
                        if _CODE_RE.fullmatch(code.strip())
                    ]
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


def _with_recall_tail(runtime_tail, recall_tail):
    if not isinstance(runtime_tail, Sequence) or isinstance(runtime_tail, (str, bytes, bytearray)):
        raise _invalid()
    if not isinstance(recall_tail, Sequence) or isinstance(recall_tail, (str, bytes, bytearray)):
        raise _invalid()
    # Keep the existing strict validation of the source tail before bounding it.
    if len(runtime_tail) > MAX_TAIL_RECORDS or len(recall_tail) > 32:
        raise _invalid()
    from runtime.diagnostics.recall_trace import project
    validated = [_project_tail_record(row, runtime=True) for row in runtime_tail]
    safe = [project(dict(row)) for row in recall_tail if isinstance(row, Mapping)]
    return [*validated, *(row for row in safe if row)][-MAX_TAIL_RECORDS:]


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
            "revision": 2,
            "features": ["capability_tiers", "offline_components", "delivery_projection", "audio_download",
                         "natural_voice_chunks", "worker_progress", "waveform_styles", "reply_route_preview_diagnostics",
                         "history_relationship_failure_codes", "route_failure_context", "history_recall_delivery",
                         "reply_quality_violation_codes", "daily_life_failure_stages"],
        }),
        "summary.json": _json_bytes(summary),
        "health.json": _json_bytes(health),
        "install.json": _json_bytes(install),
        "tasks.json": _json_bytes(tasks),
        "launcher-tail.jsonl": _project_tail(values["launcher_tail"], runtime=False),
        "runtime-tail.jsonl": _project_tail(
            _with_recall_tail(values["runtime_tail"], values.get("recall_tail", ())), runtime=True),
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
