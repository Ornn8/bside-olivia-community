"""Concrete candidate-review backend for the local Control Center.

Approval executes exactly one typed relationship command before recording the
candidate decision. If decision persistence fails after the command commits, a
retry receives the command service's duplicate result and safely finishes the
pending decision. Rejection never executes a relationship command.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from threading import RLock
from typing import Callable

from private_world_candidates import (
    CandidateDecision,
    CandidateDecisionKind,
    CandidateDecisionWriteStatus,
    CandidateStatus,
    CandidateType,
    PrivateWorldCandidate,
    PrivateWorldCandidateError,
    SQLitePrivateWorldCandidateStore,
)
from private_world_commands import (
    PrivateWorldActor,
    PrivateWorldCommandSource,
    RecordBoundaryRespected,
    RecordConflict,
    RecordRepair,
)
from private_world_service import (
    PrivateWorldCommandService,
    PrivateWorldCommandServiceError,
)

from .private_world_candidate_api import (
    CandidateAPIError,
    CandidateDecisionRequest,
    CandidateDecisionResult,
    CandidateSummary,
)


_COMMAND_TYPES = {
    CandidateType.BOUNDARY_RESPECTED: RecordBoundaryRespected,
    CandidateType.CONFLICT: RecordConflict,
    CandidateType.REPAIR: RecordRepair,
}


def _digest(prefix: str, material: str) -> str:
    return f"{prefix}.{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CandidateAPIError(
            "PRIVATE_WORLD_CANDIDATE_DECIDED_AT_INVALID"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CandidateAPIError(
            "PRIVATE_WORLD_CANDIDATE_DECIDED_AT_INVALID"
        )
    return parsed


def _candidate_summary(candidate: PrivateWorldCandidate) -> CandidateSummary:
    return CandidateSummary(
        candidate_id=candidate.candidate_id,
        candidate_type=candidate.candidate_type.value,
        summary=candidate.summary,
        confidence=candidate.confidence,
        source_letter_id=candidate.source_letter_id,
        source_reply_revision=candidate.source_reply_revision,
        created_at=candidate.created_at.isoformat(),
        expires_at=candidate.expires_at.isoformat(),
    )


def _candidate_error(exc: PrivateWorldCandidateError) -> CandidateAPIError:
    code = exc.code
    if code == "PRIVATE_WORLD_CANDIDATE_NOT_FOUND":
        return CandidateAPIError(code, http_status=404)
    if code in {
        "PRIVATE_WORLD_CANDIDATE_NOT_PENDING",
        "PRIVATE_WORLD_CANDIDATE_DECISION_CONFLICT",
        "PRIVATE_WORLD_CANDIDATE_IDENTITY_CONFLICT",
    }:
        return CandidateAPIError(code, http_status=409)
    if code == "PRIVATE_WORLD_CANDIDATE_STORAGE_UNAVAILABLE":
        return CandidateAPIError(code, http_status=503)
    return CandidateAPIError(code)


def _command_error(exc: PrivateWorldCommandServiceError) -> CandidateAPIError:
    if exc.code in {
        "PRIVATE_WORLD_COMMAND_STORAGE_UNAVAILABLE",
        "PRIVATE_WORLD_COMMAND_AUDIT_INVALID",
    }:
        return CandidateAPIError(exc.code, http_status=503)
    if exc.code == "PRIVATE_WORLD_COMMAND_IDENTITY_CONFLICT":
        return CandidateAPIError(exc.code, http_status=409)
    return CandidateAPIError(exc.code)


class SQLiteCandidateReviewBackend:
    """Review pending candidates against one ledger-backed command service."""

    def __init__(
        self,
        store: SQLitePrivateWorldCandidateStore,
        command_service: PrivateWorldCommandService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(store, SQLitePrivateWorldCandidateStore):
            raise TypeError("a SQLite candidate store is required")
        if not isinstance(command_service, PrivateWorldCommandService):
            raise TypeError("a PrivateWorld command service is required")
        self.store = store
        self.command_service = command_service
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()

    def pending(self, *, limit: int):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise CandidateAPIError("CONTROL_LIMIT_INVALID")
        try:
            candidates = self.store.list_candidates(
                status=CandidateStatus.PENDING,
                now=self.clock(),
            )
        except PrivateWorldCandidateError as exc:
            raise _candidate_error(exc) from exc
        return tuple(_candidate_summary(value) for value in candidates[:limit])

    @staticmethod
    def _decision_id(request: CandidateDecisionRequest) -> str:
        return _digest("decision", request.request_id)

    @staticmethod
    def _command_identity(
        candidate: PrivateWorldCandidate,
        request: CandidateDecisionRequest,
    ) -> tuple[str, str]:
        material = f"{candidate.candidate_id}:{request.request_id}"
        return (
            _digest("candidate-command", material),
            _digest("candidate-idempotency", material),
        )

    @staticmethod
    def _evidence(candidate: PrivateWorldCandidate) -> tuple[str, str]:
        letter = _digest("letter", candidate.source_letter_id)
        reply = _digest(
            "reply",
            f"{candidate.source_letter_id}:{candidate.source_reply_revision}",
        )
        return letter, reply

    @staticmethod
    def _duplicate_result(
        decision: CandidateDecision,
    ) -> CandidateDecisionResult:
        return CandidateDecisionResult(
            candidate_id=decision.candidate_id,
            decision=decision.decision.value,
            status="duplicate",
            reason_code="PRIVATE_WORLD_CANDIDATE_DECISION_DUPLICATE",
        )

    def _existing_decision(
        self,
        candidate_id: str,
        request: CandidateDecisionRequest,
    ) -> CandidateDecisionResult | None:
        try:
            existing = self.store.decision(candidate_id)
        except PrivateWorldCandidateError as exc:
            raise _candidate_error(exc) from exc
        if existing is None:
            return None
        if (
            existing.decision_id == self._decision_id(request)
            and existing.decision.value == request.decision
            and existing.reason == request.reason
            and existing.decided_at == _parse_time(request.decided_at)
        ):
            return self._duplicate_result(existing)
        raise CandidateAPIError(
            "PRIVATE_WORLD_CANDIDATE_DECISION_CONFLICT",
            http_status=409,
        )

    def _candidate(
        self,
        candidate_id: str,
        decided_at: datetime,
    ) -> PrivateWorldCandidate:
        try:
            candidate = self.store.get(candidate_id, now=decided_at)
        except PrivateWorldCandidateError as exc:
            raise _candidate_error(exc) from exc
        if candidate is None:
            raise CandidateAPIError(
                "PRIVATE_WORLD_CANDIDATE_NOT_FOUND",
                http_status=404,
            )
        if candidate.status is CandidateStatus.EXPIRED:
            raise CandidateAPIError(
                "PRIVATE_WORLD_CANDIDATE_EXPIRED",
                http_status=409,
            )
        if candidate.status is not CandidateStatus.PENDING:
            raise CandidateAPIError(
                "PRIVATE_WORLD_CANDIDATE_NOT_PENDING",
                http_status=409,
            )
        return candidate

    def _approve(
        self,
        candidate: PrivateWorldCandidate,
        request: CandidateDecisionRequest,
        decided_at: datetime,
    ) -> CandidateDecisionResult:
        command_id, idempotency_key = self._command_identity(
            candidate,
            request,
        )
        command_type = _COMMAND_TYPES[candidate.candidate_type]
        command = command_type(
            command_id=command_id,
            idempotency_key=idempotency_key,
            actor=PrivateWorldActor.LOCAL_USER,
            source=PrivateWorldCommandSource.APPROVED_CANDIDATE,
            occurred_at=decided_at,
            reason=request.reason,
            evidence_refs=self._evidence(candidate),
        )
        try:
            executed = self.command_service.execute(command)
        except PrivateWorldCommandServiceError as exc:
            raise _command_error(exc) from exc
        decision = CandidateDecision(
            decision_id=self._decision_id(request),
            candidate_id=candidate.candidate_id,
            decision=CandidateDecisionKind.APPROVE,
            actor=PrivateWorldActor.LOCAL_USER,
            reason=request.reason,
            decided_at=decided_at,
            command_event_id=executed.event_id,
        )
        try:
            write_status = self.store.record_decision(decision)
        except PrivateWorldCandidateError as exc:
            raise _candidate_error(exc) from exc
        return CandidateDecisionResult(
            candidate_id=candidate.candidate_id,
            decision="approve",
            status=(
                "duplicate"
                if write_status is CandidateDecisionWriteStatus.DUPLICATE
                else "approved"
            ),
            reason_code=(
                "PRIVATE_WORLD_CANDIDATE_DECISION_DUPLICATE"
                if write_status is CandidateDecisionWriteStatus.DUPLICATE
                else "PRIVATE_WORLD_CANDIDATE_APPROVED"
            ),
        )

    def _reject(
        self,
        candidate: PrivateWorldCandidate,
        request: CandidateDecisionRequest,
        decided_at: datetime,
    ) -> CandidateDecisionResult:
        decision = CandidateDecision(
            decision_id=self._decision_id(request),
            candidate_id=candidate.candidate_id,
            decision=CandidateDecisionKind.REJECT,
            actor=PrivateWorldActor.LOCAL_USER,
            reason=request.reason,
            decided_at=decided_at,
        )
        try:
            write_status = self.store.record_decision(decision)
        except PrivateWorldCandidateError as exc:
            raise _candidate_error(exc) from exc
        return CandidateDecisionResult(
            candidate_id=candidate.candidate_id,
            decision="reject",
            status=(
                "duplicate"
                if write_status is CandidateDecisionWriteStatus.DUPLICATE
                else "rejected"
            ),
            reason_code=(
                "PRIVATE_WORLD_CANDIDATE_DECISION_DUPLICATE"
                if write_status is CandidateDecisionWriteStatus.DUPLICATE
                else "PRIVATE_WORLD_CANDIDATE_REJECTED"
            ),
        )

    def decide(
        self,
        request: CandidateDecisionRequest,
    ) -> CandidateDecisionResult:
        if not isinstance(request, CandidateDecisionRequest):
            raise TypeError("a typed candidate decision request is required")
        decided_at = _parse_time(request.decided_at)
        with self._lock:
            existing = self._existing_decision(
                request.candidate_id,
                request,
            )
            if existing is not None:
                return existing
            candidate = self._candidate(
                request.candidate_id,
                decided_at,
            )
            if request.decision == "approve":
                return self._approve(candidate, request, decided_at)
            return self._reject(candidate, request, decided_at)


__all__ = ["SQLiteCandidateReviewBackend"]
