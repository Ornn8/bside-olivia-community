from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from private_world_candidates import (
    CandidateDecision,
    CandidateDecisionKind,
    CandidateDecisionWriteStatus,
    CandidateStatus,
    CandidateType,
    CandidateWriteStatus,
    PrivateWorldCandidate,
    PrivateWorldCandidateError,
    SQLitePrivateWorldCandidateStore,
    candidate_identity,
)
from private_world_commands import PrivateWorldActor
from private_world_ledger import SQLitePrivateWorldLedger


NOW = datetime(2026, 8, 22, 20, 0, tzinfo=timezone.utc)


def _candidate(
    *,
    letter_id: str = "letter.synthetic-1",
    revision: int = 1,
    candidate_type: CandidateType = CandidateType.CONFLICT,
    summary: str = "这轮互动可能形成了一次需要确认的冲突。",
    confidence: float = 0.82,
    created_at: datetime = NOW,
    expires_at: datetime = NOW + timedelta(days=7),
    candidate_id: str | None = None,
) -> PrivateWorldCandidate:
    return PrivateWorldCandidate(
        candidate_id=candidate_id
        or candidate_identity(letter_id, revision, candidate_type),
        source_letter_id=letter_id,
        source_reply_revision=revision,
        candidate_type=candidate_type,
        summary=summary,
        confidence=confidence,
        status=CandidateStatus.PENDING,
        created_at=created_at,
        expires_at=expires_at,
    )


def _store(tmp_path: Path) -> SQLitePrivateWorldCandidateStore:
    database = tmp_path / "private-world.sqlite3"
    SQLitePrivateWorldLedger(database)
    return SQLitePrivateWorldCandidateStore(database)


def test_candidate_identity_is_stable_and_source_scoped() -> None:
    first = candidate_identity(
        "letter.synthetic-1",
        1,
        CandidateType.CONFLICT,
    )
    repeated = candidate_identity(
        "letter.synthetic-1",
        1,
        CandidateType.CONFLICT,
    )
    other_revision = candidate_identity(
        "letter.synthetic-1",
        2,
        CandidateType.CONFLICT,
    )
    other_type = candidate_identity(
        "letter.synthetic-1",
        1,
        CandidateType.REPAIR,
    )

    assert first == repeated
    assert first.startswith("candidate.")
    assert len({first, other_revision, other_type}) == 3


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"candidate_id": ""}, "PRIVATE_WORLD_CANDIDATE_ID_INVALID"),
        (
            {"source_letter_id": "contains whitespace"},
            "PRIVATE_WORLD_CANDIDATE_SOURCE_INVALID",
        ),
        (
            {"source_reply_revision": 0},
            "PRIVATE_WORLD_CANDIDATE_REVISION_INVALID",
        ),
        (
            {"candidate_type": "conflict"},
            "PRIVATE_WORLD_CANDIDATE_TYPE_INVALID",
        ),
        (
            {"summary": ""},
            "PRIVATE_WORLD_CANDIDATE_SUMMARY_INVALID",
        ),
        (
            {"summary": "x" * 281},
            "PRIVATE_WORLD_CANDIDATE_SUMMARY_INVALID",
        ),
        (
            {"summary": "bad\nsummary"},
            "PRIVATE_WORLD_CANDIDATE_SUMMARY_INVALID",
        ),
        (
            {"confidence": 1.1},
            "PRIVATE_WORLD_CANDIDATE_CONFIDENCE_INVALID",
        ),
        (
            {"confidence": True},
            "PRIVATE_WORLD_CANDIDATE_CONFIDENCE_INVALID",
        ),
        (
            {"status": "pending"},
            "PRIVATE_WORLD_CANDIDATE_STATUS_INVALID",
        ),
        (
            {"created_at": datetime(2026, 8, 22, 20, 0)},
            "PRIVATE_WORLD_CANDIDATE_CREATED_AT_INVALID",
        ),
        (
            {"expires_at": NOW},
            "PRIVATE_WORLD_CANDIDATE_EXPIRY_INVALID",
        ),
    ],
)
def test_candidate_contract_is_strict(
    changes: dict[str, object],
    code: str,
) -> None:
    values = {
        "candidate_id": candidate_identity(
            "letter.synthetic-1",
            1,
            CandidateType.CONFLICT,
        ),
        "source_letter_id": "letter.synthetic-1",
        "source_reply_revision": 1,
        "candidate_type": CandidateType.CONFLICT,
        "summary": "合成候选摘要。",
        "confidence": 0.8,
        "status": CandidateStatus.PENDING,
        "created_at": NOW,
        "expires_at": NOW + timedelta(days=7),
        **changes,
    }
    with pytest.raises(PrivateWorldCandidateError, match=code):
        PrivateWorldCandidate(**values)  # type: ignore[arg-type]


