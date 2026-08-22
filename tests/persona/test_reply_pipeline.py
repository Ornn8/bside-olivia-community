import asyncio
from datetime import datetime, timezone

import pytest

from reply_context import ReplyContext, ReplyMode, TrustedTime
from reply_orchestrator import ReplyResult, ReplyState
from reply_pipeline import (
    PipelineResult,
    ReplyPipeline,
    UnavailableRewriter,
)
from reply_reviewer import (
    NullReviewer,
    ReviewResult,
    ReviewerScores,
    ReviewStatus,
    ReviewVerdict,
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


class FakeTriage:
    reply_mode = "video"

    def to_dict(self) -> dict[str, str]:
        return {"reply_mode": self.reply_mode}


class FakeTriageService:
    async def classify(self, content: str) -> FakeTriage:
        return FakeTriage()


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

    letter = {"letter_id": "letter-1", "reply_text": "", "letter_status": "PENDING"}
    scheduled: list[tuple[object, ...]] = []
    remembered: list[tuple[str, str]] = []
    monkeypatch.setattr(local_server.store, "letters", [letter])
    monkeypatch.setattr(local_server, "emotion_triage", FakeTriageService())
    monkeypatch.setattr(local_server, "_persist_store_state", lambda: None)
    monkeypatch.setattr(local_server, "_schedule_text_reply_delay", lambda *args: None)
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
                text="canonical final text",
                quality_status="accepted",
                violation_codes=("SYNTHETIC_FIXED",),
                reviewer_calls=2,
                rewrite_calls=1,
            )
        ),
    )

    assert asyncio.run(local_server.generate_reply("letter-1", "candidate input")) is True
    assert letter["reply_text"] == "canonical final text"
    assert letter["letter_status"] == "COMPLETED"
    assert letter["quality_status"] == "accepted"
    assert letter["quality_violation_codes"] == ["SYNTHETIC_FIXED"]
    assert scheduled[0][2] == "canonical final text"
    assert remembered == [("candidate input", "canonical final text")]


def test_blocked_candidate_never_reaches_storage_or_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    letter = {"letter_id": "letter-2", "reply_text": "", "letter_status": "PENDING"}
    scheduled: list[tuple[object, ...]] = []
    monkeypatch.setattr(local_server.store, "letters", [letter])
    monkeypatch.setattr(local_server, "emotion_triage", FakeTriageService())
    monkeypatch.setattr(local_server, "_persist_store_state", lambda: None)
    monkeypatch.setattr(local_server, "_schedule_text_reply_delay", lambda *args: None)
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

    assert asyncio.run(local_server.generate_reply("letter-2", "candidate input")) is False
    assert letter["reply_text"] == ""
    assert letter["letter_status"] == "FAILED"
    assert letter["quality_status"] == "blocked"
    assert scheduled == []
    assert "review_prompt" not in letter
    assert "private_world" not in letter
