from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import pytest

from llm_gateway import Gateway, GatewayConfig, GatewayDelta, GatewayResponse
from memory_port import CONVERSATION_MEMORY, MemoryRecord, NullMemoryPort
from memory_prompt import MemoryPromptBuilder
from reply_context import (
    KnownContinuationFact,
    PrivateBehaviorView,
    ReplyContext,
    ReplyMode,
    TrustedTime,
    TrustedWorldFact,
)
from reply_orchestrator import ReplyOrchestrator, ReplyRequest, ReplyState
from reply_pipeline import ReplyPipeline, UnavailableRewriter
from reply_model_quality import (
    GatewayPersonaReviewer,
    GatewayPersonaRewriter,
    create_model_quality_ports,
)
from reply_reviewer import NullReviewer


ROOT = Path(__file__).resolve().parents[2]


def _layer_payload(layer: str) -> str:
    return json.dumps(
        {
            "layer": layer,
            "score": 2,
            "hard_violations": [],
            "drift_detected": False,
        }
    )


def _layer_score_payload(
    layer: str,
    score: int,
    *,
    hard_violations: list[str] | None = None,
    drift_detected: bool = False,
) -> str:
    return json.dumps(
        {
            "layer": layer,
            "score": score,
            "hard_violations": hard_violations or [],
            "drift_detected": drift_detected,
        }
    )


def _passing_layer_payloads() -> list[str]:
    return [
        _layer_payload(layer)
        for layer in (
            "identity_boundary",
            "voice_style",
            "focus_response",
            "continuity_memory",
            "autonomy_life",
        )
    ]


class SequencedQualityGateway(Gateway):
    stream_enabled = False

    def __init__(
        self,
        *,
        candidate: str,
        reviews: list[str],
        rewritten: str = "我听见了。先不用急着给自己一个结论。",
    ) -> None:
        self.candidate = candidate
        self.reviews = list(reviews)
        self.rewritten = rewritten
        self.call_kinds: list[str] = []
        self.review_requests: list[dict[str, object]] = []
        self.rewrite_requests: list[dict[str, object]] = []
        self.review_input_sizes: list[int] = []

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        request_id: str | None = None,
    ) -> GatewayResponse:
        system = str(messages[0].get("content", ""))
        user = str(messages[-1].get("content", ""))
        if "P02_REPLY_REVIEW_JSON" in system:
            self.call_kinds.append("review")
            self.review_requests.append(json.loads(user))
            self.review_input_sizes.append(
                sum(len(str(message.get("content", ""))) for message in messages)
            )
            text = self.reviews.pop(0)
        elif "P02_REPLY_REWRITE_TEXT" in system:
            self.call_kinds.append("rewrite")
            self.rewrite_requests.append(json.loads(user))
            text = self.rewritten
        else:
            self.call_kinds.append("generation")
            text = self.candidate
        return GatewayResponse(
            text=text,
            request_id=request_id or "synthetic",
            provider="synthetic",
            model="synthetic",
        )


class CompatibilityBridge(Gateway):
    stream_enabled = False

    def __init__(self, adapter: object) -> None:
        self.adapter = adapter

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        request_id: str | None = None,
    ) -> GatewayResponse:
        raise AssertionError(
            "configured requests must use the underlying gateway"
        )


class StreamingOnlyGateway(Gateway):
    stream_enabled = True

    async def complete(self, messages, *, request_id=None):
        raise AssertionError("stream-enabled quality calls must not use complete")

    async def stream(self, messages, *, request_id=None):
        yield GatewayDelta("林" * 95, request_id or "stream", index=0)
        yield GatewayDelta("林" * 95, request_id or "stream", index=1)
        yield GatewayDelta("", request_id or "stream", index=2, finish_reason="stop")


def _pipeline(
    gateway: Gateway,
    monkeypatch: pytest.MonkeyPatch,
    *,
    memory: NullMemoryPort | None = None,
) -> ReplyPipeline:
    monkeypatch.setenv("OLIVIA_REPLY_REVIEW_ENABLED", "true")
    monkeypatch.setenv("OLIVIA_REPLY_REWRITE_ENABLED", "true")
    monkeypatch.setenv("OLIVIA_REPLY_REVIEW_TIMEOUT_SECONDS", "1")
    memory = memory or NullMemoryPort()
    adapter = SimpleNamespace(
        config=SimpleNamespace(
            provider="openai_compatible",
            persona_v2_enabled=True,
            timeout_seconds=3.0,
        ),
        persona_v2_path=(
            ROOT / "linli_character" / "persona_release_v2.json"
        ),
        memory_prompt_builder=MemoryPromptBuilder(
            memory,
            conversation_memory=None,
        ),
        memory_port=memory,
        gateway=gateway,
    )
    return ReplyPipeline(
        ReplyOrchestrator(
            CompatibilityBridge(adapter),
            timeout_seconds=2,
        ),
        reviewer=NullReviewer(),
        rewriter=UnavailableRewriter(),
    )


