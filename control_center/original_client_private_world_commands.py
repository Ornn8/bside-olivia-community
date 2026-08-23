"""Bridge original Olivia settings commands to the existing PrivateWorld backend."""

from __future__ import annotations

from typing import Mapping

from control_center.private_world_api import (
    PrivateWorldApiCommand,
    PrivateWorldApiError,
    PrivateWorldCommandResult,
    PrivateWorldControlBackend,
)
from original_client_companion_mutation_api import (
    CompanionMutationResult,
    OriginalClientCompanionMutationError,
)


class OriginalClientPrivateWorldCommandServiceBackend:
    """Reuse the canonical typed command path without a second reducer."""

    def __init__(self, backend: PrivateWorldControlBackend | None) -> None:
        self._backend = backend

    def execute_private_world(
        self,
        *,
        operation: str,
        payload: Mapping[str, object],
        request_id: str,
        reason: str,
        occurred_at: str,
    ) -> CompanionMutationResult:
        if self._backend is None:
            raise OriginalClientCompanionMutationError(
                "PRIVATE_WORLD_MUTATION_DISABLED",
                status=503,
            )
        command = PrivateWorldApiCommand(
            operation=operation,
            idempotency_key=request_id,
            reason=reason,
            evidence_refs=(f"control:{request_id}",),
            occurred_at=occurred_at,
            payload=dict(payload),
        )
        try:
            result = self._backend.execute(command)
        except PrivateWorldApiError as exc:
            raise OriginalClientCompanionMutationError(
                exc.code,
                status=exc.status,
            ) from exc
        except Exception as exc:
            code = getattr(exc, "code", None)
            if isinstance(code, str) and code:
                status = self._status_for_code(code)
                raise OriginalClientCompanionMutationError(
                    code.upper(),
                    status=status,
                ) from exc
            raise OriginalClientCompanionMutationError(
                "PRIVATE_WORLD_MUTATION_UNAVAILABLE",
                status=503,
            ) from exc
        if not isinstance(result, PrivateWorldCommandResult):
            raise OriginalClientCompanionMutationError(
                "PRIVATE_WORLD_MUTATION_RESULT_INVALID",
                status=503,
            )
        statuses = {
            "committed": "APPLIED",
            "duplicate": "DUPLICATE",
            "noop": "NOOP",
            "rejected": "REJECTED",
        }
        try:
            status = statuses[result.status]
        except KeyError as exc:
            raise OriginalClientCompanionMutationError(
                "PRIVATE_WORLD_MUTATION_RESULT_INVALID",
                status=503,
            ) from exc
        return CompanionMutationResult(
            request_id=request_id,
            status=status,
            affected_count=1 if result.applied else 0,
            reason_code=result.reason_code,
        )

    @staticmethod
    def _status_for_code(code: str) -> int:
        normalized = code.upper()
        if any(token in normalized for token in ("NOT_FOUND", "MISSING", "EXPIRED")):
            return 404
        if any(token in normalized for token in ("CONFLICT", "ALREADY", "DUPLICATE")):
            return 409
        if any(token in normalized for token in ("INVALID", "REQUIRED", "FORBIDDEN")):
            return 400
        return 503


__all__ = ["OriginalClientPrivateWorldCommandServiceBackend"]