def test_store_requires_an_initialized_private_world_ledger(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(
        PrivateWorldCandidateError,
        match="PRIVATE_WORLD_CANDIDATE_LEDGER_REQUIRED",
    ):
        SQLitePrivateWorldCandidateStore(missing)

    unrelated = tmp_path / "unrelated.sqlite3"
    sqlite3.connect(unrelated).close()
    with pytest.raises(
        PrivateWorldCandidateError,
        match="PRIVATE_WORLD_CANDIDATE_LEDGER_REQUIRED",
    ):
        SQLitePrivateWorldCandidateStore(unrelated)


def test_add_is_idempotent_and_rejects_identity_reuse(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    candidate = _candidate()

    assert store.add(candidate) is CandidateWriteStatus.CREATED
    assert store.add(candidate) is CandidateWriteStatus.DUPLICATE

    with pytest.raises(
        PrivateWorldCandidateError,
        match="PRIVATE_WORLD_CANDIDATE_IDENTITY_CONFLICT",
    ):
        store.add(_candidate(summary="同一身份的不同摘要。"))

    with pytest.raises(
        PrivateWorldCandidateError,
        match="PRIVATE_WORLD_CANDIDATE_IDENTITY_CONFLICT",
    ):
        store.add(
            _candidate(
                candidate_id="candidate.manual-conflict",
            )
        )

    stored = store.get(candidate.candidate_id)
    assert stored == candidate
    assert store.health() == {
        "status": "READY",
        "schema_version": 1,
        "pending": 1,
        "approved": 0,
        "rejected": 0,
        "expired": 0,
        "decisions": 0,
    }


def test_store_persists_and_lists_without_private_source_text(
    tmp_path: Path,
) -> None:
    database = tmp_path / "private-world.sqlite3"
    SQLitePrivateWorldLedger(database)
    store = SQLitePrivateWorldCandidateStore(database)
    candidates = (
        _candidate(
            letter_id="letter.synthetic-1",
            candidate_type=CandidateType.CONFLICT,
        ),
        _candidate(
            letter_id="letter.synthetic-2",
            candidate_type=CandidateType.REPAIR,
            created_at=NOW + timedelta(minutes=1),
            expires_at=NOW + timedelta(days=7, minutes=1),
        ),
    )
    for candidate in candidates:
        store.add(candidate)

    reopened = SQLitePrivateWorldCandidateStore(database)
    assert reopened.list_candidates() == tuple(reversed(candidates))
    assert reopened.list_candidates(
        status=CandidateStatus.PENDING
    ) == tuple(reversed(candidates))

    with sqlite3.connect(database) as connection:
        candidate_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(private_world_candidates)"
            )
        }
        decision_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(private_world_candidate_decisions)"
            )
        }
        metadata = connection.execute(
            """SELECT value FROM private_world_metadata
               WHERE key = 'candidate_schema_version'"""
        ).fetchone()
    assert metadata == ("1",)
    for forbidden in (
        "user_message",
        "assistant_message",
        "full_text",
        "evidence_spans",
        "prompt",
    ):
        assert forbidden not in candidate_columns
        assert forbidden not in decision_columns


