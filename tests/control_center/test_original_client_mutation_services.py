from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import inspect

import pytest

from control_center.original_client_mutation_services import (
    OriginalClientCompanionMutationServiceBackend,
)
from original_client_companion_mutation_api import (
    OriginalClientCompanionMutationError,
)


class DirectMemoryService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def correct_memory(
        self,
        *,
        memory_id: str,
        replacement_text: str,
        request_id: str,
        reason: str,
    ):
        self.calls.append(("correct", locals().copy()))
        return {
            "status": "applied",
            "request_id": request_id,
            "affected_count": 2,
        }

    def delete_memory(
        self,
        *,
        memory_id: str,
        request_id: str,
        reason: str,
    ):
        self.calls.append(("delete", locals().copy()))
        return {
            "status": "duplicate",
            "request_id": request_id,
            "affected_count": 0,
        }


@dataclass(frozen=True)
class CandidateDecisionRequest:
    request_id: str
    reason: str
    decided_at: datetime


@dataclass(frozen=True)
class CandidateDecisionResult:
    status: str
    applied: bool
    reason_code: str


class RequestCandidateService:
    def __init__(self) -> None:
        self.candidate_id: str | None = None
        self.decision: str | None = None
        self.request: CandidateDecisionRequest | None = None

    def decide(
        self,
        candidate_id: str,
        decision: str,
        request: CandidateDecisionRequest,
    ) -> CandidateDecisionResult:
        self.candidate_id = candidate_id
        self.decision = decision
        self.request = request
        return CandidateDecisionResult(
            status="committed",
            applied=True,
            reason_code="CANDIDATE_DECISION_COMMITTED",
        )


def test_adapter_delegates_direct_memory_operations_and_preserves_idempotency() -> None:
    memory = DirectMemoryService()
    backend = OriginalClientCompanionMutationServiceBackend(memory_service=memory)

    corrected = backend.correct_memory(
        memory_id="memory.fixture.1",
        replacement_text="用户现在住在东京北区。",
        request_id="request.memory.correct.1",
        reason="用户明确纠正。",
    )
    deleted = backend.delete_memory(
        memory_id="memory.fixture.2",
        request_id="request.memory.delete.1",
        reason="用户确认删除。",
    )

    assert corrected.status == "APPLIED"
    assert corrected.affected_count == 2
    assert corrected.request_id == "request.memory.correct.1"
    assert deleted.status == "DUPLICATE"
    assert deleted.affected_count == 0
    assert memory.calls[0][1]["memory_id"] == "memory.fixture.1"
    assert memory.calls[1][1]["request_id"] == "request.memory.delete.1"


def test_adapter_constructs_existing_typed_candidate_request() -> None:
    candidate = RequestCandidateService()
    backend = OriginalClientCompanionMutationServiceBackend(candidate_service=candidate)

    result = backend.decide_candidate(
        candidate_id="candidate.fixture.1",
        decision="approve",
        request_id="request.candidate.approve.1",
        reason="用户明确批准。",
        decided_at="2026-08-23T12:00:00+00:00",
    )

    assert result.status == "APPLIED"
    assert result.affected_count == 1
    assert result.reason_code == "CANDIDATE_DECISION_COMMITTED"
    assert candidate.candidate_id == "candidate.fixture.1"
    assert candidate.decision == "approve"
    assert candidate.request == CandidateDecisionRequest(
        request_id="request.candidate.approve.1",
        reason="用户明确批准。",
        decided_at=datetime.fromisoformat("2026-08-23T12:00:00+00:00"),
    )


def test_disabled_domains_remain_independent_and_honest() -> None:
    backend = OriginalClientCompanionMutationServiceBackend()

    with pytest.raises(OriginalClientCompanionMutationError) as memory:
        backend.delete_memory(
            memory_id="memory.fixture.1",
            request_id="request.memory.delete.1",
            reason="用户确认删除。",
        )
    assert memory.value.code == "MEMORY_MUTATION_DISABLED"
    assert memory.value.status == 503

    with pytest.raises(OriginalClientCompanionMutationError) as candidate:
        backend.decide_candidate(
            candidate_id="candidate.fixture.1",
            decision="reject",
            request_id="request.candidate.reject.1",
            reason="用户明确拒绝。",
            decided_at="2026-08-23T12:00:00+00:00",
        )
    assert candidate.value.code == "CANDIDATE_MUTATION_DISABLED"


def test_service_errors_map_to_stable_http_ready_categories() -> None:
    class ServiceError(RuntimeError):
        def __init__(self, code: str) -> None:
            self.code = code
            super().__init__(code)

    class MissingMemory:
        def delete_memory(self, **_kwargs):
            raise ServiceError("MEMORY_NOT_FOUND")

    backend = OriginalClientCompanionMutationServiceBackend(
        memory_service=MissingMemory()
    )
    with pytest.raises(OriginalClientCompanionMutationError) as error:
        backend.delete_memory(
            memory_id="memory.fixture.1",
            request_id="request.memory.delete.1",
            reason="用户确认删除。",
        )
    assert error.value.code == "MEMORY_NOT_FOUND"
    assert error.value.status == 404


def test_malformed_results_and_ambiguous_services_fail_closed() -> None:
    class MalformedMemory:
        def delete_memory(self, **_kwargs):
            return {"status": "surprising", "affected_count": 1}

    class AmbiguousMemory:
        def delete_memory(self, **_kwargs):
            return {"status": "applied", "affected_count": 1}

        def delete(self, **_kwargs):
            return {"status": "applied", "affected_count": 1}

    for service in (MalformedMemory(), AmbiguousMemory()):
        backend = OriginalClientCompanionMutationServiceBackend(memory_service=service)
        with pytest.raises(OriginalClientCompanionMutationError) as error:
            backend.delete_memory(
                memory_id="memory.fixture.1",
                request_id="request.memory.delete.1",
                reason="用户确认删除。",
            )
        assert error.value.status == 503


def test_adapter_source_contains_no_storage_or_reducer_imports() -> None:
    source = inspect.getsource(
        __import__(
            "control_center.original_client_mutation_services",
            fromlist=["*"],
        )
    ).casefold()
    for forbidden in (
        "import qdrant",
        "from qdrant",
        "import mem0",
        "from mem0",
        "import sqlite3",
        "from private_world_reducer",
        "from private_world_ledger",
    ):
        assert forbidden not in source
