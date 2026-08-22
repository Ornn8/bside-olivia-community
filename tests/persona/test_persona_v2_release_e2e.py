import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from llm_gateway import GatewayConfig
from local_server import LetterAdapter
from memory_port import NullMemoryPort
from private_world_admin import PrivateWorldAdmin
from private_world_delivery import (
    DeliveryEvent,
    DeliveryStatus,
    PrivateWorldDeliveryCommitter,
)
from private_world_ledger import SQLitePrivateWorldLedger
from private_world_port import ContinuationAwareness, PrivateWorldSnapshot
from private_world_reducer import ReducerEventKind
from reply_context import ReplyContext, ReplyMode, TrustedTime
from reply_orchestrator import ReplyResult, ReplyState
from reply_pipeline import ReplyPipeline, UnavailableRewriter
from reply_reviewer import (
    NullReviewer,
    ReviewResult,
    ReviewerScores,
    ReviewStatus,
    ReviewVerdict,
)


NOW = datetime(2026, 8, 22, tzinfo=timezone.utc)


class CompletedOrchestrator:
    def __init__(self, text: str) -> None:
        self.text = text

    async def run(self, request: object) -> ReplyResult:
        return ReplyResult("synthetic-request", ReplyState.COMPLETED, text=self.text)


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


class SnapshotPort:
    def __init__(self, snapshot: PrivateWorldSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> PrivateWorldSnapshot:
        return self._snapshot


def _context(mode: ReplyMode) -> ReplyContext:
    return ReplyContext.create(mode, trusted_time=TrustedTime(NOW))


def test_chinese_letter_uses_release_persona_then_commits_canonical_once(
    tmp_path: Path,
) -> None:
    user_text = "今天有点累，但我想慢慢把生活重新整理好。"
    canonical = "听起来你已经开始为自己留出一点空间了。今晚先做一件小事就好。"
    adapter = LetterAdapter(
        GatewayConfig(provider="mock", model="synthetic"),
        memory_port=NullMemoryPort(),
        now=lambda: NOW,
    )

    messages = adapter._messages(user_text)
    assert messages[1] == {"role": "user", "content": user_text}
    assert "Persona status is DRAFT" not in messages[0]["content"]
    assert "constitution.language_match" in messages[0]["content"]

    result = asyncio.run(
        ReplyPipeline(
            CompletedOrchestrator(canonical),
            reviewer=NullReviewer(),
            rewriter=UnavailableRewriter(),
        ).run(object(), _context(ReplyMode.TEXT_LETTER))
    )
    assert result.state is ReplyState.COMPLETED
    assert result.text == canonical
    assert result.quality_status == "accepted_degraded"

    ledger = SQLitePrivateWorldLedger(tmp_path / "private.sqlite3")
    committer = PrivateWorldDeliveryCommitter(ledger)
    delivery = DeliveryEvent(
        delivery_id="synthetic-letter:1",
        kind=ReducerEventKind.CANONICAL_REPLY_DELIVERED,
        occurred_at=NOW,
        semantic_key="canonical.synthetic-letter",
    )
    assert committer.commit(delivery) is DeliveryStatus.COMMITTED
    assert committer.commit(delivery) is DeliveryStatus.DUPLICATE
    assert ledger.health()["event_count"] == 1


@pytest.mark.parametrize("mode", [ReplyMode.SPOKEN_VIDEO, ReplyMode.MUSICAL_VIDEO])
def test_media_spoken_text_rewrites_stage_directions_once(mode: ReplyMode) -> None:
    rewriter = FixedRewriter("我听见了。先不用急着给自己一个结论。")
    result = asyncio.run(
        ReplyPipeline(
            CompletedOrchestrator("(smiles)\n我听见了。"),
            reviewer=PassingReviewer(),
            rewriter=rewriter,
        ).run(object(), _context(mode))
    )

    assert result.state is ReplyState.COMPLETED
    assert result.rewrite_calls == 1
    assert rewriter.calls == 1
    assert "(" not in result.text


def test_reviewer_outage_degrades_clean_text_but_hard_violation_blocks() -> None:
    clean = asyncio.run(
        ReplyPipeline(
            CompletedOrchestrator("我会认真读完这封信。"),
            reviewer=NullReviewer(),
            rewriter=UnavailableRewriter(),
        ).run(object(), _context(ReplyMode.TEXT_LETTER))
    )
    blocked = asyncio.run(
        ReplyPipeline(
            CompletedOrchestrator("<CONTROL>hidden</CONTROL>"),
            reviewer=NullReviewer(),
            rewriter=UnavailableRewriter(),
        ).run(object(), _context(ReplyMode.TEXT_LETTER))
    )

    assert clean.state is ReplyState.COMPLETED
    assert clean.quality_status == "accepted_degraded"
    assert blocked.state is ReplyState.FAILED
    assert blocked.text == ""
    assert blocked.rewrite_calls == 1


def test_corrupt_persona_and_unknown_continuation_stay_out_of_character_view(
    tmp_path: Path,
) -> None:
    corrupt = tmp_path / "persona.json"
    corrupt.write_text("{broken", encoding="utf-8")
    adapter = LetterAdapter(
        GatewayConfig(
            provider="mock",
            model="synthetic",
            persona_v2_file=str(corrupt),
        ),
        memory_port=NullMemoryPort(),
        private_world_port=SnapshotPort(
            PrivateWorldSnapshot(
                trust=91,
                continuation_awareness=ContinuationAwareness.PENDING,
            )
        ),
        now=lambda: NOW,
    )

    system = adapter._messages("合成输入")[0]["content"]
    assert "Persona status is DRAFT" in system
    assert "91" not in system
    assert "pending" not in system
    assert "control_only" not in system


def test_private_world_export_reset_and_delete_are_explicit_and_complete(
    tmp_path: Path,
) -> None:
    database = tmp_path / "private.sqlite3"
    ledger = SQLitePrivateWorldLedger(database)
    committer = PrivateWorldDeliveryCommitter(ledger)
    committer.commit(
        DeliveryEvent(
            delivery_id="admin-letter:1",
            kind=ReducerEventKind.CANONICAL_REPLY_DELIVERED,
            occurred_at=NOW,
            semantic_key="canonical.admin-letter",
        )
    )
    admin = PrivateWorldAdmin(database)
    exported = tmp_path / "export.json"

    admin.export(exported, confirmed=True)
    assert json.loads(exported.read_text(encoding="utf-8"))["events"]
    admin.reset(confirmed=True)
    assert SQLitePrivateWorldLedger(database).health()["event_count"] == 0
    admin.delete(confirmed=True)
    assert not database.exists()
