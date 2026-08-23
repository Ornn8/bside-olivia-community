"""Versioned local HTTP contract for the B02 compatibility boundary.

This module contains only route metadata and sanitized response helpers.  It
does not know about the original client, local filesystem paths, user data, or
any provider credentials.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable


CONTRACT_VERSION = "b02.v1"
SCHEMA_VERSION = 1
HEALTH_PROFILE_CORE = "core"
HEALTH_PROFILE_LLM = "llm"
HEALTH_PROFILE_MEMORY = "memory"
HEALTH_PROFILE_ASR = "asr"


ERROR_CODES: dict[str, dict[str, Any]] = {
    "INVALID_JSON": {"http_status": 400, "retryable": False},
    "INVALID_BODY": {"http_status": 400, "retryable": False},
    "MISSING_FIELD": {"http_status": 400, "retryable": False},
    "INVALID_FIELD_TYPE": {"http_status": 400, "retryable": False},
    "INVALID_CONTENT": {"http_status": 400, "retryable": False},
    "CONTENT_TOO_LONG": {"http_status": 400, "retryable": False},
    "INVALID_SCOPE": {"http_status": 400, "retryable": False},
    "INVALID_PROFILE": {"http_status": 400, "retryable": False},
    "INVALID_IDEMPOTENCY_KEY": {"http_status": 400, "retryable": False},
    "READ_ONLY_SCOPE": {"http_status": 403, "retryable": False},
    "CORS_ORIGIN_DENIED": {"http_status": 403, "retryable": False},
    "LETTER_NOT_FOUND": {"http_status": 404, "retryable": False},
    "MIDI_JOB_NOT_FOUND": {"http_status": 404, "retryable": False},
    "METHOD_NOT_ALLOWED": {"http_status": 405, "retryable": False},
    "IDEMPOTENCY_CONFLICT": {"http_status": 409, "retryable": False},
    "LETTER_SUPERSEDED": {"http_status": 410, "retryable": False},
    "INTERNAL_ERROR": {"http_status": 500, "retryable": False},
    "LLM_UNAVAILABLE": {"http_status": 503, "retryable": True},
    "LLM_TIMEOUT": {"http_status": 503, "retryable": True},
    "LLM_PROVIDER_REJECTED": {"http_status": 503, "retryable": False},
    "LLM_PROTOCOL_ERROR": {"http_status": 503, "retryable": False},
    "LETTER_RESEND_NOT_IMPLEMENTED": {"http_status": 501, "retryable": False},
    "LETTER_SHARE_NOT_IMPLEMENTED": {"http_status": 501, "retryable": False},
    "PREFERENCE_SURVEY_NOT_IMPLEMENTED": {"http_status": 501, "retryable": False},
    "MUSIC_WRITE_NOT_IMPLEMENTED": {"http_status": 501, "retryable": False},
    "MIDI_UPLOAD_NOT_IMPLEMENTED": {"http_status": 501, "retryable": False},
    "MIDI_NOT_IMPLEMENTED": {"http_status": 501, "retryable": False},
    "MIDI_IMPORT_NOT_IMPLEMENTED": {"http_status": 501, "retryable": False},
    "PROFILE_EDIT_NOT_IMPLEMENTED": {"http_status": 501, "retryable": False},
    "FEEDBACK_NOT_IMPLEMENTED": {"http_status": 501, "retryable": False},
    "SHARE_TOKEN_NOT_IMPLEMENTED": {"http_status": 501, "retryable": False},
    "MEMORY_UNAVAILABLE": {"http_status": 503, "retryable": True},
    "WEBSOCKET_UNAVAILABLE": {"http_status": 501, "retryable": False},
    "ASR_UNAVAILABLE": {"http_status": 501, "retryable": False},
    "ASR_NOT_PROBED": {"http_status": 503, "retryable": True},
    "ASR_NOT_READY": {"http_status": 503, "retryable": True},
    "ASR_RUNTIME_MISSING": {"http_status": 503, "retryable": False},
    "ASR_MODEL_MISSING": {"http_status": 503, "retryable": False},
    "ASR_MODEL_CORRUPT": {"http_status": 503, "retryable": False},
    "ASR_PROVIDER_UNAVAILABLE": {"http_status": 503, "retryable": True},
    "TTS_UNAVAILABLE": {"http_status": 501, "retryable": False},
    "LIVE_UNAVAILABLE": {"http_status": 501, "retryable": False},
    "ROUTE_NOT_IMPLEMENTED": {"http_status": 501, "retryable": False},
}


def _route(
    methods: Iterable[str],
    capability: str,
    *,
    state: str = "available",
    read_only: bool = False,
    error_code: str | None = None,
    evidence: str = "protocol",
) -> dict[str, Any]:
    return {
        "methods": list(methods),
        "capability": capability,
        "state": state,
        "read_only": read_only,
        "error_code": error_code,
        "evidence": evidence,
    }


# Paths are the stable local compatibility paths.  The source protocol uses
# the same /toy base path; no source URL or private identifier is retained.
ROUTES: dict[str, dict[str, Any]] = {
    "/health": _route(["GET"], "core.health", read_only=True, evidence="local"),
    "/toy/signIn": _route(["GET", "POST"], "core.session", read_only=True),
    "/toy/getUserInfo": _route(["GET", "POST"], "core.session", read_only=True),
    "/toy/getPreferenceSurvey": _route(["GET"], "profile.preference.read", read_only=True),
    "/toy/submitPreferenceSurvey": _route(
        ["POST"],
        "profile.preference.write",
        state="not_implemented",
        error_code="PREFERENCE_SURVEY_NOT_IMPLEMENTED",
    ),
    "/toy/letter/list": _route(["GET"], "letters.read", read_only=True),
    "/toy/letter/unread_count": _route(["GET"], "letters.unread", read_only=True),
    "/toy/letter/detail": _route(["GET"], "letters.read", read_only=True),
    "/toy/letter/send": _route(["POST"], "letters.send"),
    "/toy/letter/resend": _route(
        ["POST"],
        "letters.resend",
        state="not_implemented",
        error_code="LETTER_RESEND_NOT_IMPLEMENTED",
    ),
    "/toy/letter/share": _route(
        ["POST"],
        "letters.share",
        state="not_implemented",
        error_code="LETTER_SHARE_NOT_IMPLEMENTED",
    ),
    "/toy/letter/legacy/import": _route(
        ["POST"],
        "letters.legacy_import",
        evidence="local-extension",
    ),
    "/toy/getMusicTypeInfo": _route(["GET"], "music.catalog", read_only=True),
    "/toy/searchSongs": _route(["GET"], "music.catalog", read_only=True),
    "/toy/searchPlaylist": _route(["GET"], "music.catalog", read_only=True),
    "/toy/searchUserSongs": _route(["GET"], "music.catalog", read_only=True),
    "/toy/searchPerformances": _route(["GET"], "music.catalog", read_only=True),
    "/toy/getSongStats": _route(["GET"], "music.catalog", read_only=True),
    "/toy/addPerformance": _route(
        ["POST"],
        "music.write",
        state="not_implemented",
        error_code="MUSIC_WRITE_NOT_IMPLEMENTED",
    ),
    "/toy/editPerformance": _route(
        ["POST"],
        "music.write",
        state="not_implemented",
        error_code="MUSIC_WRITE_NOT_IMPLEMENTED",
    ),
    "/toy/delPerformance": _route(
        ["POST"],
        "music.write",
        state="not_implemented",
        error_code="MUSIC_WRITE_NOT_IMPLEMENTED",
    ),
    "/toy/addToPlaylist": _route(
        ["POST"],
        "music.write",
        state="not_implemented",
        error_code="MUSIC_WRITE_NOT_IMPLEMENTED",
    ),
    "/toy/delFromPlaylist": _route(
        ["POST"],
        "music.write",
        state="not_implemented",
        error_code="MUSIC_WRITE_NOT_IMPLEMENTED",
    ),
    "/toy/deleteUserSong": _route(
        ["POST"],
        "music.write",
        state="not_implemented",
        error_code="MUSIC_WRITE_NOT_IMPLEMENTED",
    ),
    "/toy/genObjectUploadUrl": _route(
        ["POST"],
        "music.midi_upload",
        state="not_implemented",
        error_code="MIDI_UPLOAD_NOT_IMPLEMENTED",
    ),
    "/toy/midi/generate": _route(
        ["POST"],
        "music.midi_generate",
        state="not_implemented",
        error_code="MIDI_NOT_IMPLEMENTED",
    ),
    "/toy/midi/getGenerateResult": _route(["GET"], "music.midi_jobs", read_only=True),
    "/toy/midi/listJobs": _route(["GET"], "music.midi_jobs", read_only=True),
    "/toy/midi/batchGetResult": _route(["GET"], "music.midi_jobs", read_only=True),
    "/toy/midi/cancelGenerate": _route(["POST"], "music.midi_jobs"),
    "/toy/midi/deleteJob": _route(["POST"], "music.midi_jobs"),
    "/toy/midi/importShareCode": _route(
        ["POST"],
        "music.midi_import",
        state="not_implemented",
        error_code="MIDI_IMPORT_NOT_IMPLEMENTED",
    ),
    "/toy/editProfile": _route(
        ["POST"],
        "profile.write",
        state="not_implemented",
        error_code="PROFILE_EDIT_NOT_IMPLEMENTED",
    ),
    "/toy/createFeedback": _route(
        ["POST"],
        "profile.feedback",
        state="not_implemented",
        error_code="FEEDBACK_NOT_IMPLEMENTED",
    ),
    "/toy/generateShareToken": _route(
        ["POST"],
        "profile.share",
        state="not_implemented",
        error_code="SHARE_TOKEN_NOT_IMPLEMENTED",
    ),
}


CAPABILITIES: dict[str, dict[str, Any]] = {
    "core.health": {
        "status": "available",
        "provider": "local-http",
        "required_for": [HEALTH_PROFILE_CORE],
    },
    "core.session": {"status": "available", "provider": "local-memory"},
    "letters.read": {"status": "available", "provider": "local-memory"},
    "letters.unread": {"status": "available", "provider": "local-memory"},
    "letters.send": {
        "status": "degraded",
        "provider": "configured-llm-adapter",
        "probe": "not-run",
    },
    "llm.gateway": {
        "status": "degraded",
        "provider": "configured-llm-adapter",
        "probe": "not-run",
    },
    "llm.streaming": {
        "status": "unavailable",
        "provider": "none",
        "probe": "internal-events-only",
    },
    "letters.resend": {
        "status": "unavailable",
        "provider": "none",
        "reason_code": "LETTER_RESEND_NOT_IMPLEMENTED",
    },
    "letters.share": {
        "status": "unavailable",
        "provider": "none",
        "reason_code": "LETTER_SHARE_NOT_IMPLEMENTED",
    },
    "letters.legacy_import": {
        "status": "available",
        "provider": "sqlite",
        "probe": "in-process",
        "mode": "read-only-atomic-import",
    },
    "memory.local": {
        "status": "unavailable",
        "provider": "none",
        "probe": "not-run",
        "optional": True,
    },
    "memory.legacy": {
        "status": "unavailable",
        "provider": "none",
        "probe": "not-run",
        "mode": "read-only-whole-library-import",
    },
    "memory.conversation": {
        "status": "unavailable",
        "provider": "none",
        "probe": "not-run",
        "mode": "opt-in-clearable-ttl",
    },
    "music.catalog": {
        "status": "available",
        "provider": "sanitized-local-fixture",
        "empty_data_is_valid": True,
    },
    "music.write": {
        "status": "unavailable",
        "provider": "none",
        "reason_code": "MUSIC_WRITE_NOT_IMPLEMENTED",
    },
    "music.midi_upload": {
        "status": "unavailable",
        "provider": "none",
        "reason_code": "MIDI_UPLOAD_NOT_IMPLEMENTED",
    },
    "music.midi_generate": {
        "status": "unavailable",
        "provider": "none",
        "reason_code": "MIDI_NOT_IMPLEMENTED",
    },
    "music.midi_jobs": {
        "status": "available",
        "provider": "local-memory-terminal-state",
    },
    "music.midi_import": {
        "status": "unavailable",
        "provider": "none",
        "reason_code": "MIDI_IMPORT_NOT_IMPLEMENTED",
    },
    "profile.preference.read": {"status": "available", "provider": "local-empty-fixture"},
    "profile.preference.write": {
        "status": "unavailable",
        "provider": "none",
        "reason_code": "PREFERENCE_SURVEY_NOT_IMPLEMENTED",
    },
    "profile.write": {
        "status": "unavailable",
        "provider": "none",
        "reason_code": "PROFILE_EDIT_NOT_IMPLEMENTED",
    },
    "profile.feedback": {
        "status": "unavailable",
        "provider": "none",
        "reason_code": "FEEDBACK_NOT_IMPLEMENTED",
    },
    "profile.share": {
        "status": "unavailable",
        "provider": "none",
        "reason_code": "SHARE_TOKEN_NOT_IMPLEMENTED",
    },
    "native.websocket": {
        "status": "unavailable",
        "provider": "none",
        "reason_code": "WEBSOCKET_UNAVAILABLE",
    },
    "native.asr": {
        "status": "unavailable",
        "provider": "none",
        "reason_code": "ASR_NOT_PROBED",
        "probe": "not-run",
    },
    "text.input.fallback": {
        "status": "available",
        "provider": "text-fallback",
        "is_asr": False,
        "reason_code": "TEXT_INPUT_FALLBACK",
    },
    "native.tts": {
        "status": "unavailable",
        "provider": "none",
        "reason_code": "TTS_UNAVAILABLE",
    },
    "native.live": {
        "status": "unavailable",
        "provider": "none",
        "reason_code": "LIVE_UNAVAILABLE",
    },
}


PROFILES: dict[str, dict[str, Any]] = {
    HEALTH_PROFILE_CORE: {
        "required_capabilities": ["core.health", "core.session", "letters.read", "music.catalog"],
        "optional_capabilities": [
            "letters.send",
            "letters.resend",
            "letters.share",
            "native.websocket",
            "native.asr",
            "text.input.fallback",
            "native.tts",
            "native.live",
        ],
    },
    HEALTH_PROFILE_LLM: {
        "required_capabilities": [],
        "optional_capabilities": ["llm.gateway", "llm.streaming", "letters.send"],
    },
    HEALTH_PROFILE_MEMORY: {
        "required_capabilities": ["memory.local"],
        "optional_capabilities": ["memory.legacy", "memory.conversation"],
    },
    HEALTH_PROFILE_ASR: {
        "required_capabilities": ["native.asr"],
        "optional_capabilities": ["text.input.fallback"],
    },
}


def route_spec(path: str) -> dict[str, Any] | None:
    """Return a defensive copy of the normalized route metadata."""

    return deepcopy(ROUTES.get(path.rstrip("/") or "/"))


def contract_document() -> dict[str, Any]:
    """Return the JSON-serializable contract artifact exposed by healthcheck."""

    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "base_path": "/toy",
        "routes": deepcopy(ROUTES),
        "capabilities": deepcopy(CAPABILITIES),
        "profiles": deepcopy(PROFILES),
        "privacy": {
            "logs_include_request_body": False,
            "logs_include_query_values": False,
            "legacy_import_mode": "read-only-atomic-import",
            "original_assets_in_response": False,
        },
    }


def _payload(status: str, error_code: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": status, "error_code": error_code}
    if details:
        payload.update(details)
    return payload


def ok(data: Any = None) -> dict[str, Any]:
    return {"code": 0, "message": "ok", "data": {} if data is None else data}


def error(
    http_code: int,
    error_code: str,
    message: str | None = None,
    *,
    status: str = "FAILED",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": http_code,
        "message": message or error_code,
        "data": _payload(status, error_code, details),
    }


def unavailable(error_code: str, capability: str) -> dict[str, Any]:
    return error(
        501,
        error_code,
        "capability unavailable",
        status="UNAVAILABLE",
        details={"capability": capability},
    )


def not_implemented(error_code: str = "ROUTE_NOT_IMPLEMENTED") -> dict[str, Any]:
    return error(
        501,
        error_code,
        "NOT_IMPLEMENTED",
        status="NOT_IMPLEMENTED",
    )


def error_metadata(error_code: str) -> dict[str, Any]:
    """Return stable retry metadata without exposing provider details."""

    return deepcopy(ERROR_CODES.get(error_code, {"http_status": 500, "retryable": False}))
