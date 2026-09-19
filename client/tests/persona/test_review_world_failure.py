"""Inactive review adapters must preserve the writer's state-read distinction."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

import runtime.reply.reply_model_quality as quality
from runtime.reply.reply_reviewer import JsonReviewerAdapter, ReviewerConfig, ReviewVerdict
from tests.persona.test_reply_reviewer import _Transport, _valid_response
from tests.persona.test_reply_model_quality import (
    ROOT, SequencedQualityGateway, _context, _passing_layer_payloads,
)


@pytest.mark.parametrize("available", [False, True])
def test_review_payload_distinguishes_state_read_failure_from_empty_world(available):
    context = replace(_context(), world_state_available=available)
    transport = _Transport(_valid_response())
    JsonReviewerAdapter(transport, ReviewerConfig("synthetic")).review("原来的早餐我记得", context)
    payload = transport.requests[0]
    assert payload["world_state_available"] is available
    relationship = payload["relationship_context"]
    if available:
        assert relationship["relationship_stage"] == "unknown"
        assert payload["known_continuations"] == []
    else:
        assert relationship["state_status"] == "unavailable"
        assert "relationship_stage" not in relationship
        assert "读取失败不表示关系回到初始状态" in relationship["meaning"]
        assert payload["known_continuations"] is None


def test_layered_review_carries_unavailable_status_to_each_factual_layer():
    gateway = SequencedQualityGateway(candidate="早餐我记得", reviews=_passing_layer_payloads())
    reviewer = quality.GatewayPersonaReviewer(gateway, ROOT / "linli_character/persona_release_v2.json", 2)
    result = reviewer.review("早餐我记得", replace(_context(), world_state_available=False))
    assert result.verdict is ReviewVerdict.PASS
    layers = {request["layer"]: request for request in gateway.review_requests}
    assert {"identity_boundary", "continuity_memory"} <= layers.keys()
    for layer in ("identity_boundary", "continuity_memory"):
        assert layers[layer]["world_state_available"] is False
        assert "读取失败不表示关系回到初始状态" in layers[layer]["world_state_meaning"]
    assert "读取失败不表示关系回到初始状态" in layers["continuity_memory"]["memory_evidence"]["world_state"]
    assert "relationship_stage" not in layers["identity_boundary"]["relationship_context"]


@pytest.mark.parametrize("available", [False, True])
def test_rewrite_does_not_replace_unavailable_state_with_initial_relationship(monkeypatch, available):
    captured = []
    def complete(gateway, messages, *args, **kwargs):
        captured.append(messages)
        return "早餐我记得"
    monkeypatch.setattr(quality, "_complete_text", complete)
    rewriter = quality.GatewayPersonaRewriter(SimpleNamespace(), ROOT / "missing.json", 2)
    rewriter.rewrite_with_messages("早餐我记得", replace(_context(), world_state_available=available), (),
        ({"role": "user", "content": "还记得早餐吗"},))
    payload = json.loads(captured[0][-1]["content"])
    assert payload["world_state_available"] is available
    relationship = payload["relationship_context"]
    assert relationship["intimacy_request"] == "none"
    if available:
        assert relationship["relationship_stage"] == "unknown"
    else:
        assert relationship["state_status"] == "unavailable"
        assert "relationship_stage" not in relationship
        assert payload["known_continuations"] is None


def test_identity_adjudication_preserves_failed_world_read():
    authority = SimpleNamespace(global_authority="global", layer_authority="layer")
    support = quality._adjudication_support_context("identity_world", authority=authority,
        current_user_input="", character_reply_history="", relationship_context={},
        memory_evidence={"world_facts": "[]", "world_state": "unavailable, not initial"})
    assert support["world_state"] == "unavailable, not initial"
