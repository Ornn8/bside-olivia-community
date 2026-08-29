import asyncio
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import pytest

from conversation_memory_port import ConversationMemoryRecord, ConversationMemoryStatus
from llm_gateway import Gateway, GatewayConfig, GatewayResponse
from memory_port import NullMemoryPort
from memory_prompt import MemoryPromptBuilder
from runtime.reply.reply_context import (
    IntimacyRequest,
    ReplyContext,
    ReplyMode,
    TrustedTime,
)
from reply_orchestrator import (
    ReplyOrchestrator,
    ReplyRequest,
    ReplyResult,
    ReplyState,
)
from runtime.reply.reply_pipeline import (
    PipelineResult,
    ReplyPipeline,
    UnavailableRewriter,
)
from reply_model_quality import GatewayPersonaReviewer
from runtime.reply.reply_reviewer import (
    NullReviewer,
    ReviewResult,
    ReviewerScores,
    ReviewStatus,
    ReviewVerdict,
)


ROOT = Path(__file__).resolve().parents[2]


def test_legacy_reply_modules_alias_the_canonical_runtime_modules() -> None:
    assert importlib.import_module("reply_pipeline") is importlib.import_module(
        "runtime.reply.reply_pipeline"
    )
    assert importlib.import_module("reply_reviewer") is importlib.import_module(
        "runtime.reply.reply_reviewer"
    )


def _context(mode: ReplyMode = ReplyMode.TEXT_LETTER) -> ReplyContext:
    return ReplyContext.create(
        mode,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
    )


class CompletedOrchestrator:
    def __init__(self, text: str) -> None:
        self.text = text

    async def run(self, request: object) -> ReplyResult:
        return ReplyResult("request-1", ReplyState.COMPLETED, text=self.text)


class PassingReviewer:
    def review(self, candidate: str, context: ReplyContext) -> ReviewResult:
        return ReviewResult(
            ReviewStatus.COMPLETED,
            ReviewVerdict.PASS,
            (),
            ReviewerScores(100, 100, 100, 100),
            IntimacyRequest.NONE,
            (),
        )


class FixedRewriter:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def rewrite(
        self,
        candidate: str,
        context: ReplyContext,
        violation_codes: tuple[str, ...],
    ) -> str:
        self.calls += 1
        return self.text


def test_pipeline_accepts_clean_candidate_with_disabled_reviewer() -> None:
    pipeline = ReplyPipeline(
        CompletedOrchestrator("clean canonical reply"),
        reviewer=NullReviewer(),
        rewriter=UnavailableRewriter(),
    )

    result = asyncio.run(pipeline.run(object(), _context()))

    assert result.state is ReplyState.COMPLETED
    assert result.text == "clean canonical reply"
    assert result.quality_status == "accepted_degraded"
    assert result.rewrite_calls == 0


def test_pipeline_exposes_only_rewritten_canonical_text() -> None:
    rewriter = FixedRewriter("safe canonical reply")
    pipeline = ReplyPipeline(
        CompletedOrchestrator("candidate <CONTROL>private</CONTROL>"),
        reviewer=PassingReviewer(),
        rewriter=rewriter,
    )

    result = asyncio.run(pipeline.run(object(), _context()))

    assert result.state is ReplyState.COMPLETED
    assert result.text == "safe canonical reply"
    assert "candidate" not in repr(result)
    assert rewriter.calls == 1


def test_pipeline_blocks_candidate_when_single_rewrite_fails() -> None:
    pipeline = ReplyPipeline(
        CompletedOrchestrator("<CONTROL>private</CONTROL>"),
        reviewer=PassingReviewer(),
        rewriter=UnavailableRewriter(),
    )

    result = asyncio.run(pipeline.run(object(), _context()))

    assert result.state is ReplyState.FAILED
    assert result.text == ""
    assert result.error_code == "REWRITE_FAILED"
    assert result.quality_status == "blocked"
    assert result.violation_codes == ("INTERNAL_CONTROL_MARKUP",)


def test_pipeline_preserves_only_length_blocked_video_copy_for_duration_repair() -> None:
    pipeline = ReplyPipeline(
        CompletedOrchestrator("太短。"),
        reviewer=PassingReviewer(),
        rewriter=UnavailableRewriter(),
    )

    result = asyncio.run(pipeline.run(object(), _context(ReplyMode.SPOKEN_VIDEO)))

    assert result.state is ReplyState.FAILED
    assert result.text == "太短。"
    assert result.error_code == "REWRITE_FAILED"
    assert result.violation_codes == ("VIDEO_REPLY_LENGTH_OUT_OF_RANGE",)


