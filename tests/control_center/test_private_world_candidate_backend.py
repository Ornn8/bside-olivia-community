from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from control_center.private_world_candidate_api import (
    CandidateAPIError,
    CandidateDecisionRequest,
)
from control_center.private_world_candidate_backend import (
    SQLiteCandidateReviewBackend,
)
from private_world_candidates import (
    CandidateStatus,
    CandidateType,
    PrivateWorldCandidate,
    PrivateWorldCandidateError,
    SQLitePrivateWorldCandidateStore,
    candidate_identity,
)
from private_world_ledger import SQLitePrivateWorldLedger
from private_world_service import PrivateWorldCommandService


NOW = datetime(2026, 8, 23, 4, 0, tzinfo=timezone.utc)


def _candidate(
    candidate_type: CandidateType,
    *,
    source: str = "letter-fixture-1",
    revision: int = 1,
    expires_delta: timedelta = timedelta(days=7),
) -> PrivateWorldCandidate:
    return PrivateWorldCandidate(
        candidate_id=candidate_identity(source, revision, candidate_type),
        source_letter_id=source,
        source_reply_revision=revision,
        candidate_type=candidate_type,
        summary="这是一条合成的关系事件候选。",
        confidence=0.82,
        status=CandidateStatus.PENDING,
        created_at=NOW,
        expires_at=NOW + expires_delta,
    )


def _request(
    candidate: PrivateWorldCandidate,
    decision: str,
    *,
    request_id: str = "candidate-review.fixture.1",
    reason: str = "用户在管理界面确认这条候选。",
    decided_at: datetime = NOW + timedelta(minutes=5),
) -> CandidateDecisionRequest:
    return CandidateDecisionRequest(
        candidate_id=candidate.candidate_id,
        decision=decision,
        request_id=request_id,
        reason=reason,
        decided_at=decided_at.isoformat(),
    )


def _backend(
    tmp_path: Path,
    *,
    store_type=SQLitePrivateWorldCandidateStore,
):
    path = tmp_path / "private_world.sqlite3"
    ledger = SQLitePrivateWorldLedger(path)
    store = store_type(path)
    service = PrivateWorldCommandService(ledger)
    backend = SQLiteCandidateReviewBackend(
        store,
        service,
        clock=lambda: NOW + timedelta(minutes=1),
    )
    return backend, ledger, store


def test_pending_and_approval_commit_one_typed_relationship_event(
    tmp_path: Path,
) -> None:
    backend, ledger, store = _backend(tmp_path)
    candidate = _candidate(CandidateType.CONFLICT)
    store.add(candidate)

    pending = backend.pending(limit=10)
    assert len(pending) == 1
    assert pending[0].candidate_id == candidate.candidate_id
    assert pending[0].candidate_type == "conflict"

    request = _request(candidate, "approve")
    approved = backend.decide(request)
    assert approved.status == "approved"
    assert approved.reason_code == "PRIVATE_WORLD_CANDIDATE_APPROVED"
    assert ledger.snapshot().tension == 3
    assert len(ledger.events()) == 1

    stored = store.decision(candidate.candidate_id)
    assert stored is not None
    assert stored.command_event_id == ledger.events()[0].event_id
    assert store.get(candidate.candidate_id).status is CandidateStatus.APPROVED

    duplicate = backend.decide(request)
    assert duplicate.status == "duplicate"
    assert len(ledger.events()) == 1


def test_rejection_records_decision_without_relationship_command(
    tmp_path: Path,
) -> None:
    backend, ledger, store = _backend(tmp_path)
    candidate = _candidate(CandidateType.REPAIR)
    store.add(candidate)

    rejected = backend.decide(_request(candidate, "reject"))
    assert rejected.status == "rejected"
    assert rejected.reason_code == "PRIVATE_WORLD_CANDIDATE_REJECTED"
    assert ledger.events() == ()

    stored = store.decision(candidate.candidate_id)
    assert stored is not None
    assert stored.command_event_id is None
    assert store.get(candidate.candidate_id).status is CandidateStatus.REJECTED


def test_expired_missing_and_conflicting_decisions_are_explicit(
    tmp_path: Path,
) -> None:
    backend, _ledger, store = _backend(tmp_path)
    expired = _candidate(
        CandidateType.BOUNDARY_RESPECTED,
        expires_delta=timedelta(minutes=2),
    )
    store.add(expired)

    with pytest.raises(CandidateAPIError) as expired_error:
        backend.decide(
            _request(
                expired,
                "approve",
                decided_at=NOW + timedelta(minutes=3),
            )
        )
    assert expired_error.value.code == "PRIVATE_WORLD_CANDIDATE_EXPIRED"
    assert expired_error.value.http_status == 409

    missing = _candidate(
        CandidateType.CONFLICT,
        source="letter-missing",
    )
    with pytest.raises(CandidateAPIError) as missing_error:
        backend.decide(_request(missing, "reject"))
    assert missing_error.value.code == "PRIVATE_WORLD_CANDIDATE_NOT_FOUND"
    assert missing_error.value.http_status == 404

    current = _candidate(CandidateType.REPAIR, source="letter-current")
    store.add(current)
    backend.decide(_request(current, "reject"))
    with pytest.raises(CandidateAPIError) as conflict:
        backend.decide(
            _request(
                current,
                "approve",
                request_id="candidate-review.fixture.2",
            )
        )
    assert conflict.value.code == (
        "PRIVATE_WORLD_CANDIDATE_DECISION_CONFLICT"
    )
    assert conflict.value.http_status == 409


def test_retry_finishes_decision_after_command_committed_first(
    tmp_path: Path,
) -> None:
    class FlakyCandidateStore(SQLitePrivateWorldCandidateStore):
        def __init__(self, database_path: Path) -> None:
            super().__init__(database_path)
            self.fail_once = True

        def record_decision(self, decision):
            if self.fail_once:
                self.fail_once = False
                raise PrivateWorldCandidateError(
                    "PRIVATE_WORLD_CANDIDATE_STORAGE_UNAVAILABLE"
                )
            return super().record_decision(decision)

    backend, ledger, store = _backend(
        tmp_path,
        store_type=FlakyCandidateStore,
    )
    candidate = _candidate(CandidateType.CONFLICT)
    store.add(candidate)
    request = _request(candidate, "approve")

    with pytest.raises(CandidateAPIError) as first:
        backend.decide(request)
    assert first.value.code == "PRIVATE_WORLD_CANDIDATE_STORAGE_UNAVAILABLE"
    assert first.value.http_status == 503
    assert len(ledger.events()) == 1
    assert store.get(candidate.candidate_id).status is CandidateStatus.PENDING

    recovered = backend.decide(request)
    assert recovered.status == "approved"
    assert len(ledger.events()) == 1
    assert store.decision(candidate.candidate_id) is not None
