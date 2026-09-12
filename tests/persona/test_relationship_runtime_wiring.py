"""Exercise the real local bridge; isolated provider and relationship state."""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re

import pytest

from llm_gateway import Gateway, GatewayConfig, GatewayDelta, GatewayResponse
from memory_port import NullMemoryPort
from reply_orchestrator import ReplyOrchestrator, ReplyRequest, ReplyState
from runtime.reply.reply_context import (
    BehaviorLevel, KnownActiveBoundary, NicknamePermission, PrivateBehaviorView,
    ReplyContext, ReplyMode, TrustedTime,
)
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer


ROOT = Path(__file__).resolve().parents[2]
AXES = {"familiarity", "trust", "comfort", "closeness", "tension"}


class RecordingProvider(Gateway):
    def __init__(self, streaming):
        self.stream_enabled = streaming
        self.requests = []

    async def complete(self, messages, *, request_id=None):
        self.requests.append(tuple(dict(message) for message in messages))
        return GatewayResponse("合成回信。", request_id, "synthetic", "synthetic")

    async def stream(self, messages, *, request_id=None):
        self.requests.append(tuple(dict(message) for message in messages))
        yield GatewayDelta("合成", request_id, index=0)
        yield GatewayDelta("回信。", request_id, index=1, finish_reason="stop")


def _configured(streaming=False):
    from local_server import LetterAdapter, _LetterGateway

    adapter = LetterAdapter(
        GatewayConfig(
            provider="openai_compatible", base_url="http://127.0.0.1:9/v1",
            model="synthetic", persona_v2_enabled=True,
            persona_v2_file=str(ROOT / "linli_character/persona_release_v2.json"),
        ),
        memory_port=NullMemoryPort(),
    )
    provider = RecordingProvider(streaming)
    adapter.gateway = provider
    pipeline = ReplyPipeline(
        ReplyOrchestrator(_LetterGateway(adapter)),
        reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
        discover_runtime_ports=False,
    )
    return adapter, provider, pipeline


def _context(mode, behavior):
    return ReplyContext.create(
        mode, private_behavior=behavior,
        trusted_time=TrustedTime(datetime(2026, 9, 10, tzinfo=timezone.utc)),
    )


def _block(messages, tag):
    match = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", messages[0]["content"], re.S)
    assert match is not None
    return json.loads(match.group(1))


@pytest.mark.parametrize("mode", [ReplyMode.TEXT_LETTER, ReplyMode.VOICE_REPLY,
                                  ReplyMode.SPOKEN_VIDEO, ReplyMode.MUSICAL_VIDEO])
@pytest.mark.parametrize("streaming", [False, True])
def test_real_bridge_delivers_current_relationship_without_reassembly(monkeypatch, mode, streaming):
    adapter, provider, pipeline = _configured(streaming)

    def unexpected_reassembly(*args, **kwargs):
        pytest.fail("prepared messages were rebuilt by the legacy bridge")

    monkeypatch.setattr(adapter, "_messages", unexpected_reassembly)
    mixed = PrivateBehaviorView(
        familiarity=BehaviorLevel.HIGH, closeness=BehaviorLevel.HIGH,
        trust=BehaviorLevel.LOW, comfort=BehaviorLevel.LOW,
    )
    stable = PrivateBehaviorView(
        familiarity=BehaviorLevel.HIGH, closeness=BehaviorLevel.HIGH,
        trust=BehaviorLevel.HIGH, comfort=BehaviorLevel.HIGH,
    )
    observed = []
    for i, behavior in enumerate((mixed, stable, PrivateBehaviorView())):
        before = behavior.to_dict()
        result = asyncio.run(pipeline.run(
            ReplyRequest(content="今天聊音乐吧。", request_id=f"synthetic-{i}"),
            _context(mode, behavior),
        ))
        assert result.state is ReplyState.COMPLETED
        assert result.text == "合成回信。"
        assert len(provider.requests) == i + 1
        assert behavior.to_dict() == before
        messages = provider.requests[-1]
        assert messages[-1] == {"role": "user", "content": "今天聊音乐吧。"}
        constraints = _block(messages, "mode_constraints")
        assert constraints.get("delivery_mode", constraints["mode"]) == mode.value
        payload = _block(messages, "private_behavior")
        if i < 2:
            assert payload["expression_context"]
            assert not AXES.intersection(payload)
        else:
            assert "expression_context" not in payload
            assert not AXES.intersection(payload)
        assert "relationship_stage" not in payload
        observed.append(payload)
    assert observed[0]["expression_context"] != observed[1]["expression_context"]
    assert observed[0]["action_permissions"] == observed[1]["action_permissions"]


def test_direct_adapter_generation_also_uses_relationship_projection(monkeypatch):
    adapter, _, _ = _configured()
    behavior = PrivateBehaviorView(familiarity=BehaviorLevel.HIGH, trust=BehaviorLevel.LOW)
    monkeypatch.setattr(adapter, "build_reply_context", lambda mode: _context(mode, behavior))
    payload = _block(adapter._persona_v2_messages("今天聊音乐吧。"), "private_behavior")
    assert payload["expression_context"]
    assert not AXES.intersection(payload)


@pytest.mark.parametrize("streaming", [False, True])
def test_real_bridge_discloses_school_and_scopes_nickname_history(streaming):
    _, provider, pipeline = _configured(streaming)
    letter = "你是从哪所学校毕业的？我能叫你小青吗？你叫我阿岚就好。"
    for index, permission in enumerate((NicknamePermission.NOT_ALLOWED, NicknamePermission.ALLOWED, NicknamePermission.NOT_ALLOWED)):
        behavior = PrivateBehaviorView(
            nickname_permission=permission,
            active_boundaries=(KnownActiveBoundary("boundary.synthetic", "不接受合成坏称呼。"),),
        )
        before = behavior.to_dict()
        result = asyncio.run(pipeline.run(
            ReplyRequest(content=letter, request_id=f"synthetic-addressing-{index}", max_input_chars=30000),
            _context(ReplyMode.TEXT_LETTER, behavior),
        ))
        assert result.state is ReplyState.COMPLETED
        assert len(provider.requests) == index + 1
        messages = provider.requests[-1]
        assert messages[-1] == {"role": "user", "content": letter}
        assert '"declaration_id":"anchor.school_timeline"' in messages[0]["content"]
        payload = _block(messages, "private_behavior")
        assert payload["action_permissions"]["nickname_use"] == {
            "has_authorized_history": permission is NicknamePermission.ALLOWED,
        }
        assert payload["active_boundaries"] == before["active_boundaries"]
        assert behavior.to_dict() == before