def test_expiry_is_explicit_and_preserves_unexpired_candidates(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    expired = _candidate(
        letter_id="letter.expired",
        expires_at=NOW + timedelta(hours=1),
    )
    active = _candidate(
        letter_id="letter.active",
        created_at=NOW + timedelta(minutes=1),
        expires_at=NOW + timedelta(days=1),
    )
    store.add(expired)
    store.add(active)

    changed = store.expire(NOW + timedelta(hours=2))

    assert changed == 1
    assert store.get(expired.candidate_id).status is CandidateStatus.EXPIRED
    assert store.get(active.candidate_id).status is CandidateStatus.PENDING
    assert store.list_candidates(
        status=CandidateStatus.PENDING
    ) == (active,)
    assert store.list_candidates(
        status=CandidateStatus.EXPIRED
    ) == (expired.with_status(CandidateStatus.EXPIRED),)


def test_rejection_is_audited_once_and_changes_only_candidate_status(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    candidate = _candidate()
    store.add(candidate)
    decision = CandidateDecision(
        decision_id="decision.synthetic-1",
        candidate_id=candidate.candidate_id,
        decision=CandidateDecisionKind.REJECT,
        actor=PrivateWorldActor.LOCAL_USER,
        reason="用户确认这不是关系事件。",
        decided_at=NOW + timedelta(minutes=1),
    )

    assert (
        store.record_decision(decision)
        is CandidateDecisionWriteStatus.RECORDED
    )
    assert (
        store.record_decision(decision)
        is CandidateDecisionWriteStatus.DUPLICATE
    )
    assert store.decision(candidate.candidate_id) == decision
    assert (
        store.get(candidate.candidate_id).status
        is CandidateStatus.REJECTED
    )

    conflicting = CandidateDecision(
        decision_id="decision.synthetic-2",
        candidate_id=candidate.candidate_id,
        decision=CandidateDecisionKind.REJECT,
        actor=PrivateWorldActor.LOCAL_USER,
        reason="不同的第二次决定。",
        decided_at=NOW + timedelta(minutes=2),
    )
    with pytest.raises(
        PrivateWorldCandidateError,
        match="PRIVATE_WORLD_CANDIDATE_DECISION_CONFLICT",
    ):
        store.record_decision(conflicting)


def test_approval_requires_command_event_and_persists_across_reopen(
    tmp_path: Path,
) -> None:
    database = tmp_path / "private-world.sqlite3"
    SQLitePrivateWorldLedger(database)
    store = SQLitePrivateWorldCandidateStore(database)
    candidate = _candidate()
    store.add(candidate)

    with pytest.raises(
        PrivateWorldCandidateError,
        match="PRIVATE_WORLD_CANDIDATE_COMMAND_EVENT_INVALID",
    ):
        CandidateDecision(
            decision_id="decision.invalid",
            candidate_id=candidate.candidate_id,
            decision=CandidateDecisionKind.APPROVE,
            actor=PrivateWorldActor.LOCAL_USER,
            reason="确认候选。",
            decided_at=NOW + timedelta(minutes=1),
        )

    decision = CandidateDecision(
        decision_id="decision.approve-1",
        candidate_id=candidate.candidate_id,
        decision=CandidateDecisionKind.APPROVE,
        actor=PrivateWorldActor.LOCAL_USER,
        reason="确认候选。",
        decided_at=NOW + timedelta(minutes=1),
        command_event_id="command.event.synthetic-1",
    )
    assert (
        store.record_decision(decision)
        is CandidateDecisionWriteStatus.RECORDED
    )

    reopened = SQLitePrivateWorldCandidateStore(database)
    assert reopened.decision(candidate.candidate_id) == decision
    assert (
        reopened.get(candidate.candidate_id).status
        is CandidateStatus.APPROVED
    )


def test_decision_rejects_missing_expired_or_non_local_candidate(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    candidate = _candidate(expires_at=NOW + timedelta(minutes=1))
    store.add(candidate)

    missing = CandidateDecision(
        decision_id="decision.missing",
        candidate_id="candidate.missing",
        decision=CandidateDecisionKind.REJECT,
        actor=PrivateWorldActor.LOCAL_USER,
        reason="合成测试。",
        decided_at=NOW,
    )
    with pytest.raises(
        PrivateWorldCandidateError,
        match="PRIVATE_WORLD_CANDIDATE_NOT_FOUND",
    ):
        store.record_decision(missing)

    expired = CandidateDecision(
        decision_id="decision.expired",
        candidate_id=candidate.candidate_id,
        decision=CandidateDecisionKind.REJECT,
        actor=PrivateWorldActor.LOCAL_USER,
        reason="过期后不再决定。",
        decided_at=NOW + timedelta(minutes=2),
    )
    with pytest.raises(
        PrivateWorldCandidateError,
        match="PRIVATE_WORLD_CANDIDATE_NOT_PENDING",
    ):
        store.record_decision(expired)

    with pytest.raises(
        PrivateWorldCandidateError,
        match="PRIVATE_WORLD_CANDIDATE_DECISION_ACTOR_INVALID",
    ):
        CandidateDecision(
            decision_id="decision.system",
            candidate_id=candidate.candidate_id,
            decision=CandidateDecisionKind.REJECT,
            actor=PrivateWorldActor.SYSTEM_CANDIDATE,
            reason="系统不能代替用户决定。",
            decided_at=NOW,
        )
