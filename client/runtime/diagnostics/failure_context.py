"""Finite failure metadata; never retain exception text or request content."""
from collections import deque
from collections.abc import Mapping
import sqlite3
import uuid

_RECENT_FAILURES = deque(maxlen=40)

DAILY_LIFE_STAGES = {'initialization', 'read', 'request', 'response', 'render'}
DAILY_LIFE_ENDPOINTS = {'daily_life', 'daily_life_history', 'companion_status'}
DAILY_LIFE_CODES = {
    'DAILY_LIFE_UNAVAILABLE', 'DAILY_LIFE_INITIALIZATION_FAILED', 'DAILY_LIFE_DISABLED',
    'DAILY_LIFE_READ_FAILED', 'DAILY_LIFE_REQUEST_FAILED', 'DAILY_LIFE_RESPONSE_INVALID',
    'DAILY_LIFE_RENDER_FAILED',
}
DAILY_LIFE_EXCEPTION_TYPES = {
    'Error', 'SyntaxError', 'RangeError', 'ReferenceError', 'AbortError', 'KeyError',
    'JSONDecodeError', 'OSError', 'PermissionError', 'FileNotFoundError',
    'ImportError', 'ModuleNotFoundError',
    'OperationalError', 'DatabaseError', 'IntegrityError', 'ProgrammingError',
    'TimeoutError', 'TypeError', 'ValueError', 'AttributeError', 'RuntimeError', 'OTHER',
}
_SQLITE_NAMES = {name for name in dir(sqlite3) if name.startswith('SQLITE_')}


def project_daily_life_failure(source):
    """Fixed endpoint IDs and finite error metadata, never exception prose."""
    if not isinstance(source, Mapping):
        return {}
    result = {}
    for key, allowed in (('failure_stage', DAILY_LIFE_STAGES), ('endpoint', DAILY_LIFE_ENDPOINTS),
                         ('error_code', DAILY_LIFE_CODES), ('exception_type', DAILY_LIFE_EXCEPTION_TYPES),
                         ('sqlite_errorname', _SQLITE_NAMES)):
        value = source.get(key)
        if isinstance(value, str) and value in allowed:
            result[key] = value
    for key, low, high in (('http_status', 100, 599), ('sqlite_errorcode', 0, 65535),
                           ('recorded_at_ms', 0, 10_000_000_000_000)):
        value = source.get(key)
        if type(value) is int and low <= value <= high:
            result[key] = value
    if isinstance(source.get('method'), str) and source['method'] in {'GET', 'POST'}:
        result['method'] = source['method']
    return result


def daily_life_failure(exc, stage, *, endpoint='daily_life', http_status=None):
    kind = type(exc).__name__
    return project_daily_life_failure({
        'failure_stage': stage, 'endpoint': endpoint,
        'error_code': {'initialization': 'DAILY_LIFE_INITIALIZATION_FAILED',
                       'read': 'DAILY_LIFE_READ_FAILED', 'request': 'DAILY_LIFE_REQUEST_FAILED',
                       'response': 'DAILY_LIFE_RESPONSE_INVALID', 'render': 'DAILY_LIFE_RENDER_FAILED'}[stage],
        'exception_type': kind if kind in DAILY_LIFE_EXCEPTION_TYPES else 'OTHER',
        'http_status': http_status,
        'sqlite_errorcode': getattr(exc, 'sqlite_errorcode', None),
        'sqlite_errorname': getattr(exc, 'sqlite_errorname', None),
    })

STAGES = {"configuration", "request", "http_response", "response_json", "tool_parse", "route_validation", "internal",
          "structured_completion", "structured_validation", "tool_completion"}
CODES = {"PROVIDER_QUOTA_EXHAUSTED", "PROVIDER_TIMEOUT", "PROVIDER_PROTOCOL", "PROVIDER_UNAVAILABLE", "PROVIDER_RETRYABLE", "PROVIDER_REJECTED", "GATEWAY_OTHER"}
CODES |= {'PROVIDER_USAGE_PENDING', 'PROVIDER_REQUEST_DUPLICATE', 'PROVIDER_AUTH_FAILED', 'PROVIDER_BUSY'}
KINDS = {"TimeoutError", "TypeError", "ValueError", "AttributeError", "RuntimeError", "ClientConnectorError", "ClientConnectorCertificateError", "ClientConnectorSSLError", "ServerDisconnectedError", "ClientPayloadError", "OTHER"}
DETAILS = {"invalid_json", "invalid_response_shape", "missing_tools", "invalid_tool_entry", "invalid_tool_name", "invalid_tool_arguments"}
KINDS.add('ClientConnectorDNSError')
DETAILS |= {'structured_truncated', 'structured_validation_failed', 'tool_truncated',
            'unexpected_tool', 'invalid_tool_schema', 'invalid_tool_count', 'unsupported_tool_fallback',
            'unsupported_response_format', 'unsupported_tools', 'unsupported_tool_choice',
            'output_truncated', 'empty_output', 'invalid_stream_chunk', 'stream_error'}
