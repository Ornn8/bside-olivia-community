"""Finite failure metadata; never retain exception text or request content."""
from collections import deque
import uuid

_RECENT_FAILURES = deque(maxlen=40)

STAGES = {"configuration", "request", "http_response", "response_json", "tool_parse", "route_validation", "internal",
          "structured_completion", "structured_validation", "tool_completion"}
CODES = {"PROVIDER_QUOTA_EXHAUSTED", "PROVIDER_TIMEOUT", "PROVIDER_PROTOCOL", "PROVIDER_UNAVAILABLE", "PROVIDER_RETRYABLE", "PROVIDER_REJECTED", "GATEWAY_OTHER"}
CODES |= {'PROVIDER_USAGE_PENDING', 'PROVIDER_REQUEST_DUPLICATE', 'PROVIDER_AUTH_FAILED'}
KINDS = {"TimeoutError", "TypeError", "ValueError", "AttributeError", "RuntimeError", "ClientConnectorError", "ClientConnectorCertificateError", "ClientConnectorSSLError", "ServerDisconnectedError", "ClientPayloadError", "OTHER"}
DETAILS = {"invalid_json", "invalid_response_shape", "missing_tools", "invalid_tool_entry", "invalid_tool_name", "invalid_tool_arguments"}
KINDS.add('ClientConnectorDNSError')
DETAILS |= {'structured_truncated', 'structured_validation_failed', 'tool_truncated',
            'unexpected_tool', 'invalid_tool_schema', 'invalid_tool_count', 'unsupported_tool_fallback',
            'unsupported_response_format', 'unsupported_tools', 'unsupported_tool_choice'}
DETAILS |= {
    "route_tool_count", "route_tool_name", "route_fields", "route_values",
    "route_contexts", "route_booleans", "route_disposition", "route_current_work",
    "route_music_context", "route_text_constraints", "route_voice_constraints",
    "route_music_constraints",
}


def project_failure_context(source):
    result = {}
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


def record_failure(exc):
    _RECENT_FAILURES.append({'event': 'provider_failure', **exception_context(exc)})


def failure_snapshot():
    return tuple(dict(item) for item in _RECENT_FAILURES)