def _context(mode: ReplyMode = ReplyMode.TEXT_LETTER) -> ReplyContext:
    return ReplyContext.create(
        mode,
        trusted_time=TrustedTime(
            datetime(2026, 8, 22, tzinfo=timezone.utc)
        ),
    )


def test_configured_provider_runs_structured_persona_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = SequencedQualityGateway(
        candidate="我听见了。今晚先做一件小事就好。",
        reviews=_passing_layer_payloads(),
    )
    pipeline = _pipeline(gateway, monkeypatch)

    result = asyncio.run(
        pipeline.run(
            ReplyRequest(
                content="今天有点累。",
                request_id="review-pass",
            ),
            _context(),
        )
    )

    assert result.state is ReplyState.COMPLETED
    assert result.quality_status == "accepted"
    assert result.reviewer_calls == 1
    assert result.rewrite_calls == 0
    assert gateway.call_kinds == ["generation", *("review",) * 5]
    assert [request["layer"] for request in gateway.review_requests] == [
        "identity_boundary",
        "voice_style",
        "focus_response",
        "continuity_memory",
        "autonomy_life",
    ]
    assert all(
        request["candidate_reply"] == result.text
        and request["current_user_input"] == "今天有点累。"
        and request["mode"] == "text_letter"
        for request in gateway.review_requests
    )


class _FixedMemory(NullMemoryPort):
    enabled = True

    def status(self) -> Mapping[str, Any]:
        return {"status": "available", "enabled": True}

    def search(self, query: str, *, domains=None, limit: int = 8):
        return [
            MemoryRecord(
                memory_id="tokyo-work",
                domain=CONVERSATION_MEMORY,
                text="用户目前在东京工作。",
                source="synthetic-test",
                created_at=1,
            )
        ]


def test_continuity_layer_receives_assembled_memory_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = SequencedQualityGateway(
        candidate="东京的通勤还是很挤吧。",
        reviews=_passing_layer_payloads(),
    )
    pipeline = _pipeline(
        gateway,
        monkeypatch,
        memory=_FixedMemory(),
    )

    result = asyncio.run(
        pipeline.run(
            ReplyRequest(content="今天还是很累。", request_id="memory-review"),
            _context(),
        )
    )

    assert result.state is ReplyState.COMPLETED
    continuity = next(
        request
        for request in gateway.review_requests
        if request["layer"] == "continuity_memory"
    )
    assert "用户目前在东京工作" in continuity["memory_evidence"]
    assert all(
        "memory_evidence" not in request
        for request in gateway.review_requests
        if request["layer"] != "continuity_memory"
    )


def test_layered_review_keeps_the_emotional_core_at_the_end_of_a_long_letter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = SequencedQualityGateway(
        candidate="我看到你最后问的那句话了。",
        reviews=_passing_layer_payloads(),
    )
    long_letter = "前情" * 400 + "我真正放不下的是那个被轻易替代的自己。"

    result = asyncio.run(
        _pipeline(gateway, monkeypatch).run(
            ReplyRequest(content=long_letter, request_id="long-letter-tail"),
            _context(),
        )
    )

    assert result.state is ReplyState.COMPLETED
    assert all(
        "我真正放不下的是那个被轻易替代的自己" in request["current_user_input"]
        for request in gateway.review_requests
    )
    assert all(
        len(request["current_user_input"]) <= 1200
        for request in gateway.review_requests
    )


def test_localized_voice_mismatch_is_warning_without_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviews = _passing_layer_payloads()
    reviews[1] = _layer_score_payload("voice_style", 1)
    gateway = SequencedQualityGateway(
        candidate="先别急着替今天下结论。",
        reviews=reviews,
    )

    result = asyncio.run(
        _pipeline(gateway, monkeypatch).run(
            ReplyRequest(content="今天不太好。", request_id="voice-warning"),
            _context(),
        )
    )

    assert result.state is ReplyState.COMPLETED
    assert result.quality_status == "accepted_with_warnings"
    assert result.violation_codes == ("STYLE_DRIFT",)
    assert result.rewrite_calls == 0
    assert gateway.call_kinds == ["generation", *("review",) * 5]