DETAILS |= {
    "route_tool_count", "route_tool_name", "route_fields", "route_values",
    "route_contexts", "route_booleans", "route_disposition", "route_current_work",
    "route_music_context", "route_text_constraints", "route_voice_constraints",
    "route_music_constraints",
}


import re

# Internal failure codes are fixed identifiers raised by Olivia itself. Only
# these shapes are kept, so arbitrary exception text never enters a bundle.
_CAUSE_CODE = re.compile(r'(?:JEV|LLM|MEM0|MEMORY|PRIVATE_WORLD|DAILY_LIFE|REPLY|IMAGE|WORLD|COMPANION|QUALITY|RECALL|REVIEW|PERSONA)_[A-Z0-9_]{2,60}')
REWRITE_ERROR_CODES = frozenset({
    'REWRITE_FAILED', 'REWRITE_EVIDENCE_INVALID', 'REWRITE_INPUT_TOO_LARGE',
    'REWRITE_OUTPUT_INVALID', 'REWRITE_OUTPUT_EMPTY', 'REWRITE_PROVIDER_UNAVAILABLE',
    'REWRITE_BUDGET_EXHAUSTED',
})


def cause_code(exc):
    """First internal failure code along the exception chain, if any."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        for value in (getattr(exc, 'code', None), *getattr(exc, 'args', ())[:1]):
            if isinstance(value, str) and (value in REWRITE_ERROR_CODES or _CAUSE_CODE.fullmatch(value)):
                return value
        exc = exc.__cause__ or exc.__context__
    return None


def failure_detail(exc):
    """First provider protocol detail along the exception chain, if any."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        detail = getattr(exc, 'diagnostic_detail', None)
        if isinstance(detail, str) and detail in DETAILS:
            return detail
        exc = exc.__cause__ or exc.__context__
    return None


def letter_failure_context(exc):
    kind = type(exc).__name__
    return project_failure_context({'exception_type': kind if kind in KINDS else 'OTHER',
                                    'cause_code': cause_code(exc), 'failure_detail': failure_detail(exc)})


def project_failure_context(source):
    result = {}
    cause = source.get('cause_code')
    if isinstance(cause, str) and (cause in REWRITE_ERROR_CODES or _CAUSE_CODE.fullmatch(cause)):
        result['cause_code'] = cause
    raw = source.get('provider_request_id')
    if isinstance(raw, str):
        try:
            result['provider_request_id'] = str(uuid.UUID(raw))
        except ValueError:
            pass
    for key, allowed in (("failure_stage", STAGES), ("failure_detail", DETAILS), ("provider_code", CODES), ("exception_type", KINDS)):
        value = source.get(key)
        if isinstance(value, str) and value in allowed:
            result[key] = value
    status = source.get("http_status")
    if type(status) is int and 100 <= status <= 599:
        result["http_status"] = status
    fields = source.get("route_missing_fields")
    allowed_fields = {"mode", "reason_code", "emotion_level", "music_contexts",
        "music_role", "music_intent", "request_disposition", "direct_response_sufficient",
        "voice_materially_better", "music_materially_better", "character_willing"}
    if isinstance(fields, list):
        result["route_missing_fields"] = sorted({item for item in fields
            if isinstance(item, str) and item in allowed_fields})
    count = source.get("route_extra_field_count")
    if type(count) is int and 0 <= count <= 1000:
        result["route_extra_field_count"] = count
    return result


def exception_context(exc, stage="request"):
    if type(exc).__name__ == "InvalidGatewayInput":
        stage = "configuration"
    code = getattr(exc, "code", None)
    kind = getattr(exc, "diagnostic_exception_type", type(exc).__name__)
    return project_failure_context({
        "failure_stage": getattr(exc, "diagnostic_stage", stage),
        "failure_detail": getattr(exc, "diagnostic_detail", None),
        "provider_code": code if isinstance(code, str) and code in CODES else "GATEWAY_OTHER",
        "http_status": getattr(exc, "status", None),
        "exception_type": kind if kind in KINDS else "OTHER",
        "provider_request_id": getattr(exc, 'provider_request_id', None),
    })


def provider_failure_context(source):
    """Only provider metadata belongs to a durable chat failure receipt."""
    if not isinstance(source, Mapping):
        return {}
    return project_failure_context({key: source[key] for key in (
        'failure_stage', 'failure_detail', 'provider_code', 'exception_type',
        'http_status', 'provider_request_id') if key in source})


def record_failure(exc):
    _RECENT_FAILURES.append({'event': 'provider_failure', **exception_context(exc)})


def failure_snapshot():
    return tuple(dict(item) for item in _RECENT_FAILURES)