class RecordingProvider:
    stream_enabled = False

    def __init__(self) -> None:
        self.messages: tuple[dict[str, str], ...] = ()
        self.calls = 0

    async def complete(
        self,
        messages: object,
        *,
        request_id: str | None = None,
    ) -> GatewayResponse:
        self.calls += 1
        self.messages = tuple(dict(message) for message in messages)  # type: ignore[arg-type]
        return GatewayResponse(
            text="我听见了。" + "林" * 185,
            request_id=request_id or "generated",
            provider="synthetic",
            model="synthetic",
        )


class CompatibilityBridge:
    stream_enabled = False

    def __init__(self, adapter: object) -> None:
        self.adapter = adapter
        self.calls = 0

    async def complete(
        self,
        messages: object,
        *,
        request_id: str | None = None,
    ) -> GatewayResponse:
        self.calls += 1
        raise AssertionError(
            "prepared Persona messages must not be rebuilt by the bridge"
        )


class CountingReplyOrchestrator(ReplyOrchestrator):
    def __init__(self, gateway: Gateway) -> None:
        super().__init__(gateway, timeout_seconds=1)
        self.calls = 0

    async def run(self, request: ReplyRequest) -> ReplyResult:
        self.calls += 1
        return await super().run(request)


def _configured_v2_pipeline(persona_path: Path):
    import local_server

    provider = RecordingProvider()
    adapter = local_server.LetterAdapter(
        GatewayConfig(
            provider="openai_compatible",
            base_url="http://127.0.0.1:9/v1",
            model="synthetic",
            persona_v2_enabled=True,
            persona_v2_file=str(persona_path),
        ),
        memory_port=NullMemoryPort(),
    )
    adapter.gateway = provider
    bridge = CompatibilityBridge(adapter)
    orchestrator = CountingReplyOrchestrator(bridge)  # type: ignore[arg-type]
    pipeline = ReplyPipeline(
        orchestrator,
        reviewer=NullReviewer(),
        rewriter=UnavailableRewriter(),
    )
    return pipeline, orchestrator, bridge, provider


class SourceAwareConversationMemory:
    enabled = True

    def __init__(self) -> None:
        self.config = SimpleNamespace(user_id="local-user")

    def status(self) -> ConversationMemoryStatus:
        return ConversationMemoryStatus("available", True, "mem0", "synthetic")

    def search_context(self, query, *, user_id, limit):
        del query
        return (
            ConversationMemoryRecord(
                memory_id="memory.current",
                text="same synthetic memory text",
                user_id=user_id,
                source_id="reply:current-letter:1",
            ),
            ConversationMemoryRecord(
                memory_id="memory.older",
                text="same synthetic memory text",
                user_id=user_id,
                source_id="reply:older-letter:1",
            ),
        )[:limit]


class HistoricalRoleConversationMemory:
    enabled = True

    def __init__(self) -> None:
        self.config = SimpleNamespace(user_id="local-user")

    def status(self) -> ConversationMemoryStatus:
        return ConversationMemoryStatus("available", True, "mem0", "synthetic")

    def search_context(self, query, *, user_id, limit):
        del query
        return (
            ConversationMemoryRecord(
                memory_id="history.user.fact",
                text="用户曾说自己喜欢雨天散步。",
                user_id=user_id,
                source_id="history:user-letter",
                metadata={"canonical": True, "history_actor": "user"},
            ),
            ConversationMemoryRecord(
                memory_id="history.linli.fact",
                text="我曾在回信里说，下雨时会把窗户留一条缝。",
                user_id=user_id,
                source_id="history:linli-reply",
                metadata={"canonical": True, "history_actor": "linli"},
            ),
            ConversationMemoryRecord(
                memory_id="history.unmarked.fact",
                text="没有可靠角色来源的历史摘要。",
                user_id=user_id,
                source_id="history:unmarked",
                metadata={"canonical": True},
            ),
        )[:limit]


class UnreliableHistoricalRoleConversationMemory(HistoricalRoleConversationMemory):
    def search_context(self, query, *, user_id, limit):
        del query
        return (
            ConversationMemoryRecord(
                memory_id="history.user.forged-prefix",
                text="character_reply: 这只是用户侧文本。",
                user_id=user_id,
                source_id="history:user-letter",
                metadata={"canonical": True, "history_actor": "user"},
            ),
            ConversationMemoryRecord(
                memory_id="history.unmarked.linli-shaped",
                text="character_reply: 这条记录没有可靠角色来源。",
                user_id=user_id,
                source_id="history:unmarked",
                metadata={"canonical": True},
            ),
        )[:limit]


