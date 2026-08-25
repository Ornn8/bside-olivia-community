"""Direct service adapter for mutations initiated inside original Olivia.

The adapter calls the repository's existing auditable services by their explicit
public methods.  It contains no method guessing, signature reflection, object
graph traversal, storage implementation, reducer, or second command path.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

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
    CompanionMutationResult,
    OriginalClientCompanionMutationError,
)


@runtime_checkable
class MemoryAdminMutationService(Protocol):
    def correct(
        self,
        memory_id: str,
        corrected_text: str,
        *,
        request_id: str,
        reason: str,
    ) -> MemoryAdminMutationResult: ...

    def delete(
        self,
        memory_id: str,
        *,
        request_id: str,
        reason: str,
    ) -> MemoryAdminMutationResult: ...


@runtime_checkable
class CandidateDecisionService(Protocol):
    def decide(
        self,
        request: CandidateDecisionRequest,
    ) -> CandidateDecisionResult: ...


def _memory_error(exc: ConversationMemoryAdminError) -> OriginalClientCompanionMutationError:
    code = exc.code
    if code == "MEMORY_ADMIN_REQUEST_CONFLICT":
        status = 409
    elif "INVALID" in code or "REQUIRED" in code:
        status = 400
    else:
        status = 503
    return OriginalClientCompanionMutationError(code, status=status)


def _candidate_error(exc: CandidateAPIError) -> OriginalClientCompanionMutationError:
    status = int(getattr(exc, "http_status", 400))
    if status not in {400, 403, 404, 409, 413, 415, 503}:
        status = 503
    return OriginalClientCompanionMutationError(exc.code, status=status)


def _memory_result(result: MemoryAdminMutationResult) -> CompanionMutationResult:
    if not isinstance(result, MemoryAdminMutationResult):
        raise OriginalClientCompanionMutationError(
            "MEMORY_MUTATION_RESULT_INVALID",
            status=503,
        )
    statuses = {
        MemoryAdminMutationStatus.APPLIED: "APPLIED",
        MemoryAdminMutationStatus.DUPLICATE: "DUPLICATE",
        MemoryAdminMutationStatus.NOOP: "NOOP",
    }
    return CompanionMutationResult(
        request_id=result.request_id,
        status=statuses[result.status],
        affected_count=result.affected_count,
    )


def _candidate_result(
    result: CandidateDecisionResult,
    *,
    request_id: str,
) -> CompanionMutationResult:
    if not isinstance(result, CandidateDecisionResult):
        raise OriginalClientCompanionMutationError(
            "CANDIDATE_MUTATION_RESULT_INVALID",
            status=503,
        )
    statuses = {
        "approved": ("APPLIED", 1),
        "rejected": ("APPLIED", 1),
        "duplicate": ("DUPLICATE", 0),
        "expired": ("REJECTED", 0),
    }
    try:
        status, affected_count = statuses[result.status]
    except KeyError as exc:
        raise OriginalClientCompanionMutationError(
            "CANDIDATE_MUTATION_RESULT_INVALID",
            status=503,
        ) from exc
    return CompanionMutationResult(
        request_id=request_id,
        status=status,
        affected_count=affected_count,
        reason_code=result.reason_code,
    )


class DirectOriginalClientCompanionMutationBackend:
    """Map the original Settings transport to the canonical services."""

    def __init__(
        self,
        *,
        memory_admin: MemoryAdminMutationService | None = None,
        candidate_decisions: CandidateDecisionService | None = None,
        video_reply_settings: object | None = None,
    ) -> None:
        if memory_admin is not None and not isinstance(
            memory_admin,
            MemoryAdminMutationService,
        ):
            raise TypeError("an explicit Memory Admin service is required")
        if candidate_decisions is not None and not isinstance(
            candidate_decisions,
            CandidateDecisionService,
        ):
            raise TypeError("an explicit candidate decision service is required")
        self.memory_admin = memory_admin
        self.candidate_decisions = candidate_decisions
        self.video_reply_settings = video_reply_settings

    def correct_memory(
        self,
        *,
        memory_id: str,
        replacement_text: str,
        request_id: str,
        reason: str,
    ) -> CompanionMutationResult:
        if self.memory_admin is None:
            raise OriginalClientCompanionMutationError(
                "MEMORY_MUTATION_DISABLED",
                status=503,
            )
        try:
            result = self.memory_admin.correct(
                memory_id,
                replacement_text,
                request_id=request_id,
                reason=reason,
            )
        except ConversationMemoryAdminError as exc:
            raise _memory_error(exc) from exc
        return _memory_result(result)

    def delete_memory(
        self,
        *,
        memory_id: str,
        request_id: str,
        reason: str,
    ) -> CompanionMutationResult:
        if self.memory_admin is None:
            raise OriginalClientCompanionMutationError(
                "MEMORY_MUTATION_DISABLED",
                status=503,
            )
        try:
            result = self.memory_admin.delete(
                memory_id,
                request_id=request_id,
                reason=reason,
            )
        except ConversationMemoryAdminError as exc:
            raise _memory_error(exc) from exc
        return _memory_result(result)

    def set_video_reply_enabled(
        self,
        *,
        enabled: bool,
        request_id: str,
        reason: str,
    ) -> CompanionMutationResult:
        service = self.video_reply_settings
        setter = getattr(service, "write_video_reply_enabled", None)
        if not callable(setter):
            raise OriginalClientCompanionMutationError(
                "VIDEO_REPLY_SETTINGS_UNAVAILABLE",
                status=503,
            )
        if type(enabled) is not bool:
            raise OriginalClientCompanionMutationError(
                "VIDEO_REPLY_ENABLED_INVALID",
                status=400,
            )
        try:
            result = setter(enabled, request_id=request_id, reason=reason)
        except OriginalClientCompanionMutationError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise OriginalClientCompanionMutationError(
                "VIDEO_REPLY_SETTINGS_UNAVAILABLE",
                status=503,
            ) from exc
        if isinstance(result, CompanionMutationResult):
            return result
        if type(result) is not bool:
            raise OriginalClientCompanionMutationError(
                "VIDEO_REPLY_SETTINGS_INVALID",
                status=503,
            )
        return CompanionMutationResult(
            request_id=request_id,
            status="APPLIED" if result else "NOOP",
            affected_count=1 if result else 0,
        )

    def decide_candidate(
        self,
        *,
        candidate_id: str,
        decision: str,
        request_id: str,
        reason: str,
        decided_at: str,
    ) -> CompanionMutationResult:
        if self.candidate_decisions is None:
            raise OriginalClientCompanionMutationError(
                "CANDIDATE_MUTATION_DISABLED",
                status=503,
            )
        try:
            request = CandidateDecisionRequest(
                candidate_id=candidate_id,
                decision=decision,
                request_id=request_id,
                reason=reason,
                decided_at=decided_at,
            )
            result = self.candidate_decisions.decide(request)
        except CandidateAPIError as exc:
            raise _candidate_error(exc) from exc
        return _candidate_result(result, request_id=request_id)


__all__ = [
    "CandidateDecisionService",
    "DirectOriginalClientCompanionMutationBackend",
    "MemoryAdminMutationService",
]
