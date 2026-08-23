"""Authenticated review API for bounded PrivateWorld candidates.

The Control Center middleware owns loopback, session, Origin, and CSRF checks.
This adapter exposes advisory summaries and delegates decisions to an injected
backend; it never writes SQLite or relationship state directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Mapping, Protocol, Sequence, runtime_checkable

from aiohttp import web

from .private_world_api import PrivateWorldAPIError


PRIVATE_WORLD_CANDIDATE_CONTROL_SCHEMA = (
    "p03.private-world-candidate-control.v1"
)
_CANDIDATE_TYPES = frozenset(
    {"boundary_respected", "conflict", "repair"}
)
_DECISIONS = frozenset({"approve", "reject"})
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
_MAX_TEXT_LENGTH = 280


class CandidateAPIError(PrivateWorldAPIError):
    """Stable candidate-control error handled by the shared middleware."""


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

    def __post_init__(self) -> None:
        _identifier(
            self.candidate_id,
            code="PRIVATE_WORLD_CANDIDATE_ID_INVALID",
        )
        if self.candidate_type not in _CANDIDATE_TYPES:
            raise CandidateAPIError(
                "PRIVATE_WORLD_CANDIDATE_TYPE_INVALID"
            )
        object.__setattr__(
            self,
            "summary",
            _text(
                self.summary,
                code="PRIVATE_WORLD_CANDIDATE_SUMMARY_INVALID",
            ),
        )
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not 0 <= float(self.confidence) <= 1
        ):
            raise CandidateAPIError(
                "PRIVATE_WORLD_CANDIDATE_CONFIDENCE_INVALID"
            )
        object.__setattr__(self, "confidence", float(self.confidence))
        _identifier(
            self.source_letter_id,
            code="PRIVATE_WORLD_CANDIDATE_SOURCE_INVALID",
        )
        if (
            type(self.source_reply_revision) is not int
            or self.source_reply_revision < 1
        ):
            raise CandidateAPIError(
                "PRIVATE_WORLD_CANDIDATE_REVISION_INVALID"
            )
        _timestamp(
            self.created_at,
            code="PRIVATE_WORLD_CANDIDATE_CREATED_AT_INVALID",
        )
        _timestamp(
            self.expires_at,
            code="PRIVATE_WORLD_CANDIDATE_EXPIRES_AT_INVALID",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "candidate_type": self.candidate_type,
            "summary": self.summary,
            "confidence": round(self.confidence, 6),
            "source_letter_id": self.source_letter_id,
            "source_reply_revision": self.source_reply_revision,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class CandidateDecisionRequest:
    candidate_id: str
    decision: str
    request_id: str
    reason: str
    decided_at: str

    def __post_init__(self) -> None:
        _identifier(
            self.candidate_id,
            code="PRIVATE_WORLD_CANDIDATE_ID_INVALID",
        )
        if self.decision not in _DECISIONS:
            raise CandidateAPIError(
                "PRIVATE_WORLD_CANDIDATE_DECISION_INVALID"
            )
        if (
            not isinstance(self.request_id, str)
            or not _REQUEST_ID_RE.fullmatch(self.request_id)
        ):
            raise CandidateAPIError("CONTROL_REQUEST_ID_INVALID")
        object.__setattr__(
            self,
            "reason",
            _text(
                self.reason,
                code=(
                    "PRIVATE_WORLD_CANDIDATE_DECISION_REASON_INVALID"
                ),
            ),
        )
        _timestamp(
            self.decided_at,
            code="PRIVATE_WORLD_CANDIDATE_DECIDED_AT_INVALID",
        )


@dataclass(frozen=True)
class CandidateDecisionResult:
    candidate_id: str
    decision: str
    status: str
    reason_code: str

    def __post_init__(self) -> None:
        _identifier(
            self.candidate_id,
            code="PRIVATE_WORLD_CANDIDATE_ID_INVALID",
        )
        if self.decision not in _DECISIONS:
            raise CandidateAPIError(
                "PRIVATE_WORLD_CANDIDATE_DECISION_INVALID"
            )
        if self.status not in {
            "approved",
            "rejected",
            "duplicate",
            "expired",
        }:
            raise CandidateAPIError(
                "PRIVATE_WORLD_CANDIDATE_STATUS_INVALID"
            )
        if (
            not isinstance(self.reason_code, str)
            or not re.fullmatch(r"^[A-Z][A-Z0-9_]{0,95}$", self.reason_code)
        ):
            raise CandidateAPIError(
                "PRIVATE_WORLD_CANDIDATE_REASON_CODE_INVALID"
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": PRIVATE_WORLD_CANDIDATE_CONTROL_SCHEMA,
            "candidate_id": self.candidate_id,
            "decision": self.decision,
            "status": self.status,
            "reason_code": self.reason_code,
        }


@runtime_checkable
class CandidateReviewBackend(Protocol):
    def pending(self, *, limit: int) -> Sequence[CandidateSummary]: ...

    def decide(
        self,
        request: CandidateDecisionRequest,
    ) -> CandidateDecisionResult: ...


CANDIDATE_REVIEW_KEY = web.AppKey(
    "control_center.private_world_candidate_review",
    CandidateReviewBackend,
)


def _identifier(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise CandidateAPIError(code)
    return value


def _text(value: object, *, code: str) -> str:
    if not isinstance(value, str):
        raise CandidateAPIError(code)
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_TEXT_LENGTH
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in normalized
        )
    ):
        raise CandidateAPIError(code)
    return normalized


def _timestamp(value: object, *, code: str) -> str:
    if not isinstance(value, str):
        raise CandidateAPIError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CandidateAPIError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CandidateAPIError(code)
    return value


def _response(data: dict[str, object]) -> web.Response:
    return web.json_response({"ok": True, "data": data})


def _strict_body(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CandidateAPIError("CONTROL_BODY_INVALID")
    required = frozenset({"request_id", "reason", "decided_at"})
    if frozenset(value) != required or any(
        not isinstance(key, str) for key in value
    ):
        raise CandidateAPIError("CONTROL_BODY_FIELDS_INVALID")
    return value


async def _json_body(request: web.Request) -> object:
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    return await request.json(loads=json.loads)


def _backend(request: web.Request) -> CandidateReviewBackend:
    backend = request.app.get(CANDIDATE_REVIEW_KEY)
    if not isinstance(backend, CandidateReviewBackend):
        raise CandidateAPIError(
            "PRIVATE_WORLD_CANDIDATE_CONTROL_UNAVAILABLE",
            http_status=503,
        )
    return backend


async def pending_candidates(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", "50"))
    except ValueError as exc:
        raise CandidateAPIError("CONTROL_LIMIT_INVALID") from exc
    if not 1 <= limit <= 200:
        raise CandidateAPIError("CONTROL_LIMIT_INVALID")
    try:
        candidates = tuple(_backend(request).pending(limit=limit))
    except CandidateAPIError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        raise CandidateAPIError(
            "PRIVATE_WORLD_CANDIDATE_CONTROL_UNAVAILABLE",
            http_status=503,
        ) from exc
    if len(candidates) > limit or any(
        not isinstance(candidate, CandidateSummary)
        for candidate in candidates
    ):
        raise CandidateAPIError(
            "PRIVATE_WORLD_CANDIDATE_CONTROL_INVALID",
            http_status=503,
        )
    return _response(
        {
            "schema_version": PRIVATE_WORLD_CANDIDATE_CONTROL_SCHEMA,
            "status": "READY",
            "candidates": [candidate.to_dict() for candidate in candidates],
        }
    )


async def decide_candidate(request: web.Request) -> web.Response:
    candidate_id = _identifier(
        request.match_info.get("candidate_id", ""),
        code="PRIVATE_WORLD_CANDIDATE_ID_INVALID",
    )
    decision = request.match_info.get("decision", "")
    if decision not in _DECISIONS:
        raise CandidateAPIError(
            "PRIVATE_WORLD_CANDIDATE_DECISION_INVALID"
        )
    body = _strict_body(await _json_body(request))
    command = CandidateDecisionRequest(
        candidate_id=candidate_id,
        decision=decision,
        request_id=str(body["request_id"]),
        reason=str(body["reason"]),
        decided_at=str(body["decided_at"]),
    )
    try:
        result = _backend(request).decide(command)
    except CandidateAPIError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        raise CandidateAPIError(
            "PRIVATE_WORLD_CANDIDATE_CONTROL_UNAVAILABLE",
            http_status=503,
        ) from exc
    if not isinstance(result, CandidateDecisionResult):
        raise CandidateAPIError(
            "PRIVATE_WORLD_CANDIDATE_CONTROL_INVALID",
            http_status=503,
        )
    return _response(result.to_dict())


def mount_candidate_review_api(
    app: web.Application,
    backend: CandidateReviewBackend,
) -> None:
    if not isinstance(backend, CandidateReviewBackend):
        raise TypeError("a typed candidate review backend is required")
    if CANDIDATE_REVIEW_KEY in app:
        raise RuntimeError(
            "PRIVATE_WORLD_CANDIDATE_CONTROL_ALREADY_MOUNTED"
        )
    app[CANDIDATE_REVIEW_KEY] = backend
    app.add_routes(
        [
            web.get(
                "/control/api/private-world/candidates",
                pending_candidates,
            ),
            web.post(
                "/control/api/private-world/candidates/"
                "{candidate_id}/{decision}",
                decide_candidate,
            ),
        ]
    )


__all__ = [
    "CANDIDATE_REVIEW_KEY",
    "PRIVATE_WORLD_CANDIDATE_CONTROL_SCHEMA",
    "CandidateAPIError",
    "CandidateDecisionRequest",
    "CandidateDecisionResult",
    "CandidateReviewBackend",
    "CandidateSummary",
    "mount_candidate_review_api",
]
