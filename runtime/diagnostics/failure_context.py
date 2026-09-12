"""Finite failure metadata; never retain exception text or request content."""

STAGES = {"configuration", "request", "http_response", "response_json", "tool_parse", "route_validation", "internal"}
CODES = {"PROVIDER_QUOTA_EXHAUSTED", "PROVIDER_TIMEOUT", "PROVIDER_PROTOCOL", "PROVIDER_UNAVAILABLE", "PROVIDER_RETRYABLE", "PROVIDER_REJECTED", "GATEWAY_OTHER"}
KINDS = {"TimeoutError", "TypeError", "ValueError", "AttributeError", "RuntimeError", "ClientConnectorError", "ClientConnectorCertificateError", "ClientConnectorSSLError", "ServerDisconnectedError", "ClientPayloadError", "OTHER"}
DETAILS = {"invalid_json", "invalid_response_shape", "missing_tools", "invalid_tool_entry", "invalid_tool_name", "invalid_tool_arguments"}
DETAILS |= {
    "route_tool_count", "route_tool_name", "route_fields", "route_values",
    "route_contexts", "route_booleans", "route_disposition", "route_current_work",
    "route_music_context", "route_text_constraints", "route_voice_constraints",
    "route_music_constraints",
}


def project_failure_context(source):
    result = {}
    for key, allowed in (("failure_stage", STAGES), ("failure_detail", DETAILS), ("provider_code", CODES), ("exception_type", KINDS)):
        value = source.get(key)
        if isinstance(value, str) and value in allowed:
            result[key] = value
    status = source.get("http_status")
    if type(status) is int and 100 <= status <= 599:
        result["http_status"] = status
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
    })