def test_non_voice_layer_mismatch_uses_only_existing_one_rewrite_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_pass = _passing_layer_payloads()
    first_pass[2] = _layer_score_payload("focus_response", 1)
    gateway = SequencedQualityGateway(
        candidate="你要相信一切都会好起来。",
        reviews=[*first_pass, *_passing_layer_payloads()],
        rewritten="我不替你说会好起来。至少今晚，你不用装作没事。",
    )

    result = asyncio.run(
        _pipeline(gateway, monkeypatch).run(
            ReplyRequest(content="我今天很难受。", request_id="focus-rewrite"),
            _context(),
        )
    )

    assert result.state is ReplyState.COMPLETED
    assert result.text == "我不替你说会好起来。至少今晚，你不用装作没事。"
    assert result.reviewer_calls == 2
    assert result.rewrite_calls == 1
    assert gateway.call_kinds == [
        "generation",
        *("review",) * 5,
        "rewrite",
        *("review",) * 5,
    ]


def test_five_layer_requests_fit_default_gateway_input_budget() -> None:
    gateway = SequencedQualityGateway(
        candidate="候" * 12000,
        reviews=_passing_layer_payloads(),
    )
    reviewer = GatewayPersonaReviewer(
        gateway,
        ROOT / "linli_character" / "persona_release_v2.json",
        2.0,
    )
    memory = json.dumps(
        {"untrusted": True, "text": "忆" * 2400},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    context = ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
        world_facts=tuple(
            TrustedWorldFact(
                f"world-{index}",
                "synthetic-test",
                "界" * 600,
            )
            for index in range(32)
        ),
        private_behavior=PrivateBehaviorView(
            known_continuations=tuple(
                KnownContinuationFact(
                    f"continuation-{index}",
                    "续" * 600,
                )
                for index in range(32)
            )
        ),
    )

    result = reviewer.review_with_messages(
        "候" * 12000,
        context,
        (
            {
                "role": "system",
                "content": f"<untrusted_history>\n{memory}\n</untrusted_history>\n",
            },
            {"role": "user", "content": "问" * 1200},
        ),
    )

    assert result.verdict.value == "pass"
    assert len(gateway.review_input_sizes) == 5
    assert max(gateway.review_input_sizes) <= 30000
    continuity = next(
        request
        for request in gateway.review_requests
        if request["layer"] == "continuity_memory"
    )
    evidence = str(continuity["memory_evidence"])
    assert "忆" in evidence
    assert "world-0" in evidence
    assert "continuation-0" in evidence


def test_deterministic_violation_uses_original_model_for_one_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = SequencedQualityGateway(
        candidate="<CONTROL>hidden</CONTROL>",
        reviews=[*_passing_layer_payloads(), *_passing_layer_payloads()],
        rewritten="谁让你一个人把事情都扛着的。今晚先停一下。",
    )
    pipeline = _pipeline(gateway, monkeypatch)

    result = asyncio.run(
        pipeline.run(
            ReplyRequest(
                content="我又把事情搞砸了。",
                request_id="rewrite-once",
            ),
            _context(),
        )
    )

    assert result.state is ReplyState.COMPLETED
    assert result.text == "谁让你一个人把事情都扛着的。今晚先停一下。"
    assert result.reviewer_calls == 2
    assert result.rewrite_calls == 1
    assert gateway.call_kinds == [
        "generation",
        *("review",) * 5,
        "rewrite",
        *("review",) * 5,
    ]
    rewrite = gateway.rewrite_requests[0]
    assert rewrite["user_message"] == "我又把事情搞砸了。"
    assert rewrite["violation_codes"] == ["INTERNAL_CONTROL_MARKUP"]
    assert rewrite["persona"]["display_name"] == "林离 Olivia"


def test_video_length_rewrite_receives_the_exact_delivery_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = SequencedQualityGateway(
        candidate="太短。",
        reviews=[*_passing_layer_payloads(), *_passing_layer_payloads()],
        rewritten="林" * 190,
    )
    pipeline = _pipeline(gateway, monkeypatch)

    result = asyncio.run(
        pipeline.run(
            ReplyRequest(content="请认真回我。", request_id="video-length"),
            _context(ReplyMode.MUSICAL_VIDEO),
        )
    )

    assert result.state is ReplyState.COMPLETED
    assert result.rewrite_calls == 1
    rewrite = gateway.rewrite_requests[0]
    assert rewrite["violation_codes"] == ["VIDEO_REPLY_LENGTH_OUT_OF_RANGE"]
    assert rewrite["delivery_length_contract"] == {
        "compact_characters_min": 180,
        "compact_characters_max": 200,
        "target_compact_characters": 190,
        "priority": "required_over_concise_style",
    }