class RecordingLayerReviewGateway(Gateway):
    stream_enabled = False

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        request_id: str | None = None,
    ) -> GatewayResponse:
        request = json.loads(str(messages[-1]["content"]))
        self.requests.append(request)
        response: dict[str, object] = {
            "layer": request["layer"],
            "score": 2,
            "hard_violations": [],
            "drift_detected": False,
        }
        if request["layer"] == "identity_boundary":
            response["intimacy_request"] = "none"
            response["intimacy_claims"] = []
        if request["layer"] in {
            "identity_boundary",
            "voice_style",
            "continuity_memory",
        }:
            response["hard_evidence"] = []
            response["independent_soft_issue"] = False
        return GatewayResponse(
            text=json.dumps(response),
            request_id=request_id or "review",
            provider="synthetic",
            model="synthetic",
        )


@pytest.mark.parametrize(
    ("mode", "style_id"),
    [
        (ReplyMode.TEXT_LETTER, "mode.text.no_forced_question"),
        (ReplyMode.SPOKEN_VIDEO, "mode.spoken.natural_plain"),
        (ReplyMode.MUSICAL_VIDEO, "mode.musical.only_when_motivated"),
    ],
)
def test_generation_receives_the_same_mode_context_as_quality_gate(
    mode: ReplyMode,
    style_id: str,
) -> None:
    memory = NullMemoryPort()
    provider = RecordingProvider()
    adapter = SimpleNamespace(
        config=SimpleNamespace(
            persona_v2_enabled=True,
            provider="synthetic",
        ),
        persona_v2_path=ROOT / "linli_character" / "persona_release_v2.json",
        memory_prompt_builder=MemoryPromptBuilder(memory),
        memory_port=memory,
        gateway=provider,
    )
    bridge = CompatibilityBridge(adapter)
    pipeline = ReplyPipeline(
        ReplyOrchestrator(bridge, timeout_seconds=1),  # type: ignore[arg-type]
        reviewer=NullReviewer(),
        rewriter=UnavailableRewriter(),
    )

    result = asyncio.run(
        pipeline.run(
            ReplyRequest(
                content="今天只是普通地有点累。",
                request_id=f"mode-{mode.value}",
                max_input_chars=10_000,
            ),
            _context(mode),
        )
    )

    assert result.state is ReplyState.COMPLETED
    assert bridge.calls == 0
    assert provider.calls == 1
    assert tuple(message["role"] for message in provider.messages) == (
        "system",
        "user",
    )
    assert provider.messages[1]["content"] == "今天只是普通地有点累。"
    system = provider.messages[0]["content"]
    assert f'"mode":"{mode.value}"' in system
    assert style_id in system
    assert "林离 Olivia" in system


def test_configured_persona_v2_preparation_excludes_current_memory_source() -> None:
    """Release-default Persona v2 must use the production source selector."""

    import local_server

    memory = SourceAwareConversationMemory()
    provider = RecordingProvider()
    adapter = local_server.LetterAdapter(
        GatewayConfig(
            provider="openai_compatible",
            base_url="http://127.0.0.1:9/v1",
            model="synthetic",
            persona_v2_enabled=True,
        ),
        memory_port=NullMemoryPort(),
        conversation_memory=memory,
    )
    adapter.gateway = provider
    pipeline = ReplyPipeline(
        ReplyOrchestrator(CompatibilityBridge(adapter), timeout_seconds=1),  # type: ignore[arg-type]
        reviewer=NullReviewer(),
        rewriter=UnavailableRewriter(),
    )
    token = local_server._CURRENT_LETTER_MEMORY_SOURCE.set(
        "reply:current-letter:1"
    )
    try:
        result = asyncio.run(
            pipeline.run(
                ReplyRequest(
                    content="synthetic current letter",
                    request_id="current-letter-request",
                ),
                _context(),
            )
        )
    finally:
        local_server._CURRENT_LETTER_MEMORY_SOURCE.reset(token)

    assert result.state is ReplyState.COMPLETED
    assert provider.calls == 1
    rendered = "\n".join(message["content"] for message in provider.messages)
    assert "reply:current-letter:1" not in rendered
    assert "reply:older-letter:1" in rendered


