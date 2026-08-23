"""Authenticated Control Center API for long-term conversation memory.

The shared Control Center middleware owns loopback, session, Origin, and CSRF
checks.  This adapter exposes bounded memory records and delegates all
mutations to the auditable provider-neutral admin service.  It never opens
Qdrant files or returns user scope, provider configuration, credentials, or
private-world state.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping, Protocol, Sequence, runtime_checkable

from aiohttp import web

from conversation_memory_admin import (
    ConversationMemoryAdminError,
    MemoryAdminMutationResult,
    MemoryAdminStatus,
)
from conversation_memory_port import ConversationMemoryRecord

from .private_world_api import PrivateWorldAPIError


MEMORY_CONTROL_SCHEMA = "p03.conversation-memory-control.v1"
MEMORY_ADMIN_KEY = web.AppKey(
    "control_center.conversation_memory_admin",
    object,
)
_MAX_LIST_LIMIT = 500


class MemoryAPIError(PrivateWorldAPIError):
    """Stable memory-control error handled by the shared middleware."""


@runtime_checkable
class MemoryControlBackend(Protocol):
    def list_memories(
        self,
        *,
        query: str | None = None,
        limit: int = 100,
    ) -> Sequence[ConversationMemoryRecord]: ...

    def add(
        self,
        text: str,
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

    def correct(
        self,
        memory_id: str,
        corrected_text: str,
        *,
        request_id: str,
        reason: str,
    ) -> MemoryAdminMutationResult: ...

    def clear(
        self,
        *,
        request_id: str,
        reason: str,
        confirmed: bool,
    ) -> MemoryAdminMutationResult: ...

    def export(self) -> Mapping[str, object]: ...

    def status(self) -> MemoryAdminStatus: ...


@dataclass(frozen=True)
class MemorySummary:
    memory_id: str
    text: str
    source_id: str
    domain: str
    score: float | None = None
    occurred_at: str | None = None
    created_at: str | None = None

    @classmethod
    def from_record(cls, record: ConversationMemoryRecord) -> "MemorySummary":
        if not isinstance(record, ConversationMemoryRecord):
            raise MemoryAPIError(
                "MEMORY_CONTROL_RECORD_INVALID",
                http_status=503,
            )
        return cls(
            memory_id=record.memory_id,
            text=record.text,
            source_id=record.source_id,
            domain=record.domain,
            score=record.score,
            occurred_at=(
                record.occurred_at.isoformat()
                if record.occurred_at is not None
                else None
            ),
            created_at=(
                record.created_at.isoformat()
                if record.created_at is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "memory_id": self.memory_id,
            "text": self.text,
            "source_id": self.source_id,
            "domain": self.domain,
        }
        if self.score is not None:
            payload["score"] = round(float(self.score), 6)
        if self.occurred_at is not None:
            payload["occurred_at"] = self.occurred_at
        if self.created_at is not None:
            payload["created_at"] = self.created_at
        return payload


def _response(data: Mapping[str, object]) -> web.Response:
    return web.json_response({"ok": True, "data": dict(data)})


async def _json_body(request: web.Request) -> object:
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    return await request.json(loads=json.loads)


def _strict_body(
    value: object,
    *,
    required: Sequence[str],
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise MemoryAPIError("CONTROL_BODY_INVALID")
    expected = frozenset(required)
    if frozenset(value) != expected or any(
        not isinstance(key, str) for key in value
    ):
        raise MemoryAPIError("CONTROL_BODY_FIELDS_INVALID")
    return value


def _backend(request: web.Request) -> MemoryControlBackend:
    backend = request.app.get(MEMORY_ADMIN_KEY)
    if not isinstance(backend, MemoryControlBackend):
        raise MemoryAPIError(
            "MEMORY_CONTROL_UNAVAILABLE",
            http_status=503,
        )
    return backend


def _limit(value: object, *, maximum: int = _MAX_LIST_LIMIT) -> int:
    if isinstance(value, bool):
        raise MemoryAPIError("CONTROL_LIMIT_INVALID")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise MemoryAPIError("CONTROL_LIMIT_INVALID") from exc
    if not 1 <= parsed <= maximum:
        raise MemoryAPIError("CONTROL_LIMIT_INVALID")
    return parsed


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ConversationMemoryAdminError as exc:
        raise MemoryAPIError(
            exc.code,
            http_status=_admin_http_status(exc.code),
        ) from exc
    except MemoryAPIError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        raise MemoryAPIError(
            "MEMORY_CONTROL_UNAVAILABLE",
            http_status=503,
        ) from exc


def _admin_http_status(code: str) -> int:
    if code in {
        "MEMORY_ADMIN_DISABLED",
        "MEMORY_ADMIN_UNAVAILABLE",
        "MEMORY_ADMIN_READ_FAILED",
        "MEMORY_ADMIN_ADD_FAILED",
        "MEMORY_ADMIN_DELETE_FAILED",
        "MEMORY_ADMIN_CORRECTION_WRITE_FAILED",
        "MEMORY_ADMIN_CORRECTION_DELETE_FAILED",
        "MEMORY_ADMIN_CLEAR_FAILED",
        "MEMORY_ADMIN_EXPORT_FAILED",
        "MEMORY_ADMIN_AUDIT_UNAVAILABLE",
    }:
        return 503
    if code in {
        "MEMORY_ADMIN_CONFIRMATION_REQUIRED",
        "MEMORY_ADMIN_REQUEST_CONFLICT",
    }:
        return 409
    return 400


async def memory_status(request: web.Request) -> web.Response:
    status = _call(_backend(request).status)
    if not isinstance(status, MemoryAdminStatus):
        raise MemoryAPIError(
            "MEMORY_CONTROL_STATUS_INVALID",
            http_status=503,
        )
    return _response(
        {
            "schema_version": MEMORY_CONTROL_SCHEMA,
            **status.to_dict(),
        }
    )


async def list_memories(request: web.Request) -> web.Response:
    limit = _limit(request.query.get("limit", "100"))
    records = tuple(
        _call(
            _backend(request).list_memories,
            query=None,
            limit=limit,
        )
    )
    if len(records) > limit:
        raise MemoryAPIError(
            "MEMORY_CONTROL_RECORDS_INVALID",
            http_status=503,
        )
    summaries = [MemorySummary.from_record(record).to_dict() for record in records]
    return _response(
        {
            "schema_version": MEMORY_CONTROL_SCHEMA,
            "status": "READY",
            "memories": summaries,
            "count": len(summaries),
        }
    )


async def search_memories(request: web.Request) -> web.Response:
    body = _strict_body(
        await _json_body(request),
        required=("query", "limit"),
    )
    query = body["query"]
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise MemoryAPIError("MEMORY_CONTROL_QUERY_INVALID")
    limit = _limit(body["limit"], maximum=100)
    records = tuple(
        _call(
            _backend(request).list_memories,
            query=query.strip(),
            limit=limit,
        )
    )
    if len(records) > limit:
        raise MemoryAPIError(
            "MEMORY_CONTROL_RECORDS_INVALID",
            http_status=503,
        )
    summaries = [MemorySummary.from_record(record).to_dict() for record in records]
    return _response(
        {
            "schema_version": MEMORY_CONTROL_SCHEMA,
            "status": "READY",
            "memories": summaries,
            "count": len(summaries),
        }
    )


async def add_memory(request: web.Request) -> web.Response:
    body = _strict_body(
        await _json_body(request),
        required=("request_id", "text", "reason"),
    )
    result = _call(
        _backend(request).add,
        body["text"],
        request_id=body["request_id"],
        reason=body["reason"],
    )
    return _mutation_response(result)


async def correct_memory(request: web.Request) -> web.Response:
    body = _strict_body(
        await _json_body(request),
        required=("request_id", "text", "reason"),
    )
    result = _call(
        _backend(request).correct,
        request.match_info.get("memory_id", ""),
        body["text"],
        request_id=body["request_id"],
        reason=body["reason"],
    )
    return _mutation_response(result)


async def delete_memory(request: web.Request) -> web.Response:
    body = _strict_body(
        await _json_body(request),
        required=("request_id", "reason"),
    )
    result = _call(
        _backend(request).delete,
        request.match_info.get("memory_id", ""),
        request_id=body["request_id"],
        reason=body["reason"],
    )
    return _mutation_response(result)


async def clear_memories(request: web.Request) -> web.Response:
    body = _strict_body(
        await _json_body(request),
        required=("request_id", "reason", "confirmed"),
    )
    if type(body["confirmed"]) is not bool:
        raise MemoryAPIError("MEMORY_CONTROL_CONFIRMATION_INVALID")
    result = _call(
        _backend(request).clear,
        request_id=body["request_id"],
        reason=body["reason"],
        confirmed=body["confirmed"],
    )
    return _mutation_response(result)


async def export_memories(request: web.Request) -> web.Response:
    del request
    payload = _call(_backend(request).export)
    if not isinstance(payload, Mapping):
        raise MemoryAPIError(
            "MEMORY_CONTROL_EXPORT_INVALID",
            http_status=503,
        )
    return _response(
        {
            "schema_version": MEMORY_CONTROL_SCHEMA,
            "status": "READY",
            "export": dict(payload),
        }
    )


def _mutation_response(result: object) -> web.Response:
    if not isinstance(result, MemoryAdminMutationResult):
        raise MemoryAPIError(
            "MEMORY_CONTROL_RESULT_INVALID",
            http_status=503,
        )
    return _response(
        {
            "schema_version": MEMORY_CONTROL_SCHEMA,
            **result.to_dict(),
        }
    )


def mount_memory_api(
    app: web.Application,
    backend: MemoryControlBackend,
) -> None:
    if not isinstance(backend, MemoryControlBackend):
        raise TypeError("a typed memory control backend is required")
    if MEMORY_ADMIN_KEY in app:
        raise RuntimeError("MEMORY_CONTROL_ALREADY_MOUNTED")
    app[MEMORY_ADMIN_KEY] = backend
    app.add_routes(
        [
            web.get("/control/api/memory/status", memory_status),
            web.get("/control/api/memory", list_memories),
            web.post("/control/api/memory/search", search_memories),
            web.post("/control/api/memory/manual", add_memory),
            web.post(
                "/control/api/memory/{memory_id}/correct",
                correct_memory,
            ),
            web.delete(
                "/control/api/memory/{memory_id}",
                delete_memory,
            ),
            web.post("/control/api/memory/clear", clear_memories),
            web.post("/control/api/memory/export", export_memories),
        ]
    )


__all__ = [
    "MEMORY_ADMIN_KEY",
    "MEMORY_CONTROL_SCHEMA",
    "MemoryAPIError",
    "MemoryControlBackend",
    "MemorySummary",
    "mount_memory_api",
]
