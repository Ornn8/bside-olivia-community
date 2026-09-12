import asyncio
import json
from collections import deque
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from runtime.diagnostics.support_bundle import _project_tail
from runtime.video_reply_settings import VideoReplySettingsStore


@pytest.mark.parametrize("change,detail", [
    ({"extra": "private-letter"}, "route_fields"),
    ({"reason_code": "private-invalid-reason"}, "route_values"),
    ({"music_role": "performance"}, "route_contexts"),
    ({"direct_response_sufficient": "true"}, "route_booleans"),
    ({"request_disposition": "fulfill"}, "route_disposition"),
    ({"music_contexts": ["current_work_relevance"]}, "route_current_work"),
    ({"mode": "voice_reply"}, "route_voice_constraints"),
])
def test_qwen_route_validation_reason_survives_export(change, detail):
    from llm_gateway import GatewayConfig, OpenAICompatibleAdapter
    from letter_triage import LetterReplyRouter
    import local_server as server

    arguments = dict(mode="text_letter", reason_code="direct_words", emotion_level="normal",
        music_contexts=[], music_role="none", music_intent="none", request_disposition="none",
        direct_response_sufficient=True, voice_materially_better=False,
        music_materially_better=False, character_willing=True)
    arguments.update(change)

    async def run():
        async def provider(request):
            body = await request.json()
            assert body["model"] == "qwen3.8-flash"
            assert body["enable_thinking"] is False
            return web.json_response({"choices": [{"message": {"tool_calls": [{"function": {
                "name": "select_reply_mode", "arguments": json.dumps(arguments),
            }}]}}]})
        app = web.Application()
        app.router.add_post('/chat/completions', provider)
        async with TestClient(TestServer(app)) as client:
            gateway = OpenAICompatibleAdapter(GatewayConfig(
                provider="openai_compatible", base_url=str(client.make_url('/')).rstrip('/'),
                model="qwen3.8-flash", requires_api_key=False, max_retries=0))
            return await LetterReplyRouter(gateway, environ={}).classify("private-letter")

    result = asyncio.run(run())
    assert result.reason_code == "router_invalid_result"
    assert result.diagnostic == {"failure_stage": "route_validation", "failure_detail": detail}
    record = server._runtime_diagnostic_record("reply_route_classification_failed", result.diagnostic)
    exported = _project_tail([record], runtime=True)
    assert detail.encode() in exported
    assert b"private-" not in exported


@pytest.mark.parametrize("reason,expected", [
    ("router_quota_exhausted", "LLM_QUOTA_EXHAUSTED"),
    ("router_auth_failed", "LLM_AUTH_FAILED"),
    ("router_timeout", "LLM_TIMEOUT"),
    ("router_rate_limited", "LLM_RATE_LIMITED"),
    ("router_invalid_result", "REPLY_ROUTE_INVALID_RESULT"),
    ("private-key-and-content", "VIDEO_TRIAGE_UNAVAILABLE"),
])
def test_preview_failure_reaches_export_without_private_input(tmp_path, monkeypatch, reason, expected):
    import local_server as server

    monkeypatch.setattr(server, "video_reply_settings_store", VideoReplySettingsStore.initialize(tmp_path))
    monkeypatch.setattr(server, "_RUNTIME_DIAGNOSTIC_EVENTS", deque(maxlen=200))

    async def classify(*args):
        return SimpleNamespace(status="unavailable", reason_code=reason)

    monkeypatch.setattr(server, "_classify_managed_route", classify)

    async def run():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", server.handler)
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/toy/letter/route-preview", json={"content": "private-letter-text"})
            payload = await response.json()
            assert payload["data"]["error_code"] == expected

    asyncio.run(run())
    records = server.runtime_diagnostic_event_snapshot()
    for event in ("reply_route_classification_failed", "reply_route_preview_result"):
        assert any(row.items() >= {"event": event, "status": "FAILED", "error_code": expected}.items() for row in records)
    exported = _project_tail(records, runtime=True)
    assert expected.encode() in exported
    assert b"private-" not in exported


@pytest.mark.parametrize("code,accepted", [("REPLY_ROUTE_CLIENT_TIMEOUT", True), ("private-key", False), (["private-key"], False)])
def test_frontend_report_only_accepts_fixed_codes(monkeypatch, code, accepted):
    import local_server as server
    monkeypatch.setattr(server, "_RUNTIME_DIAGNOSTIC_EVENTS", deque(maxlen=200))
    result = asyncio.run(server.route("POST", "/toy/letter/route-preview-diagnostic", {
        "error_code": code, "content": "private-letter", "key": "private-key",
    }, {}))
    assert (result["code"] == 0) is accepted
    exported = _project_tail(server.runtime_diagnostic_event_snapshot(), runtime=True)
    assert b"private-" not in exported


def test_internal_router_exception_has_sanitized_type():
    from letter_triage import LetterReplyRouter
    class BrokenGateway:
        async def complete_with_tools(self, **kwargs):
            raise AttributeError("private-key and letter")
    result = asyncio.run(LetterReplyRouter(BrokenGateway(), environ={}).classify("private-letter"))
    assert result.diagnostic == {"failure_stage": "internal", "provider_code": "GATEWAY_OTHER", "exception_type": "AttributeError"}


@pytest.mark.parametrize("status,body,stage,code", [
    (400, "private-provider-body", "http_response", "PROVIDER_REJECTED"),
    (404, "private-provider-body", "http_response", "PROVIDER_REJECTED"),
    (429, "private-provider-body", "http_response", "PROVIDER_RETRYABLE"),
    (503, "private-provider-body", "http_response", "PROVIDER_RETRYABLE"),
    (200, "private-invalid-json", "response_json", "PROVIDER_PROTOCOL"),
    (200, '{"choices":[]}', "tool_parse", "PROVIDER_PROTOCOL"),
    (200, '{"choices":[{"message":{"tool_calls":[{"function":{"name":"select_reply_mode","arguments":"private-invalid"}}]}}]}', "tool_parse", "PROVIDER_PROTOCOL"),
])
def test_real_gateway_failure_survives_router_and_export(status, body, stage, code):
    from llm_gateway import GatewayConfig, OpenAICompatibleAdapter
    from letter_triage import LetterReplyRouter
    import local_server as server

    async def run():
        async def provider(request):
            return web.Response(text=body, status=status)
        app = web.Application()
        app.router.add_post('/chat/completions', provider)
        async with TestClient(TestServer(app)) as client:
            gateway = OpenAICompatibleAdapter(GatewayConfig(
                provider="openai_compatible", base_url=str(client.make_url('/')).rstrip('/'),
                model="synthetic", requires_api_key=False, max_retries=0,
            ))
            return await LetterReplyRouter(gateway, environ={}).classify("private-letter")

    result = asyncio.run(run())
    assert result.status == "unavailable"
    assert result.diagnostic["failure_stage"] == stage
    assert result.diagnostic["provider_code"] == code
    if stage != "tool_parse":
        assert result.diagnostic["http_status"] == status
    record = server._runtime_diagnostic_record("reply_route_classification_failed", result.diagnostic)
    exported = _project_tail([record], runtime=True)
    assert stage.encode() in exported and code.encode() in exported
    assert b"private-" not in exported
