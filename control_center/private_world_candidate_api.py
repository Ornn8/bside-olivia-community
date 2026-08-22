"""Authenticated review API for advisory PrivateWorld candidates.

The HTTP layer cannot mutate relationship state by itself.  Approval is passed
to an injected backend that must atomically execute the typed command and record
the candidate decision.  Rejection records only the local-user decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Protocol, Sequence, runtime_checkable

from aiohttp import web

from .app import CSRF_HEADER, SESSION_COOKIE
from .auth import ControlSessionError


CANDIDATE_API_SCHEMA = "p03.private-world-candidate-control.v1"
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_MAX_BODY_BYTES = 8192


class CandidateApiError(ValueError):
    def __init__(self, code: str, *, status: int = 400) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise CandidateApiError(code)
    return value


def _reason(value: object) -> str:
    if not isinstance(value, str):
        raise CandidateApiError("PRIVATE_WORLD_DECISION_REASON_INVALID")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 500
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise CandidateApiError("PRIVATE_WORLD_DECISION_REASON_INVALID")
    return normalized


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise CandidateApiError("PRIVATE_WORLD_DECISION_TIME_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CandidateApiError("PRIVATE_WORLD_DECISION_TIME_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CandidateApiError("PRIVATE_WORLD_DECISION_TIME_INVALID")
    return value


@dataclass(frozen=True)
class CandidateSummary:
    candidate_id: str
    candidate_type: str
    summary: str
    confidence: float
    source_letter_id: str
    source_reply_revision: int
    created_at: str
    expires_at: str
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "PRIVATE_WORLD_CANDIDATE_ID_INVALID")
        if self.candidate_type not in {"boundary_respected", "conflict", "repair"}:
            raise CandidateApiError("PRIVATE_WORLD_CANDIDATE_TYPE_INVALID")
        _reason(self.summary)
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise CandidateApiError("PRIVATE_WORLD_CONFIDENCE_INVALID")
        _identifier(self.source_letter_id, "PRIVATE_WORLD_SOURCE_ID_INVALID")
        if type(self.source_reply_revision) is not int or self.source_reply_revision < 1:
            raise CandidateApiError("PRIVATE_WORLD_REVISION_INVALID")
        _timestamp(self.created_at)
        _timestamp(self.expires_at)
        if len(self.evidence_refs) > 8:
            raise CandidateApiError("PRIVATE_WORLD_EVIDENCE_INVALID")
        for reference in self.evidence_refs:
            _identifier(reference, "PRIVATE_WORLD_EVIDENCE_INVALID")

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "candidate_type": self.candidate_type,
            "summary": self.summary,
            "confidence": round(float(self.confidence), 6),
            "source_letter_id": self.source_letter_id,
            "source_reply_revision": self.source_reply_revision,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class CandidateDecisionRequest:
    candidate_id: str
    decision: str
    idempotency_key: str
    reason: str
    occurred_at: str

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "PRIVATE_WORLD_CANDIDATE_ID_INVALID")
        if self.decision not in {"approve", "reject"}:
            raise CandidateApiError("PRIVATE_WORLD_DECISION_INVALID")
        _identifier(self.idempotency_key, "PRIVATE_WORLD_IDEMPOTENCY_KEY_INVALID")
        object.__setattr__(self, "reason", _reason(self.reason))
        _timestamp(self.occurred_at)


@dataclass(frozen=True)
class CandidateDecisionResult:
    candidate_id: str
    status: str
    decision: str
    command_id: str | None = None
    reason_code: str = "decision_recorded"

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "PRIVATE_WORLD_CANDIDATE_ID_INVALID")
        if self.status not in {"approved", "rejected", "duplicate", "expired"}:
            raise CandidateApiError("PRIVATE_WORLD_CANDIDATE_STATUS_INVALID")
        if self.decision not in {"approve", "reject"}:
            raise CandidateApiError("PRIVATE_WORLD_DECISION_INVALID")
        if self.command_id is not None:
            _identifier(self.command_id, "PRIVATE_WORLD_COMMAND_ID_INVALID")
        _identifier(self.reason_code, "PRIVATE_WORLD_REASON_CODE_INVALID")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": CANDIDATE_API_SCHEMA,
            "candidate_id": self.candidate_id,
            "status": self.status,
            "decision": self.decision,
            "reason_code": self.reason_code,
        }
        if self.command_id is not None:
            payload["command_id"] = self.command_id
        return payload


@runtime_checkable
class CandidateReviewBackend(Protocol):
    def pending(self, *, limit: int) -> Sequence[CandidateSummary]: ...

    def decide(self, request: CandidateDecisionRequest) -> CandidateDecisionResult: ...


def _backend(request: web.Request) -> CandidateReviewBackend:
    backend = request.app.get("private_world_candidate_backend")
    if not isinstance(backend, CandidateReviewBackend):
        raise CandidateApiError("PRIVATE_WORLD_CANDIDATE_CONTROL_UNAVAILABLE", status=503)
    return backend


def _require_session(request: web.Request, *, csrf: bool = False) -> None:
    store = request.app.get("control_session_store")
    if store is None:
        raise CandidateApiError("CONTROL_SESSION_REQUIRED", status=403)
    try:
        store.authenticate(
            request.cookies.get(SESSION_COOKIE),
            csrf_token=request.headers.get(CSRF_HEADER),
            require_csrf=csrf,
        )
    except ControlSessionError as exc:
        raise CandidateApiError(exc.code, status=403) from exc


async def _body(request: web.Request) -> dict[str, object]:
    if request.content_length is not None and request.content_length > _MAX_BODY_BYTES:
        raise CandidateApiError("PRIVATE_WORLD_REQUEST_TOO_LARGE", status=413)
    try:
        value = await request.json(loads=json.loads)
    except (json.JSONDecodeError, UnicodeError, TypeError, ValueError) as exc:
        raise CandidateApiError("PRIVATE_WORLD_JSON_INVALID") from exc
    if not isinstance(value, dict):
        raise CandidateApiError("PRIVATE_WORLD_JSON_INVALID")
    return value


def _error(exc: CandidateApiError) -> web.Response:
    return web.json_response(
        {
            "schema_version": CANDIDATE_API_SCHEMA,
            "status": "UNAVAILABLE" if exc.status == 503 else "FAILED",
            "error_code": exc.code,
        },
        status=exc.status,
    )


async def _pending(request: web.Request) -> web.Response:
    try:
        _require_session(request)
        try:
            limit = int(request.query.get("limit", "50"))
        except ValueError as exc:
            raise CandidateApiError("PRIVATE_WORLD_LIMIT_INVALID") from exc
        if not 1 <= limit <= 200:
            raise CandidateApiError("PRIVATE_WORLD_LIMIT_INVALID")
        values = tuple(_backend(request).pending(limit=limit))
        if len(values) > limit or any(not isinstance(item, CandidateSummary) for item in values):
            raise CandidateApiError("PRIVATE_WORLD_CANDIDATE_CONTROL_INVALID", status=503)
        return web.json_response(
            {
                "schema_version": CANDIDATE_API_SCHEMA,
                "status": "READY",
                "candidates": [item.to_dict() for item in values],
            }
        )
    except CandidateApiError as exc:
        return _error(exc)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _error(
            CandidateApiError("PRIVATE_WORLD_CANDIDATE_CONTROL_UNAVAILABLE", status=503)
        )


async def _decision(request: web.Request) -> web.Response:
    try:
        _require_session(request, csrf=True)
        candidate_id = _identifier(
            request.match_info.get("candidate_id", ""),
            "PRIVATE_WORLD_CANDIDATE_ID_INVALID",
        )
        body = await _body(request)
        decision = request.match_info.get("decision", "")
        if decision not in {"approve", "reject"}:
            raise CandidateApiError("PRIVATE_WORLD_DECISION_INVALID")
        submitted = body.get("candidate_id")
        if submitted is not None and submitted != candidate_id:
            raise CandidateApiError("PRIVATE_WORLD_CANDIDATE_ID_CONFLICT")
        value = CandidateDecisionRequest(
            candidate_id=candidate_id,
            decision=decision,
            idempotency_key=str(body.get("idempotency_key", "")),
            reason=str(body.get("reason", "")),
            occurred_at=str(body.get("occurred_at", "")),
        )
        result = _backend(request).decide(value)
        if not isinstance(result, CandidateDecisionResult):
            raise CandidateApiError("PRIVATE_WORLD_CANDIDATE_CONTROL_INVALID", status=503)
        return web.json_response(result.to_dict())
    except CandidateApiError as exc:
        return _error(exc)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _error(
            CandidateApiError("PRIVATE_WORLD_CANDIDATE_CONTROL_UNAVAILABLE", status=503)
        )


def mount_candidate_review_api(app: web.Application, backend: CandidateReviewBackend) -> None:
    if not isinstance(backend, CandidateReviewBackend):
        raise TypeError("a typed candidate review backend is required")
    if "private_world_candidate_backend" in app:
        raise RuntimeError("PRIVATE_WORLD_CANDIDATE_CONTROL_ALREADY_MOUNTED")
    app["private_world_candidate_backend"] = backend
    app.router.add_get("/control/api/private-world/candidates", _pending)
    app.router.add_post(
        "/control/api/private-world/candidates/{candidate_id}/{decision}",
        _decision,
    )


__all__ = [
    "CANDIDATE_API_SCHEMA",
    "CandidateApiError",
    "CandidateDecisionRequest",
    "CandidateDecisionResult",
    "CandidateReviewBackend",
    "CandidateSummary",
    "mount_candidate_review_api",
]
