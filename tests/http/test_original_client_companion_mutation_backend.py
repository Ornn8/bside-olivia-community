from __future__ import annotations

from dataclasses import dataclass

import pytest

from control_center.private_world_candidate_api import (
    CandidateAPIError,
    CandidateDecisionRequest,
    CandidateDecisionResult,
)
from conversation_memory_admin import (
    ConversationMemoryAdminError,
    MemoryAdminMutationResult,
    MemoryAdminMutationStatus,
)
from original_client_companion_mutation_api import (
    OriginalClientCompanionMutationError,
)
from original_client_companion_mutation_backend import (
    DirectOriginalClientCompanionMutationBackend,
)


class MemoryAdminFixture:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def correct(
        self,
        memory_id: str,
        corrected_text: str,
        *,
        request_id: str,
        reason: str,
    ) -> MemoryAdminMutationResult:
        self.calls.append(
            (
                "correct",
                (memory_id, corrected_text),
                {"request_id": request_id, "reason": reason},
            )
        )
        return MemoryAdminMutationResult(
            MemoryAdminMutationStatus.APPLIED,
            request_id,
            "correct",
            affected_count=2,
            target_memory_id=memory_id,
            replacement_memory_id="memory.corrected.1",
        )

    def delete(
        self,
        memory_id: str,
        *,
        request_id: str,
        reason: str,
    ) -> MemoryAdminMutationResult:
        self.calls.append(
            (
                "delete",
                (memory_id,),
                {"request_id": request_id, "reason": reason},
            )
        )
        return MemoryAdminMutationResult(
            MemoryAdminMutationStatus.NOOP,
            request_id,
            "delete",
            target_memory_id=memory_id,
        )

    def clear(
        self,
        *,
        request_id: str,
        reason: str,
        confirmed: bool,
    ) -> MemoryAdminMutationResult:
        self.calls.append(
            (
                "clear",
                (),
                {
                    "request_id": request_id,
                    "reason": reason,
                    "confirmed": confirmed,
                },
            )
        )
        return MemoryAdminMutationResult(
            MemoryAdminMutationStatus.APPLIED,
            request_id,
            "clear",
            affected_count=2,
        )


class CandidateDecisionFixture:
    def __init__(self, status: str = "approved") -> None:
        self.status = status
        self.requests: list[CandidateDecisionRequest] = []

    def decide(
        self,
        request: CandidateDecisionRequest,
    ) -> CandidateDecisionResult:
        self.requests.append(request)
        return CandidateDecisionResult(
            candidate_id=request.candidate_id,
            decision=request.decision,
            status=self.status,
            reason_code=(
                "PRIVATE_WORLD_CANDIDATE_DECISION_DUPLICATE"
                if self.status == "duplicate"
                else "PRIVATE_WORLD_CANDIDATE_APPROVED"
                if request.decision == "approve"
                else "PRIVATE_WORLD_CANDIDATE_REJECTED"
            ),
        )


def test_memory_operations_call_exact_existing_service_methods() -> None:
    memory = MemoryAdminFixture()
    backend = DirectOriginalClientCompanionMutationBackend(memory_admin=memory)

    corrected = backend.correct_memory(
        memory_id="memory.fixture.1",
        replacement_text="用户现在住在东京北区。",
        request_id="request.memory.correct.1",
        reason="用户明确纠正。",
    )
    deleted = backend.delete_memory(
        memory_id="memory.fixture.2",
        request_id="request.memory.delete.1",
        reason="用户明确删除。",
    )
    cleared = backend.clear_memory(
        request_id="request.memory.clear.1",
        reason="用户明确清空当前长期记忆。",
        confirmed=True,
    )

    assert corrected.status == "APPLIED"
    assert corrected.affected_count == 2
    assert deleted.status == "NOOP"
    assert deleted.affected_count == 0
    assert cleared.status == "APPLIED"
    assert cleared.affected_count == 2
    assert memory.calls == [
        (
            "correct",
            ("memory.fixture.1", "用户现在住在东京北区。"),
            {
                "request_id": "request.memory.correct.1",
                "reason": "用户明确纠正。",
            },
        ),
        (
            "delete",
            ("memory.fixture.2",),
            {
                "request_id": "request.memory.delete.1",
                "reason": "用户明确删除。",
            },
        ),
        (
            "clear",
            (),
            {
                "request_id": "request.memory.clear.1",
                "reason": "用户明确清空当前长期记忆。",
                "confirmed": True,
            },
        ),
    ]


