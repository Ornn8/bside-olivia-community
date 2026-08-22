from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import pytest

from llm_gateway import Gateway, GatewayResponse
from memory_port import NullMemoryPort
from memory_prompt import MemoryPromptBuilder
from reply_context import ReplyContext, ReplyMode, TrustedTime
from reply_orchestrator import ReplyOrchestrator, ReplyRequest, ReplyState
from reply_pipeline import ReplyPipeline, UnavailableRewriter
from reply_reviewer import NullReviewer


ROOT = Path(__file__).resolve().parents[2]


def _review_payload(verdict: str = "pass") -> str:
    return json.dumps(
        {
            "schema_version": "p02.reply-review.v1",
            "status": "completed",
            "verdict": verdict,
            "violations": [],
            "scores": {
                "persona_consistency": 95,
                "factual_consistency": 96,
                "relationship_boundary": 97,
                "mode_compliance": 98,
            },
        }
    )


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


def _pipeline(
    gateway: Gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> ReplyPipeline:
    monkeypatch.setenv("OLIVIA_REPLY_REVIEW_ENABLED", "true")
    monkeypatch.setenv("OLIVIA_REPLY_REWRITE_ENABLED", "true")
    monkeypatch.setenv("OLIVIA_REPLY_REVIEW_TIMEOUT_SECONDS", "1")
    memory = NullMemoryPort()
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
    return ReplyPipeline(
        ReplyOrchestrator(
            CompatibilityBridge(adapter),
            timeout_seconds=2,
        ),
        reviewer=NullReviewer(),
        rewriter=UnavailableRewriter(),
    )


def _context() -> ReplyContext:
    return ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(
            datetime(2026, 8, 22, tzinfo=timezone.utc)
        ),
    )


def test_configured_provider_runs_structured_persona_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = SequencedQualityGateway(
        candidate="我听见了。今晚先做一件小事就好。",
        reviews=[_review_payload()],
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
    assert gateway.call_kinds == ["generation", "review"]
    request = gateway.review_requests[0]
    assert request["candidate"] == result.text
    assert request["mode"] == "text_letter"
    assert request["persona"]["display_name"] == "林离 Olivia"
    assert (
        request["references"][0]["reference_id"]
        == "current.user_excerpt"
    )
    assert "private_behavior" not in request


def test_deterministic_violation_uses_original_model_for_one_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = SequencedQualityGateway(
        candidate="<CONTROL>hidden</CONTROL>",
        reviews=[_review_payload(), _review_payload()],
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
        "review",
        "rewrite",
        "review",
    ]
    rewrite = gateway.rewrite_requests[0]
    assert rewrite["user_message"] == "我又把事情搞砸了。"
    assert rewrite["violation_codes"] == ["INTERNAL_CONTROL_MARKUP"]
    assert rewrite["persona"]["display_name"] == "林离 Olivia"


def test_invalid_reviewer_json_degrades_only_clean_candidate(
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

    assert result.state is ReplyState.COMPLETED
    assert result.quality_status == "accepted_degraded"
    assert result.error_code is None
    assert result.reviewer_calls == 1
    assert result.rewrite_calls == 0
    assert gateway.call_kinds == ["generation", "review"]


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
