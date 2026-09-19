"""Offline official structured-review protocol boundaries."""
import asyncio
import copy
import json
from pathlib import Path

import pytest

from llm_gateway import (
    GatewayConfig, GatewayRequestScope, OpenAICompatibleAdapter,
    ProviderEmptyResponse, ProviderProtocolError,
)
from runtime.reply import reply_model_quality as quality


JSON_SCOPE = GatewayRequestScope.JSON_MAX_REASONING
AUTONOMY_FORMAT = {
    "type": "json_schema", "name": "autonomy_life_review", "schema": {
        "type": "object", "properties": {
            "layer": {"type": "string", "enum": ["autonomy_life"]},
            "score": {"type": "integer", "enum": [0, 1, 2]},
            "hard_violations": {"type": "array", "items": {"type": "string", "enum": [
                "GENERIC_COUNSELOR", "IDENTITY_DRIFT"]}, "maxItems": 2},
            "drift_detected": {"type": "boolean"}},
        "required": ["layer", "score", "hard_violations", "drift_detected"],
        "additionalProperties": False}}
MESSAGES = [{"role": "system", "content": "Review as JSON."},
            {"role": "user", "content": "Synthetic input."}]


def response(text='{"ok":true}', status="completed"):
    return {"status": status, "error": None, "incomplete_details": None, "output": [
        {"type": "reasoning", "status": "completed", "content": [
            {"type": "reasoning_text", "text": "PRIVATE_REASONING"}]},
        {"type": "message", "role": "assistant", "status": "completed", "content": [
            {"type": "output_text", "text": text}]}]}


def adapter(url="https://api.deepseek.com", **kwargs):
    return OpenAICompatibleAdapter(GatewayConfig(
        provider="openai_compatible", base_url=url, model="deepseek-v4-flash",
        reasoning_timeout_seconds=600, **kwargs))


@pytest.mark.parametrize("url", ["https://api.deepseek.com", "https://api.deepseek.com/v1/"])
def test_official_scope_uses_canonical_responses_and_max(monkeypatch, url):
    gateway = adapter(url)
    seen = []

    async def post(body, request_id, **kwargs):
        seen.append((body, kwargs))
        return response()

    monkeypatch.setattr(gateway, "_post_json", post)
    result = asyncio.run(gateway.complete_scoped(MESSAGES, scope=JSON_SCOPE))
    assert result.text == '{"ok":true}'
    assert seen == [({"model": "deepseek-v4-flash", "input": MESSAGES, "stream": False,
                     "reasoning": {"effort": "max"}, "text": {"format": {"type": "json_object"}}},
                    {"max_reasoning": True, "endpoint": "https://api.deepseek.com/responses"})]
    assert gateway.timeout_seconds_for_scope(JSON_SCOPE, default=5) == 600
    assert gateway._request_timeout_seconds(max_reasoning=True) == 600


@pytest.mark.parametrize("change", [
    {"status": "incomplete"}, {"status": "failed"}, {"status": "in_progress"},
    {"status": None}, {"error": {"code": "failure"}},
    {"incomplete_details": {"reason": "max_output_tokens"}},
    {"output": []},
    {"output": "invalid"},
    {"output": [None]},
    {"output": [{"type": "function_call", "arguments": "{}"}]},
    {"output": [{"type": "message", "role": "assistant", "status": "incomplete", "content": []}]},
    {"output": [{"type": "message", "role": "user", "status": "completed", "content": []}]},
    {"output": [{"type": "message", "role": "assistant", "status": "completed", "content": [
        {"type": "reasoning_text", "text": "PRIVATE_REASONING"}]}]},
])
def test_rejects_bad_status_and_never_trusts_top_level_shortcut(monkeypatch, change):
    gateway = adapter()
    data = response()
    data.update(change, output_text='{"ok":true}')

    async def post(*args, **kwargs):
        return data

    monkeypatch.setattr(gateway, "_post_json", post)
    with pytest.raises(ProviderProtocolError):
        asyncio.run(gateway.complete_scoped(MESSAGES, scope=JSON_SCOPE))


@pytest.mark.parametrize("text", ["", " \n\t"])
def test_completed_empty_is_typed_and_reasoning_is_not_final(monkeypatch, text):
    gateway = adapter()

    async def post(*args, **kwargs):
        return response(text)

    monkeypatch.setattr(gateway, "_post_json", post)
    with pytest.raises(ProviderEmptyResponse):
        asyncio.run(gateway.complete_scoped(MESSAGES, scope=JSON_SCOPE))


@pytest.mark.parametrize("url,scope", [
    ("https://api.deepseek.com", GatewayRequestScope.TEXT_LETTER_MAX_REASONING),
    ("https://api.deepseek.com", GatewayRequestScope.BACKGROUND_REASONING),
    ("https://api.deepseek.com", None),
    ("https://api.deepseek.com/other", JSON_SCOPE),
    ("http://api.deepseek.com", JSON_SCOPE),
    ("https://go.example.test/v1", JSON_SCOPE),
])
def test_other_consumers_keep_chat_wire(monkeypatch, url, scope):
    gateway = adapter(url)
    seen = []

    async def post(body, request_id, **kwargs):
        seen.append((body, kwargs))
        return {"choices": [{"finish_reason": "stop", "message": {"content": "Plain reply."}}]}

    monkeypatch.setattr(gateway, "_post_json", post)
    result = asyncio.run(gateway.complete_scoped(MESSAGES, scope=scope)) if scope else asyncio.run(gateway.complete(MESSAGES))
    assert result.text == "Plain reply."
    assert seen[0][0]["messages"] == MESSAGES
    assert "input" not in seen[0][0] and "endpoint" not in seen[0][1]


