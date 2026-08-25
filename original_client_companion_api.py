"""Read-only companion data adapter for the original Olivia settings view.

The module mounts bounded GET endpoints on the existing loopback aiohttp app.
It contains transport and validation only: memory extraction, PrivateWorld
reduction, candidate decisions, and persistence remain owned by their services.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Mapping, Protocol, Sequence, runtime_checkable
from urllib.parse import urlsplit

from aiohttp import web


COMPANION_READ_SCHEMA = "p03.original-companion-read.v1"
STATUS_PATH = "/toy/companion/status"
MEMORY_PATH = "/toy/companion/memory"
PRIVATE_WORLD_PATH = "/toy/companion/private-world"
CANDIDATES_PATH = "/toy/companion/private-world/candidates"
_BACKEND_KEY = web.AppKey("original_companion_read_backend", object)
_TRUSTED_ORIGINS_KEY = web.AppKey("original_companion_trusted_origins", frozenset)
_MAX_QUERY_CHARS = 500
_MAX_MEMORY_LIMIT = 100
_MAX_CANDIDATE_LIMIT = 100
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")
_LOCAL_ORIGIN_RE = re.compile(r"^http://(?:127\.0\.0\.1|localhost):[0-9]{1,5}$")
_LEVELS = frozenset({"unknown", "low", "medium", "high"})
_CAPABILITY_STATES = frozenset({"available", "degraded", "unavailable", "disabled"})
_AWARENESS = frozenset({"control_only", "pending", "character_known"})
_HOME_ACCESS = frozenset({"no_access", "visit_access", "errand_access", "domestic_access"})
_CANDIDATE_TYPES = frozenset({"boundary_respected", "conflict", "repair"})
_RELATIONSHIP_FIELDS = (
    "familiarity",
    "trust",
    "comfort",
    "closeness",
    "tension",
)


class OriginalClientCompanionAPIError(RuntimeError):
    """Stable read API error without backend or filesystem detail."""

    def __init__(self, code: str, *, status: int) -> None:
        if not _CODE_RE.fullmatch(code):
            raise ValueError("companion API error code is invalid")
        self.code = code
        self.status = status
        super().__init__(code)


def _identifier(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(code)
    return value


def _text(value: object, *, maximum: int, code: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(code)
    normalized = value.strip()
    if (
        (not normalized and not allow_empty)
        or len(normalized) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ValueError(code)
    return normalized


def _timestamp(value: object, *, code: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(code)
    return value


@dataclass(frozen=True)
class CompanionCapability:
    state: str
    reason_code: str | None = None
    count: int | None = None

    def __post_init__(self) -> None:
        if self.state not in _CAPABILITY_STATES:
            raise ValueError("companion capability state is invalid")
        if self.reason_code is not None and not _CODE_RE.fullmatch(self.reason_code):
            raise ValueError("companion capability reason is invalid")
        if self.count is not None and (type(self.count) is not int or self.count < 0):
            raise ValueError("companion capability count is invalid")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"state": self.state}
        if self.reason_code is not None:
            payload["reason_code"] = self.reason_code
        if self.count is not None:
            payload["count"] = self.count
        return payload


@dataclass(frozen=True)
class CompanionVideoReplySetting:
    enabled: bool
    default_enabled: bool = True

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or type(self.default_enabled) is not bool:
            raise ValueError("video reply setting is invalid")

    def to_dict(self) -> dict[str, bool]:
        return {
            "enabled": self.enabled,
            "default_enabled": self.default_enabled,
        }


@dataclass(frozen=True)
class CompanionReadStatus:
    memory: CompanionCapability
    private_world: CompanionCapability
    candidates: CompanionCapability
    video_reply: CompanionVideoReplySetting | None = None

    def __post_init__(self) -> None:
        for value in (self.memory, self.private_world, self.candidates):
            if not isinstance(value, CompanionCapability):
                raise ValueError("companion read status is invalid")
        if self.video_reply is not None and not isinstance(
            self.video_reply,
            CompanionVideoReplySetting,
        ):
            raise ValueError("companion video reply setting is invalid")

    def to_dict(self) -> dict[str, object]:
        paused = self.memory.reason_code == "MEMORY_ADMIN_PAUSED"
        payload = {
            "schema_version": COMPANION_READ_SCHEMA,
            "status": "PAUSED" if paused else "READY",
            "capabilities": {
                "memory": self.memory.to_dict(),
                "private_world": self.private_world.to_dict(),
                "candidates": self.candidates.to_dict(),
            },
        }
        if self.video_reply is not None:
            payload["capabilities"]["video_reply"] = self.video_reply.to_dict()
        return payload


@dataclass(frozen=True)
class CompanionMemorySummary:
    memory_id: str
    text: str
    source_id: str
    created_at: str
    score: float | None = None

    def __post_init__(self) -> None:
        _identifier(self.memory_id, code="MEMORY_ID_INVALID")
        _text(self.text, maximum=2000, code="MEMORY_TEXT_INVALID")
        _identifier(self.source_id, code="MEMORY_SOURCE_INVALID")
        _timestamp(self.created_at, code="MEMORY_TIME_INVALID")
        if self.score is not None and (
            isinstance(self.score, bool)
            or not isinstance(self.score, (int, float))
            or not 0 <= float(self.score) <= 1
        ):
            raise ValueError("memory score is invalid")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "memory_id": self.memory_id,
            "text": self.text,
            "source_id": self.source_id,
            "created_at": self.created_at,
        }
        if self.score is not None:
            payload["score"] = round(float(self.score), 6)
        return payload


@dataclass(frozen=True)
class CompanionContinuationSummary:
    fact_id: str
    statement: str
    awareness: str

    def __post_init__(self) -> None:
        _identifier(self.fact_id, code="CONTINUATION_ID_INVALID")
        _text(self.statement, maximum=600, code="CONTINUATION_TEXT_INVALID")
        if self.awareness not in _AWARENESS:
            raise ValueError("continuation awareness is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "fact_id": self.fact_id,
            "statement": self.statement,
            "awareness": self.awareness,
        }


@dataclass(frozen=True)
class CompanionPrivateWorldSummary:
    version: int
    relationship_stage: str
    levels: Mapping[str, str]
    nickname_permissions: tuple[str, ...]
    home_access: str
    continuation_facts: tuple[CompanionContinuationSummary, ...] = ()

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version < 0:
            raise ValueError("private world version is invalid")
        _identifier(self.relationship_stage, code="RELATIONSHIP_STAGE_INVALID")
        if set(self.levels) != set(_RELATIONSHIP_FIELDS):
            raise ValueError("private world levels are invalid")
        if any(value not in _LEVELS for value in self.levels.values()):
            raise ValueError("private world level is invalid")
        if len(self.nickname_permissions) > 16:
            raise ValueError("nickname permissions are invalid")
        for nickname in self.nickname_permissions:
            _text(nickname, maximum=32, code="NICKNAME_INVALID")
        if self.home_access not in _HOME_ACCESS:
            raise ValueError("home access is invalid")
        if len(self.continuation_facts) > 32 or any(
            not isinstance(value, CompanionContinuationSummary)
            for value in self.continuation_facts
        ):
            raise ValueError("continuation facts are invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": COMPANION_READ_SCHEMA,
            "status": "READY",
            "version": self.version,
            "relationship_stage": self.relationship_stage,
            "levels": {name: self.levels[name] for name in _RELATIONSHIP_FIELDS},
            "nickname_permissions": list(self.nickname_permissions),
            "home_access": self.home_access,
            "continuation_facts": [value.to_dict() for value in self.continuation_facts],
        }


@dataclass(frozen=True)
class CompanionCandidateSummary:
    candidate_id: str
    candidate_type: str
    summary: str
    created_at: str
    expires_at: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, code="CANDIDATE_ID_INVALID")
        if self.candidate_type not in _CANDIDATE_TYPES:
            raise ValueError("candidate type is invalid")
        _text(self.summary, maximum=500, code="CANDIDATE_SUMMARY_INVALID")
        _timestamp(self.created_at, code="CANDIDATE_TIME_INVALID")
        _timestamp(
            self.expires_at,
            code="CANDIDATE_EXPIRY_INVALID",
            optional=True,
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "candidate_id": self.candidate_id,
            "candidate_type": self.candidate_type,
            "summary": self.summary,
            "created_at": self.created_at,
        }
        if self.expires_at is not None:
            payload["expires_at"] = self.expires_at
        return payload


@runtime_checkable
class OriginalClientCompanionReadBackend(Protocol):
    def read_status(self) -> CompanionReadStatus: ...

    def list_memories(
        self,
        *,
        query: str | None,
        limit: int,
    ) -> Sequence[CompanionMemorySummary]: ...

    def private_world_summary(self) -> CompanionPrivateWorldSummary: ...

    def list_candidates(self, *, limit: int) -> Sequence[CompanionCandidateSummary]: ...


def _host_is_loopback(request: web.Request) -> bool:
    try:
        host = urlsplit(f"//{request.host}").hostname
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


def _origin_allowed(request: web.Request, origin: str) -> bool:
    if origin in request.app.get(_TRUSTED_ORIGINS_KEY, frozenset()):
        return True
    if not _LOCAL_ORIGIN_RE.fullmatch(origin):
        return False
    try:
        port = urlsplit(origin).port
    except ValueError:
        return False
    return port is not None and 1 <= port <= 65535


def _headers(origin: str | None = None) -> dict[str, str]:
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if origin:
        headers.update(
            {
                "Access-Control-Allow-Origin": origin,
                "Vary": "Origin",
            }
        )
    return headers


def _error(code: str, status: int, *, origin: str | None = None) -> web.Response:
    return web.json_response(
        {
            "schema_version": COMPANION_READ_SCHEMA,
            "status": "UNAVAILABLE" if status == 503 else "FAILED",
            "error_code": code,
        },
        status=status,
        headers=_headers(origin),
    )


def _authorize(request: web.Request) -> str:
    if not _host_is_loopback(request):
        raise OriginalClientCompanionAPIError("COMPANION_HOST_FORBIDDEN", status=403)
    origin = request.headers.get("Origin", "")
    if not _origin_allowed(request, origin):
        raise OriginalClientCompanionAPIError("COMPANION_ORIGIN_FORBIDDEN", status=403)
    return origin


def _backend(request: web.Request) -> OriginalClientCompanionReadBackend:
    backend = request.app.get(_BACKEND_KEY)
    if not isinstance(backend, OriginalClientCompanionReadBackend):
        raise OriginalClientCompanionAPIError("COMPANION_READ_UNAVAILABLE", status=503)
    return backend


def _limit(request: web.Request, *, maximum: int) -> int:
    raw = request.query.get("limit", "50")
    try:
        value = int(raw)
    except ValueError as exc:
        raise OriginalClientCompanionAPIError("COMPANION_LIMIT_INVALID", status=400) from exc
    if not 1 <= value <= maximum:
        raise OriginalClientCompanionAPIError("COMPANION_LIMIT_INVALID", status=400)
    return value


async def _status(request: web.Request) -> web.Response:
    origin: str | None = None
    try:
        origin = _authorize(request)
        result = await asyncio.to_thread(_backend(request).read_status)
        if not isinstance(result, CompanionReadStatus):
            raise OriginalClientCompanionAPIError("COMPANION_READ_INVALID", status=503)
        return web.json_response(result.to_dict(), headers=_headers(origin))
    except OriginalClientCompanionAPIError as exc:
        return _error(exc.code, exc.status, origin=origin)
    except (OSError, RuntimeError, ValueError, TypeError):
        return _error("COMPANION_READ_UNAVAILABLE", 503, origin=origin)


async def _memory(request: web.Request) -> web.Response:
    origin: str | None = None
    try:
        origin = _authorize(request)
        limit = _limit(request, maximum=_MAX_MEMORY_LIMIT)
        query_value = request.query.get("query")
        query = None
        if query_value is not None:
            try:
                query = _text(
                    query_value,
                    maximum=_MAX_QUERY_CHARS,
                    code="COMPANION_QUERY_INVALID",
                    allow_empty=True,
                ) or None
            except ValueError as exc:
                raise OriginalClientCompanionAPIError(
                    "COMPANION_QUERY_INVALID", status=400
                ) from exc
        result = tuple(
            await asyncio.to_thread(
                _backend(request).list_memories,
                query=query,
                limit=limit,
            )
        )
        if len(result) > limit or any(
            not isinstance(value, CompanionMemorySummary) for value in result
        ):
            raise OriginalClientCompanionAPIError("COMPANION_READ_INVALID", status=503)
        return web.json_response(
            {
                "schema_version": COMPANION_READ_SCHEMA,
                "status": "READY",
                "memories": [value.to_dict() for value in result],
            },
            headers=_headers(origin),
        )
    except OriginalClientCompanionAPIError as exc:
        return _error(exc.code, exc.status, origin=origin)
    except (OSError, RuntimeError, ValueError, TypeError):
        return _error("COMPANION_READ_UNAVAILABLE", 503, origin=origin)


async def _private_world(request: web.Request) -> web.Response:
    origin: str | None = None
    try:
        origin = _authorize(request)
        result = await asyncio.to_thread(_backend(request).private_world_summary)
        if not isinstance(result, CompanionPrivateWorldSummary):
            raise OriginalClientCompanionAPIError("COMPANION_READ_INVALID", status=503)
        return web.json_response(result.to_dict(), headers=_headers(origin))
    except OriginalClientCompanionAPIError as exc:
        return _error(exc.code, exc.status, origin=origin)
    except (OSError, RuntimeError, ValueError, TypeError):
        return _error("COMPANION_READ_UNAVAILABLE", 503, origin=origin)


async def _candidates(request: web.Request) -> web.Response:
    origin: str | None = None
    try:
        origin = _authorize(request)
        limit = _limit(request, maximum=_MAX_CANDIDATE_LIMIT)
        result = tuple(
            await asyncio.to_thread(_backend(request).list_candidates, limit=limit)
        )
        if len(result) > limit or any(
            not isinstance(value, CompanionCandidateSummary) for value in result
        ):
            raise OriginalClientCompanionAPIError("COMPANION_READ_INVALID", status=503)
        return web.json_response(
            {
                "schema_version": COMPANION_READ_SCHEMA,
                "status": "READY",
                "candidates": [value.to_dict() for value in result],
            },
            headers=_headers(origin),
        )
    except OriginalClientCompanionAPIError as exc:
        return _error(exc.code, exc.status, origin=origin)
    except (OSError, RuntimeError, ValueError, TypeError):
        return _error("COMPANION_READ_UNAVAILABLE", 503, origin=origin)


def _trusted_origin(value: object) -> str:
    if not isinstance(value, str) or len(value) > 240:
        raise ValueError("trusted companion origin is invalid")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError("trusted companion origin is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("trusted companion origin is invalid")
    return f"https://{parsed.netloc}"


def mount_original_companion_read_api(
    app: web.Application,
    backend: OriginalClientCompanionReadBackend,
    *,
    trusted_origins: Sequence[str] = (),
) -> None:
    """Mount the read contract once on the existing local application."""

    if not isinstance(backend, OriginalClientCompanionReadBackend):
        raise TypeError("an original companion read backend is required")
    if len(trusted_origins) > 8:
        raise ValueError("too many trusted companion origins")
    origins = frozenset(_trusted_origin(value) for value in trusted_origins)
    if _BACKEND_KEY in app:
        raise RuntimeError("COMPANION_READ_ALREADY_MOUNTED")
    app[_BACKEND_KEY] = backend
    app[_TRUSTED_ORIGINS_KEY] = origins
    app.router.add_get(STATUS_PATH, _status)
    app.router.add_get(MEMORY_PATH, _memory)
    app.router.add_get(PRIVATE_WORLD_PATH, _private_world)
    app.router.add_get(CANDIDATES_PATH, _candidates)


__all__ = [
    "CANDIDATES_PATH",
    "COMPANION_READ_SCHEMA",
    "MEMORY_PATH",
    "PRIVATE_WORLD_PATH",
    "STATUS_PATH",
    "CompanionCandidateSummary",
    "CompanionCapability",
    "CompanionContinuationSummary",
    "CompanionMemorySummary",
    "CompanionPrivateWorldSummary",
    "CompanionReadStatus",
    "CompanionVideoReplySetting",
    "OriginalClientCompanionAPIError",
    "OriginalClientCompanionReadBackend",
    "mount_original_companion_read_api",
]