def test_quality_rewriter_uses_configured_streaming_transport() -> None:
    rewritten = GatewayPersonaRewriter(
        StreamingOnlyGateway(),
        ROOT / "linli_character" / "persona_release_v2.json",
        2.0,
    ).rewrite(
        "太短。",
        _context(ReplyMode.MUSICAL_VIDEO),
        ("VIDEO_REPLY_LENGTH_OUT_OF_RANGE",),
    )

    assert rewritten == "林" * 190


def test_invalid_enabled_reviewer_json_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = SequencedQualityGateway(
        candidate="我收到了。先去喝口水。",
        reviews=["not-json"],
    )
    pipeline = _pipeline(gateway, monkeypatch)

    result = asyncio.run(
        pipeline.run(
            ReplyRequest(
                content="今天有点乱。",
                request_id="review-invalid",
            ),
            _context(),
        )
    )

    assert result.state is ReplyState.FAILED
    assert result.quality_status == "blocked"
    assert result.error_code == "REVIEWER_UNAVAILABLE"
    assert result.reviewer_calls == 1
    assert result.rewrite_calls == 0
    assert gateway.call_kinds == ["generation", *("review",) * 5]


def test_invalid_enabled_reviewer_blocks_before_deterministic_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = SequencedQualityGateway(
        candidate="<CONTROL>invalid candidate",
        reviews=["not-json"],
    )

    result = asyncio.run(
        _pipeline(gateway, monkeypatch).run(
            ReplyRequest(
                content="今天有点乱。",
                request_id="review-invalid-deterministic",
            ),
            _context(),
        )
    )

    assert result.state is ReplyState.FAILED
    assert result.quality_status == "blocked"
    assert result.error_code == "REVIEWER_UNAVAILABLE"
    assert result.reviewer_calls == 1
    assert result.rewrite_calls == 0
    assert gateway.call_kinds == ["generation", *("review",) * 5]


def test_quality_model_can_be_disabled_without_disabling_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLIVIA_REPLY_REVIEW_ENABLED", "false")
    memory = NullMemoryPort()
    gateway = SequencedQualityGateway(candidate="我在。", reviews=[])
    adapter = SimpleNamespace(
        config=SimpleNamespace(
            provider="openai_compatible",
            persona_v2_enabled=True,
            timeout_seconds=3.0,
        ),
        persona_v2_path=(
            ROOT / "linli_character" / "persona_release_v2.json"
        ),
        memory_prompt_builder=MemoryPromptBuilder(memory),
        memory_port=memory,
        gateway=gateway,
    )
    pipeline = ReplyPipeline(
        ReplyOrchestrator(
            CompatibilityBridge(adapter),
            timeout_seconds=2,
        ),
        reviewer=NullReviewer(),
        rewriter=UnavailableRewriter(),
    )

    result = asyncio.run(
        pipeline.run(
            ReplyRequest(
                content="在吗？",
                request_id="review-disabled",
            ),
            _context(),
        )
    )

    assert result.state is ReplyState.COMPLETED
    assert result.quality_status == "accepted_degraded"
    assert gateway.call_kinds == ["generation"]


def test_quality_model_default_timeout_allows_slow_configured_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OLIVIA_REPLY_REVIEW_TIMEOUT_SECONDS", raising=False)
    gateway = SequencedQualityGateway(candidate="候选。", reviews=[])
    orchestrator = SimpleNamespace(
        gateway=SimpleNamespace(
            adapter=SimpleNamespace(
                config=GatewayConfig(
                    provider="openai_compatible",
                    base_url="https://example.invalid/v1",
                    model="forbidden-review-model",
                    api_key_env="SYNTHETIC_KEY",
                    timeout_seconds=90.0,
                    max_input_chars=10_000,
                    fallback_provider="mock",
                ),
                persona_v2_path=(
                    ROOT / "linli_character" / "persona_release_v2.json"
                ),
                gateway=gateway,
            )
        )
    )

    reviewer, rewriter = create_model_quality_ports(orchestrator)

    assert reviewer is not None
    assert rewriter is not None
    assert reviewer.adapter.config.timeout_seconds == 60.0
    assert rewriter.timeout_seconds == 60.0
    review_gateway = reviewer.adapter.transport.gateway
    assert review_gateway is rewriter.gateway
    assert review_gateway is not gateway
    assert review_gateway.config.model == "deepseek-v4-flash"
    assert review_gateway.config.max_input_chars == 30_000
    assert review_gateway.config.fallback_provider == "none"