@pytest.mark.parametrize(
    ("decision", "service_status", "public_status", "affected"),
    [
        ("approve", "approved", "APPLIED", 1),
        ("reject", "rejected", "APPLIED", 1),
        ("approve", "duplicate", "DUPLICATE", 0),
    ],
)
def test_candidate_decision_constructs_exact_existing_request(
    decision: str,
    service_status: str,
    public_status: str,
    affected: int,
) -> None:
    candidates = CandidateDecisionFixture(service_status)
    backend = DirectOriginalClientCompanionMutationBackend(
        candidate_decisions=candidates
    )

    result = backend.decide_candidate(
        candidate_id="candidate.fixture.1",
        decision=decision,
        request_id="request.candidate.decision.1",
        reason="用户明确确认。",
        decided_at="2026-08-23T12:00:00+00:00",
    )

    assert result.status == public_status
    assert result.affected_count == affected
    assert len(candidates.requests) == 1
    request = candidates.requests[0]
    assert request == CandidateDecisionRequest(
        candidate_id="candidate.fixture.1",
        decision=decision,
        request_id="request.candidate.decision.1",
        reason="用户明确确认。",
        decided_at="2026-08-23T12:00:00+00:00",
    )


def test_missing_services_are_independently_unavailable() -> None:
    backend = DirectOriginalClientCompanionMutationBackend()

    with pytest.raises(OriginalClientCompanionMutationError) as memory_error:
        backend.delete_memory(
            memory_id="memory.fixture.1",
            request_id="request.memory.delete.1",
            reason="用户明确删除。",
        )
    assert memory_error.value.code == "MEMORY_MUTATION_DISABLED"
    assert memory_error.value.status == 503

    with pytest.raises(OriginalClientCompanionMutationError) as candidate_error:
        backend.decide_candidate(
            candidate_id="candidate.fixture.1",
            decision="approve",
            request_id="request.candidate.decision.1",
            reason="用户明确确认。",
            decided_at="2026-08-23T12:00:00+00:00",
        )
    assert candidate_error.value.code == "CANDIDATE_MUTATION_DISABLED"
    assert candidate_error.value.status == 503


def test_domain_errors_remain_stable_and_path_free() -> None:
    class FailingMemory(MemoryAdminFixture):
        def delete(self, *args, **kwargs):
            raise ConversationMemoryAdminError("MEMORY_ADMIN_REQUEST_CONFLICT")

    class FailingCandidate(CandidateDecisionFixture):
        def decide(self, request: CandidateDecisionRequest):
            raise CandidateAPIError(
                "PRIVATE_WORLD_CANDIDATE_NOT_FOUND",
                http_status=404,
            )

    backend = DirectOriginalClientCompanionMutationBackend(
        memory_admin=FailingMemory(),
        candidate_decisions=FailingCandidate(),
    )
    with pytest.raises(OriginalClientCompanionMutationError) as memory_error:
        backend.delete_memory(
            memory_id="memory.fixture.1",
            request_id="request.memory.delete.1",
            reason="用户明确删除。",
        )
    assert memory_error.value.code == "MEMORY_ADMIN_REQUEST_CONFLICT"
    assert memory_error.value.status == 409

    with pytest.raises(OriginalClientCompanionMutationError) as candidate_error:
        backend.decide_candidate(
            candidate_id="candidate.fixture.1",
            decision="approve",
            request_id="request.candidate.decision.1",
            reason="用户明确确认。",
            decided_at="2026-08-23T12:00:00+00:00",
        )
    assert candidate_error.value.code == "PRIVATE_WORLD_CANDIDATE_NOT_FOUND"
    assert candidate_error.value.status == 404
    assert "path" not in str(candidate_error.value).casefold()


def test_backend_has_no_dynamic_discovery_surface() -> None:
    import inspect
    import original_client_companion_mutation_backend as module

    source = inspect.getsource(module)
    for forbidden in (
        "inspect.signature",
        "importlib.import_module",
        "__dict__",
        "dir(",
        "get_type_hints",
        "deque(",
    ):
        assert forbidden not in source
