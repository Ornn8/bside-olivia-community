"""Service adapter for explicit mutations initiated inside original Olivia.

The adapter deliberately contains no storage implementation.  It maps the
strict original-client mutation contract to the existing auditable Memory
Admin and PrivateWorld candidate-decision services, preserving their own
idempotency, audit, reducer, and persistence rules.
"""

from __future__ import annotations

from dataclasses import is_dataclass
from datetime import datetime
import inspect
import re
from typing import Any, Mapping, get_type_hints

from original_client_companion_mutation_api import (
    CompanionMutationResult,
    OriginalClientCompanionMutationError,
)


_METHODS = {
    "correct_memory": (
        "correct_memory",
        "correct",
        "replace_memory",
        "replace",
    ),
    "delete_memory": (
        "delete_memory",
        "delete",
        "remove_memory",
        "remove",
    ),
    "decide_candidate": (
        "decide_candidate",
        "decide",
        "record_decision",
    ),
}
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")


class OriginalClientCompanionMutationServiceBackend:
    """Map bounded mutation envelopes onto existing domain services."""

    def __init__(
        self,
        *,
        memory_service: object | None = None,
        candidate_service: object | None = None,
    ) -> None:
        self._memory_service = memory_service
        self._candidate_service = candidate_service

    def correct_memory(
        self,
        *,
        memory_id: str,
        replacement_text: str,
        request_id: str,
        reason: str,
    ) -> CompanionMutationResult:
        service = self._require(self._memory_service, "MEMORY_MUTATION_DISABLED")
        result = self._invoke(
            service,
            "correct_memory",
            {
                "memory_id": memory_id,
                "target_id": memory_id,
                "replacement_text": replacement_text,
                "new_text": replacement_text,
                "text": replacement_text,
                "request_id": request_id,
                "idempotency_key": request_id,
                "reason": reason,
            },
        )
        return self._normalize(result, request_id=request_id)

    def delete_memory(
        self,
        *,
        memory_id: str,
        request_id: str,
        reason: str,
    ) -> CompanionMutationResult:
        service = self._require(self._memory_service, "MEMORY_MUTATION_DISABLED")
        result = self._invoke(
            service,
            "delete_memory",
            {
                "memory_id": memory_id,
                "target_id": memory_id,
                "request_id": request_id,
                "idempotency_key": request_id,
                "reason": reason,
            },
        )
        return self._normalize(result, request_id=request_id)

    def decide_candidate(
        self,
        *,
        candidate_id: str,
        decision: str,
        request_id: str,
        reason: str,
        decided_at: str,
    ) -> CompanionMutationResult:
        service = self._require(
            self._candidate_service,
            "CANDIDATE_MUTATION_DISABLED",
        )
        result = self._invoke(
            service,
            "decide_candidate",
            {
                "candidate_id": candidate_id,
                "decision": decision,
                "request_id": request_id,
                "idempotency_key": request_id,
                "reason": reason,
                "decided_at": decided_at,
                "decision_time": decided_at,
                "occurred_at": decided_at,
            },
        )
        return self._normalize(result, request_id=request_id)

    @staticmethod
    def _require(service: object | None, code: str) -> object:
        if service is None:
            raise OriginalClientCompanionMutationError(code, status=503)
        return service

    def _invoke(
        self,
        service: object,
        operation: str,
        values: Mapping[str, object],
    ) -> object:
        methods = [
            getattr(service, name)
            for name in _METHODS[operation]
            if callable(getattr(service, name, None))
        ]
        if len(methods) != 1:
            raise OriginalClientCompanionMutationError(
                "COMPANION_MUTATION_SERVICE_UNSUPPORTED",
                status=503,
            )
        method = methods[0]
        try:
            signature = inspect.signature(method)
            hints = get_type_hints(method)
        except (TypeError, ValueError, NameError):
            signature = inspect.signature(method)
            hints = {}
        positional: list[object] = []
        kwargs: dict[str, object] = {}
        for name, parameter in signature.parameters.items():
            if parameter.kind in {
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            }:
                continue
            value = self._parameter_value(
                name,
                parameter,
                hints.get(name),
                values,
            )
            if value is _MISSING:
                if parameter.default is inspect.Parameter.empty:
                    raise OriginalClientCompanionMutationError(
                        "COMPANION_MUTATION_SERVICE_UNSUPPORTED",
                        status=503,
                    )
                continue
            if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
                positional.append(value)
            else:
                kwargs[name] = value
        try:
            return method(*positional, **kwargs)
        except OriginalClientCompanionMutationError:
            raise
        except Exception as exc:
            self._raise_service_error(exc)
        raise AssertionError("unreachable")

    def _parameter_value(
        self,
        name: str,
        parameter: inspect.Parameter,
        annotation: object | None,
        values: Mapping[str, object],
    ) -> object:
        if name in values:
            return values[name]
        normalized = name.casefold().replace("-", "_")
        aliases = {
            "id": ("memory_id", "candidate_id", "target_id"),
            "target": ("target_id", "memory_id"),
            "replacement": ("replacement_text", "new_text", "text"),
            "fact": ("replacement_text", "text"),
            "key": ("idempotency_key", "request_id"),
            "timestamp": ("decided_at", "decision_time", "occurred_at"),
        }
        for alias, candidates in aliases.items():
            if normalized == alias:
                for candidate in candidates:
                    if candidate in values:
                        return values[candidate]
        if any(token in normalized for token in ("request", "command", "payload")):
            request_type = annotation
            if isinstance(request_type, type) and request_type not in {str, int, bool, float, dict}:
                return self._construct_request(request_type, values)
        if normalized == "decision" and annotation not in {None, str, inspect.Parameter.empty}:
            if isinstance(annotation, type) and annotation is not str:
                return self._construct_request(annotation, values)
        return _MISSING

    def _construct_request(
        self,
        request_type: type,
        values: Mapping[str, object],
    ) -> object:
        try:
            signature = inspect.signature(request_type)
            hints = get_type_hints(request_type)
        except (TypeError, ValueError, NameError) as exc:
            raise OriginalClientCompanionMutationError(
                "COMPANION_MUTATION_SERVICE_UNSUPPORTED",
                status=503,
            ) from exc
        kwargs: dict[str, object] = {}
        for name, parameter in signature.parameters.items():
            if name in values:
                value = values[name]
            elif "time" in name.casefold() or name.casefold().endswith("_at"):
                raw = values.get("decided_at") or values.get("occurred_at")
                hint = hints.get(name)
                value = self._coerce_time(raw, hint)
            elif "key" in name.casefold():
                value = values.get("idempotency_key") or values.get("request_id")
            elif name.casefold() in {"target", "target_id"}:
                value = values.get("target_id") or values.get("memory_id")
            elif name.casefold() in {"replacement", "replacement_text", "new_text", "text"}:
                value = values.get("replacement_text") or values.get("text")
            elif parameter.default is not inspect.Parameter.empty:
                continue
            else:
                raise OriginalClientCompanionMutationError(
                    "COMPANION_MUTATION_SERVICE_UNSUPPORTED",
                    status=503,
                )
            if value is None:
                raise OriginalClientCompanionMutationError(
                    "COMPANION_MUTATION_SERVICE_UNSUPPORTED",
                    status=503,
                )
            kwargs[name] = value
        try:
            return request_type(**kwargs)
        except (TypeError, ValueError) as exc:
            raise OriginalClientCompanionMutationError(
                "COMPANION_MUTATION_SERVICE_UNSUPPORTED",
                status=503,
            ) from exc

    @staticmethod
    def _coerce_time(value: object, annotation: object | None) -> object:
        if annotation is datetime and isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    def _normalize(self, result: object, *, request_id: str) -> CompanionMutationResult:
        if isinstance(result, CompanionMutationResult):
            if result.request_id != request_id:
                raise OriginalClientCompanionMutationError(
                    "COMPANION_MUTATION_RESULT_INVALID",
                    status=503,
                )
            return result
        status_value = self._read(result, "status", "state", "outcome")
        normalized_status = self._status(status_value)
        affected = self._read(
            result,
            "affected_count",
            "affected",
            "count",
        )
        if affected is None:
            applied = self._read(result, "applied", "committed", "changed")
            affected = 1 if applied is True else 0
        if type(affected) is not int or affected < 0:
            raise OriginalClientCompanionMutationError(
                "COMPANION_MUTATION_RESULT_INVALID",
                status=503,
            )
        result_request_id = self._read(
            result,
            "request_id",
            "idempotency_key",
        )
        if result_request_id is not None and result_request_id != request_id:
            raise OriginalClientCompanionMutationError(
                "COMPANION_MUTATION_RESULT_INVALID",
                status=503,
            )
        reason_code = self._read(result, "reason_code", "error_code")
        if reason_code is not None:
            reason_code = str(reason_code).upper()
            if not _CODE_RE.fullmatch(reason_code):
                reason_code = None
        return CompanionMutationResult(
            request_id=request_id,
            status=normalized_status,
            affected_count=affected,
            reason_code=reason_code,
        )

    @staticmethod
    def _read(result: object, *names: str) -> object | None:
        if isinstance(result, Mapping):
            for name in names:
                if name in result:
                    return result[name]
            return None
        for name in names:
            if hasattr(result, name):
                return getattr(result, name)
        return None

    @staticmethod
    def _status(value: object) -> str:
        if hasattr(value, "value"):
            value = getattr(value, "value")
        normalized = str(value or "").strip().casefold().replace("-", "_")
        if normalized in {
            "applied",
            "committed",
            "complete",
            "completed",
            "written",
            "success",
            "approved",
            "rejected_recorded",
        }:
            return "APPLIED"
        if normalized in {"duplicate", "already_applied", "already_committed"}:
            return "DUPLICATE"
        if normalized in {
            "noop",
            "no_op",
            "not_found",
            "missing",
            "unchanged",
            "skipped",
        }:
            return "NOOP"
        if normalized in {"rejected", "denied", "blocked"}:
            return "REJECTED"
        raise OriginalClientCompanionMutationError(
            "COMPANION_MUTATION_RESULT_INVALID",
            status=503,
        )

    @staticmethod
    def _raise_service_error(exc: Exception) -> None:
        code = getattr(exc, "code", None)
        if not isinstance(code, str) or not _CODE_RE.fullmatch(code):
            raise OriginalClientCompanionMutationError(
                "COMPANION_MUTATION_UNAVAILABLE",
                status=503,
            ) from exc
        normalized = code.upper()
        if any(token in normalized for token in ("NOT_FOUND", "MISSING", "EXPIRED")):
            status = 404
        elif any(token in normalized for token in ("CONFLICT", "DUPLICATE", "ALREADY")):
            status = 409
        elif any(token in normalized for token in ("INVALID", "REQUIRED")):
            status = 400
        else:
            status = 503
        raise OriginalClientCompanionMutationError(normalized, status=status) from exc


class _Missing:
    pass


_MISSING = _Missing()


__all__ = ["OriginalClientCompanionMutationServiceBackend"]