def test_other_model_and_explicit_responses_config_are_not_rerouted(monkeypatch):
    for config in (
        GatewayConfig(provider="openai_compatible", base_url="https://api.deepseek.com", model="other-model"),
        GatewayConfig(provider="openai_compatible", base_url="https://api.deepseek.com/v1",
                      model="deepseek-v4-flash", api_style="responses"),
    ):
        gateway = OpenAICompatibleAdapter(config)

        async def post(body, request_id, **kwargs):
            assert "endpoint" not in kwargs
            assert "reasoning" not in body and "text" not in body
            return {"output_text": "Existing protocol behavior."}

        monkeypatch.setattr(gateway, "_post_json", post)
        assert asyncio.run(gateway.complete_scoped(MESSAGES, scope=JSON_SCOPE)).text == "Existing protocol behavior."


@pytest.mark.parametrize("second_empty", [False, True])
def test_real_adapter_reviewer_parser_and_existing_empty_retry(monkeypatch, second_empty):
    from datetime import datetime, timezone
    from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime

    gateway = adapter(stream=True)
    seen = []

    async def forbidden_stream(*args, **kwargs):
        raise AssertionError("JSON reviewer must use nonstream completion")
        yield

    async def post(body, request_id, **kwargs):
        layer = json.loads(body["input"][-1]["content"])["layer"]
        attempt = sum(name == layer for name, _ in seen)
        seen.append((layer, copy.deepcopy(body)))
        answer = {"layer": layer, "score": 2, "hard_violations": [], "drift_detected": False}
        if layer in {"identity_boundary", "continuity_memory", "voice_style"}:
            answer.update(hard_evidence=[], independent_soft_issue=False)
        if layer == "identity_boundary":
            answer.update(intimacy_request="none", intimacy_claims=[])
        assert body["text"]["format"] == (AUTONOMY_FORMAT if layer == "autonomy_life" else {"type": "json_object"})
        empty = layer == "autonomy_life" and (attempt == 0 or second_empty)
        return response(" " if empty else json.dumps(answer))

    monkeypatch.setattr(gateway, "stream_scoped", forbidden_stream)
    monkeypatch.setattr(gateway, "_post_json", post)
    context = ReplyContext.create(ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 1, 1, tzinfo=timezone.utc), "system_clock"))
    reviewer = quality.GatewayPersonaReviewer(gateway,
        Path(__file__).resolve().parents[2] / "linli_character/persona_release_v2.json",
        600, reasoning_timeout_seconds=600)
    result = reviewer.review("Synthetic candidate.", context)
    attempts = [body for name, body in seen if name == "autonomy_life"]
    assert len(attempts) == 2
    assert attempts[1]["input"][0]["content"] == attempts[0]["input"][0]["content"] + quality._EMPTY_REVIEW_FEEDBACK
    assert attempts[1]["input"][1:] == attempts[0]["input"][1:]
    assert "PRIVATE_REASONING" not in json.dumps(seen)
    if second_empty:
        assert result.error_code == "REVIEWER_UNAVAILABLE"
        assert any(d.reason is quality.ReviewFailureReason.EMPTY_TEXT for d in reviewer.last_failure_diagnostics)
    else:
        assert result.verdict.value == "pass"


def test_optional_structured_interface_delegates_for_existing_gateways():
    from llm_gateway import Gateway, GatewayResponse

    class ExistingGateway(Gateway):
        async def complete_scoped(self, messages, *, scope, request_id=None):
            assert scope is JSON_SCOPE and request_id == "synthetic"
            return GatewayResponse("{}", request_id, "custom", "custom")

    result = asyncio.run(ExistingGateway().complete_structured_scoped(
        MESSAGES, scope=JSON_SCOPE, request_id="synthetic", response_format=AUTONOMY_FORMAT))
    assert result.text == "{}"


@pytest.mark.parametrize("url,scope", [
    ("https://api.deepseek.com", GatewayRequestScope.TEXT_LETTER_MAX_REASONING),
    ("https://api.deepseek.com", GatewayRequestScope.BACKGROUND_REASONING),
    ("https://go.example.test/v1", JSON_SCOPE),
])
def test_structured_option_does_not_change_other_consumers(monkeypatch, url, scope):
    gateway = adapter(url)

    async def post(body, request_id, **kwargs):
        assert "messages" in body and "text" not in body and "endpoint" not in kwargs
        return {"choices": [{"finish_reason": "stop", "message": {"content": "Plain reply."}}]}

    monkeypatch.setattr(gateway, "_post_json", post)
    result = asyncio.run(gateway.complete_structured_scoped(
        MESSAGES, scope=scope, response_format=AUTONOMY_FORMAT))
    assert result.text == "Plain reply."