@pytest.mark.parametrize("persona_body", [None, "{broken"], ids=["missing", "malformed"])
def test_configured_provider_blocks_draft_persona_before_orchestrator(
    tmp_path: Path,
    persona_body: str | None,
) -> None:
    persona_path = tmp_path / "private-user" / "persona.json"
    if persona_body is not None:
        persona_path.parent.mkdir()
        persona_path.write_text(persona_body, encoding="utf-8")
    pipeline, orchestrator, bridge, provider = _configured_v2_pipeline(persona_path)

    result = asyncio.run(
        pipeline.run(
            ReplyRequest(content="synthetic letter", request_id="draft-persona"),
            _context(),
        )
    )

    assert result == PipelineResult(
        "draft-persona",
        ReplyState.FAILED,
        error_code="PERSONA_NOT_READY",
        retryable=False,
    )
    assert orchestrator.calls == 0
    assert bridge.calls == 0
    assert provider.calls == 0
    assert str(persona_path) not in repr(result)
    assert "broken" not in repr(result)


def test_configured_provider_blocks_policy_only_persona_before_orchestrator(
    tmp_path: Path,
) -> None:
    persona_path = tmp_path / "policy-only.json"
    persona_path.write_text(
        json.dumps(
            {
                "schema_version": "p02.persona.v2",
                "persona_id": "synthetic.policy",
                "declarations": [
                    {
                        "declaration_id": "constitution.synthetic",
                        "source_id": "source.synthetic",
                        "tier": "CONSTITUTION",
                        "facet": "POLICY",
                        "confidence": "HIGH",
                        "rights_status": "SUMMARY_ONLY",
                        "allowed_public_release": True,
                        "statement": "Synthetic public policy rule.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    pipeline, orchestrator, bridge, provider = _configured_v2_pipeline(persona_path)

    result = asyncio.run(
        pipeline.run(
            ReplyRequest(content="synthetic letter", request_id="policy-persona"),
            _context(),
        )
    )

    assert result == PipelineResult(
        "policy-persona",
        ReplyState.FAILED,
        error_code="PERSONA_NOT_READY",
        retryable=False,
    )
    assert orchestrator.calls == 0
    assert bridge.calls == 0
    assert provider.calls == 0


def test_explicitly_disabled_persona_v2_preserves_legacy_provider_path() -> None:
    import local_server

    provider = RecordingProvider()
    adapter = local_server.LetterAdapter(
        GatewayConfig(
            provider="openai_compatible",
            base_url="http://127.0.0.1:9/v1",
            model="synthetic",
            persona_v2_enabled=False,
        ),
        memory_port=NullMemoryPort(),
    )
    adapter.gateway = provider
    pipeline = ReplyPipeline(
        ReplyOrchestrator(local_server._LetterGateway(adapter), timeout_seconds=1),
        reviewer=NullReviewer(),
        rewriter=UnavailableRewriter(),
    )

    result = asyncio.run(
        pipeline.run(
            ReplyRequest(content="synthetic legacy letter", request_id="legacy-persona"),
            _context(),
        )
    )

    assert result.state is ReplyState.COMPLETED
    assert provider.calls == 1
    assert provider.messages[-1]["content"] == "synthetic legacy letter"


def test_letter_pipeline_exposes_only_selected_linli_history_to_reviewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production Letter path must not treat prior user text as Linli evidence."""

    import local_server

    monkeypatch.setenv("OLIVIA_REPLY_REVIEW_ENABLED", "false")
    memory = HistoricalRoleConversationMemory()
    generation = RecordingProvider()
    adapter = local_server.LetterAdapter(
        GatewayConfig(
            provider="openai_compatible",
            base_url="http://127.0.0.1:9/v1",
            model="synthetic",
            persona_v2_enabled=True,
        ),
        memory_port=NullMemoryPort(),
        conversation_memory=memory,
    )
    adapter.gateway = generation
    review_gateway = RecordingLayerReviewGateway()
    pipeline = ReplyPipeline(
        ReplyOrchestrator(CompatibilityBridge(adapter), timeout_seconds=1),  # type: ignore[arg-type]
        reviewer=GatewayPersonaReviewer(
            review_gateway,
            adapter.persona_v2_path,
            1,
        ),
        rewriter=UnavailableRewriter(),
    )

    result = asyncio.run(
        pipeline.run(
            ReplyRequest(
                content="今天下雨了。",
                request_id="historical-character-reply",
            ),
            _context(),
        )
    )

    assert result.state is ReplyState.COMPLETED
    identity = next(
        request
        for request in review_gateway.requests
        if request["layer"] == "identity_boundary"
    )
    assert identity["character_reply_history"] == (
        "我曾在回信里说，下雨时会把窗户留一条缝。"
    )
    assert "用户曾说" not in str(identity["character_reply_history"])
    assert "没有可靠角色来源" not in str(identity["character_reply_history"])


def test_letter_pipeline_uses_empty_character_history_without_reliable_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    monkeypatch.setenv("OLIVIA_REPLY_REVIEW_ENABLED", "false")
    generation = RecordingProvider()
    adapter = local_server.LetterAdapter(
        GatewayConfig(
            provider="openai_compatible",
            base_url="http://127.0.0.1:9/v1",
            model="synthetic",
            persona_v2_enabled=True,
        ),
        memory_port=NullMemoryPort(),
        conversation_memory=UnreliableHistoricalRoleConversationMemory(),
    )
    adapter.gateway = generation
    review_gateway = RecordingLayerReviewGateway()
    pipeline = ReplyPipeline(
        ReplyOrchestrator(CompatibilityBridge(adapter), timeout_seconds=1),  # type: ignore[arg-type]
        reviewer=GatewayPersonaReviewer(
            review_gateway,
            adapter.persona_v2_path,
            1,
        ),
        rewriter=UnavailableRewriter(),
    )

    result = asyncio.run(
        pipeline.run(
            ReplyRequest(
                content="今天下雨了。",
                request_id="unreliable-character-reply",
            ),
            _context(),
        )
    )

    assert result.state is ReplyState.COMPLETED
    identity = next(
        request
        for request in review_gateway.requests
        if request["layer"] == "identity_boundary"
    )
    assert identity["character_reply_history"] == ""


class FakeTriage:
    reply_mode = "video"

    def to_dict(self) -> dict[str, str]:
        return {"reply_mode": self.reply_mode}


class FakeTriageService:
    async def classify(self, content: str) -> FakeTriage:
        return FakeTriage()


def test_reply_length_error_is_terminal_and_not_provider_unavailable() -> None:
    import http_contract
    import local_server

    assert local_server._public_llm_error("LLM_REPLY_LENGTH_INVALID") == (
        "LLM_REPLY_LENGTH_INVALID",
        False,
    )
    assert http_contract.ERROR_CODES["LLM_REPLY_LENGTH_INVALID"] == {
        "http_status": 503,
        "retryable": False,
    }


def test_persona_not_ready_error_is_terminal_and_public() -> None:
    import http_contract
    import local_server

    assert local_server._public_llm_error("PERSONA_NOT_READY") == (
        "PERSONA_NOT_READY",
        False,
    )
    assert http_contract.error_metadata("PERSONA_NOT_READY") == {
        "http_status": 503,
        "retryable": False,
    }


class FakePipeline:
    def __init__(self, result: PipelineResult) -> None:
        self.result = result

    async def run(self, request: object, context: ReplyContext) -> PipelineResult:
        assert context.mode is ReplyMode.MUSICAL_VIDEO
        return self.result


def test_generate_reply_persists_and_renders_only_canonical_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    letter = {
        "letter_id": "letter-1",
        "reply_text": "",
        "letter_status": "PENDING",
    }
    canonical_text = "林" * 190
    scheduled: list[tuple[object, ...]] = []
    remembered: list[tuple[str, str]] = []
    monkeypatch.setattr(local_server.store, "letters", [letter])
    monkeypatch.setattr(local_server, "emotion_triage", FakeTriageService())
    monkeypatch.setattr(local_server, "_persist_store_state", lambda: None)
    monkeypatch.setattr(
        local_server, "_schedule_text_reply_delay", lambda *args: None
    )
    monkeypatch.setattr(
        local_server, "_schedule_media_job", lambda *args: scheduled.append(args)
    )
    monkeypatch.setattr(
        local_server.letters_adapter,
        "remember_conversation",
        lambda content, reply: remembered.append((content, reply)),
    )
    monkeypatch.setattr(
        local_server,
        "reply_pipeline",
        FakePipeline(
            PipelineResult(
                "letter-1",
                ReplyState.COMPLETED,
                text=canonical_text,
                quality_status="accepted",
                violation_codes=("SYNTHETIC_FIXED",),
                reviewer_calls=2,
                rewrite_calls=1,
            )
        ),
    )

    assert (
        asyncio.run(
            local_server.generate_reply("letter-1", "candidate input")
        )
        is True
    )
    assert letter["reply_text"] == canonical_text
    assert letter["letter_status"] == "COMPLETED"
    assert letter["quality_status"] == "accepted"
    assert letter["quality_violation_codes"] == ["SYNTHETIC_FIXED"]
    assert scheduled[0][2] == canonical_text
    assert remembered == [("candidate input", canonical_text)]


def test_blocked_candidate_never_reaches_storage_or_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    letter = {
        "letter_id": "letter-2",
        "reply_text": "",
        "letter_status": "PENDING",
    }
    scheduled: list[tuple[object, ...]] = []
    monkeypatch.setattr(local_server.store, "letters", [letter])
    monkeypatch.setattr(local_server, "emotion_triage", FakeTriageService())
    monkeypatch.setattr(local_server, "_persist_store_state", lambda: None)
    monkeypatch.setattr(
        local_server, "_schedule_text_reply_delay", lambda *args: None
    )
    monkeypatch.setattr(
        local_server, "_schedule_media_job", lambda *args: scheduled.append(args)
    )
    monkeypatch.setattr(
        local_server,
        "reply_pipeline",
        FakePipeline(
            PipelineResult(
                "letter-2",
                ReplyState.FAILED,
                error_code="REPLY_QUALITY_BLOCKED",
                quality_status="blocked",
                violation_codes=("INTERNAL_CONTROL_MARKUP",),
                reviewer_calls=1,
                rewrite_calls=1,
            )
        ),
    )

    assert (
        asyncio.run(
            local_server.generate_reply("letter-2", "candidate input")
        )
        is False
    )
    assert letter["reply_text"] == ""
    assert letter["letter_status"] == "FAILED"
    assert letter["quality_status"] == "blocked"
    assert letter["media_status"] == "NOT_REQUESTED"
    assert letter.get("media_error_code") is None
    assert scheduled == []
    assert "review_prompt" not in letter
    assert "private_world" not in letter


def test_video_reply_timeout_does_not_masquerade_as_a_media_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    class TimeoutPipeline:
        async def run(self, request: object, context: ReplyContext) -> PipelineResult:
            assert context.mode is ReplyMode.MUSICAL_VIDEO
            raise asyncio.TimeoutError

    letter = {
        "letter_id": "letter-timeout",
        "reply_text": "",
        "letter_status": "PENDING",
    }
    monkeypatch.setattr(local_server.store, "letters", [letter])
    monkeypatch.setattr(local_server, "emotion_triage", FakeTriageService())
    monkeypatch.setattr(local_server, "_persist_store_state", lambda: None)
    monkeypatch.setattr(
        local_server, "_schedule_text_reply_delay", lambda *args: None
    )
    monkeypatch.setattr(local_server, "reply_pipeline", TimeoutPipeline())

    assert (
        asyncio.run(
            local_server.generate_reply("letter-timeout", "candidate input")
        )
        is False
    )
    assert letter["letter_status"] == "FAILED"
    assert letter["error_code"] == "LLM_TIMEOUT"
    assert letter["media_status"] == "NOT_REQUESTED"
    assert letter.get("media_error_code") is None


def test_unexpected_video_reply_exception_does_not_leave_media_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    async def explode(*_args: object, **_kwargs: object) -> bool:
        raise Exception("synthetic unexpected failure")

    letter = {
        "letter_id": "letter-unexpected",
        "content": "candidate input",
        "letter_status": "PROCESSING",
        "reply_mode": "musical_video",
        "media_status": "PENDING",
        "media_error_code": "STALE_MEDIA_ERROR",
        "media_retryable": True,
    }
    monkeypatch.setattr(local_server.store, "letters", [letter])
    monkeypatch.setattr(local_server, "generate_reply", explode)
    monkeypatch.setattr(local_server, "_persist_store_state", lambda: None)

    assert (
        asyncio.run(
            local_server._run_reply_job(
                "letter-unexpected",
                "candidate input",
                idempotency_key=None,
            )
        )
        is False
    )
    assert letter["letter_status"] == "FAILED"
    assert letter["error_code"] == "LLM_UNAVAILABLE"
    assert letter["media_status"] == "NOT_REQUESTED"
    assert letter.get("media_error_code") is None
    assert letter["media_retryable"] is False
