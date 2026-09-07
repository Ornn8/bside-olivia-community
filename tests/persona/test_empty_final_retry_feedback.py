"""Synthetic transport feedback checks; no provider or private corpus calls."""
import copy
import json
from pathlib import Path

import pytest

from llm_gateway import GatewayRequestScope
from runtime.persona.persona_loader import load_persona
from runtime.reply import reply_model_quality as quality


FEEDBACK = (
    "\n\nThe previous attempt returned no final JSON. Repeat the same review and return "
    "a complete, non-empty final JSON object in the required schema. Do not output "
    "reasoning. Keep the review criteria unchanged; do not assume a passing verdict."
)


def run_review(monkeypatch, first, *, mode="text_letter", second=None):
    snapshot = load_persona(Path(__file__).resolve().parents[2] / "linli_character/persona_release_v2.json").snapshot
    authorities = tuple(a for a in quality._build_release_layer_authorities(snapshot, mode=mode)
                        if a.name in {"focus_response", "autonomy_life"})
    seen = []

    async def complete(gateway, messages, timeout, request_id, gateway_scope=None, **kwargs):
        layer = json.loads(messages[-1]["content"])["layer"]
        previous = sum(row["layer"] == layer for row in seen)
        seen.append({"layer": layer, "reference": messages, "snapshot": copy.deepcopy(messages),
                     "scope": gateway_scope, "timeout": timeout})
        if layer == "focus_response":
            value = first if previous == 0 else second
            if isinstance(value, Exception):
                raise value
            if value is not None:
                return value
        return json.dumps({"layer": layer, "score": 2, "hard_violations": [], "drift_detected": False})

    monkeypatch.setattr(quality, "_complete_layer_text", complete)
    try:
        result = quality._complete_layer_reviews(
            object(), authorities, candidate="Synthetic reply.", current_user_input="Synthetic input.",
            character_reply_history="", memory_evidence={}, relationship_context={}, mode=mode,
            evidence_bound=mode == "text_letter", timeout_seconds=600,
            gateway_scope=GatewayRequestScope.JSON_MAX_REASONING,
        )
    except quality._ReviewDiagnosticsError as exc:
        result = exc
    return result, seen


@pytest.mark.parametrize("empty", ["", " \n\t "])
def test_empty_retry_adds_feedback_without_mutating_original_or_sibling(monkeypatch, empty):
    result, seen = run_review(monkeypatch, empty)
    assert not isinstance(result, Exception)
    focus = [row for row in seen if row["layer"] == "focus_response"]
    assert len(focus) == 2 and len(seen) == 3
    assert focus[1]["snapshot"][0]["content"] == focus[0]["snapshot"][0]["content"] + FEEDBACK
    assert focus[1]["snapshot"][1:] == focus[0]["snapshot"][1:]
    assert all(row["reference"] == row["snapshot"] for row in seen)
    assert all(row["scope"] is GatewayRequestScope.JSON_MAX_REASONING and row["timeout"] == 600 for row in seen)
    assert all(FEEDBACK not in row["snapshot"][0]["content"] for row in seen if row["layer"] != "focus_response")


def test_second_empty_fails_closed_without_a_third_attempt(monkeypatch):
    result, seen = run_review(monkeypatch, "", second=" ")
    assert isinstance(result, quality._ReviewDiagnosticsError)
    assert any(d.reason is quality.ReviewFailureReason.EMPTY_TEXT for d in result.diagnostics)
    assert sum(row["layer"] == "focus_response" for row in seen) == 2


@pytest.mark.parametrize("first", ["{}", quality._GatewayInvocationFailure(retryable=True)])
def test_nonempty_contract_or_transport_retry_keeps_request_unchanged(monkeypatch, first):
    result, seen = run_review(monkeypatch, first)
    focus = [row for row in seen if row["layer"] == "focus_response"]
    if isinstance(first, Exception):
        assert not isinstance(result, Exception)
        assert len(focus) == 2
        assert focus[0]["snapshot"] == focus[1]["snapshot"]
    else:
        assert isinstance(result, quality._ReviewDiagnosticsError)
    assert all(FEEDBACK not in row["snapshot"][0]["content"] for row in focus)


def test_video_empty_final_does_not_gain_retry_feedback(monkeypatch):
    result, seen = run_review(monkeypatch, "", mode="spoken_video")
    assert isinstance(result, quality._ReviewDiagnosticsError)
    assert sum(row["layer"] == "focus_response" for row in seen) == 1
    assert all(FEEDBACK not in row["snapshot"][0]["content"] for row in seen)


@pytest.mark.parametrize("second_empty", [False, True])
def test_real_adapter_empty_stop_reaches_reviewer_feedback(monkeypatch, second_empty):
    from datetime import datetime, timezone
    from llm_gateway import GatewayConfig, OpenAICompatibleAdapter
    from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
    adapter = OpenAICompatibleAdapter(GatewayConfig(provider="openai_compatible",
        base_url="https://go.example.test/v1", model="deepseek-v4-flash"))
    bodies = []

    async def post(body, request_id, **kwargs):
        layer = json.loads(body["messages"][-1]["content"])["layer"]
        attempt = sum(row[0] == layer for row in bodies)
        bodies.append((layer, copy.deepcopy(body)))
        answer = {"layer": layer, "score": 2, "hard_violations": [], "drift_detected": False}
        if layer in {"identity_boundary", "continuity_memory", "voice_style"}:
            answer.update(hard_evidence=[], independent_soft_issue=False)
        if layer == "identity_boundary":
            answer.update(intimacy_request="none", intimacy_claims=[])
        text = "" if layer == "focus_response" and (attempt == 0 or second_empty) else json.dumps(answer)
        return {"choices": [{"finish_reason": "stop", "message": {"content": text,
                              "reasoning_content": "PRIVATE_SYNTHETIC_REASONING"}}]}

    monkeypatch.setattr(adapter, "_post_json", post)
    context = ReplyContext.create(ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 1, 1, tzinfo=timezone.utc), "system_clock"))
    reviewer = quality.GatewayPersonaReviewer(adapter,
        Path(__file__).resolve().parents[2] / "linli_character/persona_release_v2.json", 600,
        reasoning_timeout_seconds=600)
    result = reviewer.review("Synthetic candidate.", context)
    focus = [body for layer, body in bodies if layer == "focus_response"]
    assert len(focus) == 2
    assert focus[1]["messages"][0]["content"] == focus[0]["messages"][0]["content"] + FEEDBACK
    assert focus[1]["messages"][1:] == focus[0]["messages"][1:]
    assert all(body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "max"
               and "response_format" not in body for _,body in bodies)
    assert "PRIVATE_SYNTHETIC_REASONING" not in json.dumps(bodies)
    if second_empty:
        assert result.error_code == "REVIEWER_UNAVAILABLE"
        assert any(d.reason is quality.ReviewFailureReason.EMPTY_TEXT for d in reviewer.last_failure_diagnostics)
    else:
        assert result.verdict.value == "pass"
