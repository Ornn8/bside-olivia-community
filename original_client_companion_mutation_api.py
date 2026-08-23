"""Explicit-confirmation mutation contract for the original Olivia settings UI.

The original client is the sole user-facing shell.  This module only translates
strict loopback HTTP requests into an injected service backend.  It never reads
or writes Mem0, Qdrant, SQLite, PrivateWorld ledgers, or candidate rows itself.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Mapping, Protocol, runtime_checkable
from urllib.parse import urlsplit

from aiohttp import web


COMPANION_MUTATION_SCHEMA = "p03.original-companion-mutation.v1"
MEMORY_CORRECT_PATH = "/toy/companion/memory/correct"
MEMORY_DELETE_PATH = "/toy/companion/memory/delete"
CANDIDATE_DECISION_PATH = "/toy/companion/private-world/candidates/{candidate_id}/{decision}"
PRIVATE_WORLD_NICKNAME_PATH = "/toy/companion/private-world/nickname"
PRIVATE_WORLD_HOME_ACCESS_PATH = "/toy/companion/private-world/home-access"
PRIVATE_WORLD_CONTINUATION_PATH = "/toy/companion/private-world/continuation"
CONFIRM_HEADER = "X-Olivia-Companion-Action"
CONFIRM_VALUE = "confirmed"
_MAX_BODY_BYTES = 8_192
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,160}$")
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")
_LOOPBACK_ORIGIN_RE = re.compile(r"^http://(?:127\.0\.0\.1|localhost):[0-9]{1,5}$")
_BACKEND_KEY = web.AppKey("original_companion_mutation_backend", object)
_PRIVATE_WORLD_BACKEND_KEY = web.AppKey(
    "original_private_world_mutation_backend",
    object,
)
_TRUSTED_ORIGINS_KEY = web.AppKey("original_companion_mutation_origins", frozenset)
_MOUNTED_KEY = web.AppKey("original_companion_mutation_mounted", bool)
_ALLOWED_DECISIONS = frozenset({"approve", "reject"})
_ALLOWED_NICKNAME_ACTIONS = frozenset({"grant", "revoke"})
_ALLOWED_HOME_ACCESS = frozenset(
    {"no_access", "visit_access", "errand_access", "domestic_access"}
)
_ALLOWED_CONTINUATION_ACTIONS = frozenset(
    {"upsert", "set_awareness", "delete"}
)
_ALLOWED_CONTINUATION_AWARENESS = frozenset(
    {"control_only", "pending", "character_known"}
)


class OriginalClientCompanionMutationError(RuntimeError):
    """Stable, path-free transport or backend failure."""

    def __init__(self, code: str, *, status: int) -> None:
        if not _CODE_RE.fullmatch(code):
            raise ValueError("companion mutation error code is invalid")
        if status not in {400, 403, 404, 409, 413, 415, 503}:
            raise ValueError("companion mutation status is invalid")
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True)
class CompanionMutationResult:
    request_id: str
    status: str
    affected_count: int
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not _REQUEST_ID_RE.fullmatch(self.request_id):
            raise ValueError("mutation result request ID is invalid")
        if self.status not in {"APPLIED", "DUPLICATE", "NOOP", "REJECTED"}:
            raise ValueError("mutation result status is invalid")
        if type(self.affected_count) is not int or self.affected_count < 0:
            raise ValueError("mutation result affected count is invalid")
        if self.reason_code is not None and not _CODE_RE.fullmatch(self.reason_code):
            raise ValueError("mutation result reason code is invalid")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": COMPANION_MUTATION_SCHEMA,
            "status": self.status,
            "request_id": self.request_id,
            "affected_count": self.affected_count,
        }
        if self.reason_code is not None:
            value["reason_code"] = self.reason_code
        return value


@runtime_checkable
class OriginalClientCompanionMutationBackend(Protocol):
    def correct_memory(
        self,
        *,
        memory_id: str,
        replacement_text: str,
        request_id: str,
        reason: str,
    ) -> CompanionMutationResult: ...

    def delete_memory(
        self,
        *,
        memory_id: str,
        request_id: str,
        reason: str,
    ) -> CompanionMutationResult: ...

    def decide_candidate(
        self,
        *,
        candidate_id: str,
        decision: str,
        request_id: str,
        reason: str,
        decided_at: str,
    ) -> CompanionMutationResult: ...


@runtime_checkable
class OriginalClientPrivateWorldMutationBackend(Protocol):
    def execute_private_world(
        self,
        *,
        operation: str,
        payload: Mapping[str, object],
        request_id: str,
        reason: str,
        occurred_at: str,
    ) -> CompanionMutationResult: ...


def _identifier(value: object, *, code: str, request: bool = False) -> str:
    pattern = _REQUEST_ID_RE if request else _ID_RE
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise OriginalClientCompanionMutationError(code, status=400)
    return value


def _text(value: object, *, maximum: int, code: str) -> str:
    if not isinstance(value, str):
        raise OriginalClientCompanionMutationError(code, status=400)
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise OriginalClientCompanionMutationError(code, status=400)
    return normalized


def _timestamp(
    value: object,
    *,
    code: str = "COMPANION_DECISION_TIME_INVALID",
) -> str:
    if not isinstance(value, str):
        raise OriginalClientCompanionMutationError(code, status=400)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OriginalClientCompanionMutationError(code, status=400) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OriginalClientCompanionMutationError(code, status=400)
    return value


def _host_is_loopback(request: web.Request) -> bool:
    try:
        hostname = urlsplit(f"//{request.host}").hostname
    except ValueError:
        return False
    return hostname in {"127.0.0.1", "localhost", "::1"}


def _normalize_origins(values: tuple[str, ...]) -> frozenset[str]:
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValueError("trusted origins must be non-empty strings")
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise ValueError("trusted origin is invalid") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("trusted origin must be an HTTPS origin")
        normalized.add(value.rstrip("/"))
    if len(normalized) != len(values):
        raise ValueError("trusted origins must be unique")
    return frozenset(normalized)


def _loopback_origin_allowed(origin: str) -> bool:
    if not _LOOPBACK_ORIGIN_RE.fullmatch(origin):
        return False
    try:
        port = urlsplit(origin).port
    except ValueError:
        return False
    return port is not None and 1 <= port <= 65535


def _authorize(request: web.Request, *, require_confirm: bool) -> str:
    if not _host_is_loopback(request):
        raise OriginalClientCompanionMutationError(
            "COMPANION_HOST_FORBIDDEN", status=403
        )
    origin = request.headers.get("Origin", "").rstrip("/")
    trusted = request.app.get(_TRUSTED_ORIGINS_KEY, frozenset())
    if origin not in trusted and not _loopback_origin_allowed(origin):
        raise OriginalClientCompanionMutationError(
            "COMPANION_ORIGIN_FORBIDDEN", status=403
        )
    if require_confirm and request.headers.get(CONFIRM_HEADER) != CONFIRM_VALUE:
        raise OriginalClientCompanionMutationError(
            "COMPANION_CONFIRMATION_REQUIRED", status=403
        )
    return origin


def _headers(origin: str | None = None, *, preflight: bool = False) -> dict[str, str]:
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
    if preflight:
        headers.update(
            {
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": f"Content-Type, {CONFIRM_HEADER}",
                "Access-Control-Max-Age": "600",
            }
        )
    return headers


def _error(exc: OriginalClientCompanionMutationError, origin: str | None = None) -> web.Response:
    return web.json_response(
        {
            "schema_version": COMPANION_MUTATION_SCHEMA,
            "status": "UNAVAILABLE" if exc.status == 503 else "FAILED",
            "error_code": exc.code,
        },
        status=exc.status,
        headers=_headers(origin),
    )


def _backend(request: web.Request) -> OriginalClientCompanionMutationBackend:
    backend = request.app.get(_BACKEND_KEY)
    if not isinstance(backend, OriginalClientCompanionMutationBackend):
        raise OriginalClientCompanionMutationError(
            "COMPANION_MUTATION_UNAVAILABLE", status=503
        )
    return backend


def _private_world_backend(
    request: web.Request,
) -> OriginalClientPrivateWorldMutationBackend:
    backend = request.app.get(_PRIVATE_WORLD_BACKEND_KEY)
    if not isinstance(backend, OriginalClientPrivateWorldMutationBackend):
        raise OriginalClientCompanionMutationError(
            "PRIVATE_WORLD_MUTATION_DISABLED", status=503
        )
    return backend


async def _json_body(request: web.Request) -> Mapping[str, object]:
    if request.content_length is not None and request.content_length > _MAX_BODY_BYTES:
        raise OriginalClientCompanionMutationError(
            "COMPANION_REQUEST_TOO_LARGE", status=413
        )
    if request.content_type != "application/json":
        raise OriginalClientCompanionMutationError(
            "COMPANION_CONTENT_TYPE_INVALID", status=415
        )
    try:
        value = await request.json(loads=json.loads)
    except (json.JSONDecodeError, UnicodeError, ValueError, TypeError) as exc:
        raise OriginalClientCompanionMutationError(
            "COMPANION_JSON_INVALID", status=400
        ) from exc
    if not isinstance(value, dict) or any(
        not isinstance(key, str) for key in value
    ):
        raise OriginalClientCompanionMutationError(
            "COMPANION_FIELDS_INVALID", status=400
        )
    return value


async def _body(request: web.Request, *, fields: frozenset[str]) -> Mapping[str, object]:
    value = await _json_body(request)
    if set(value) != set(fields):
        raise OriginalClientCompanionMutationError(
            "COMPANION_FIELDS_INVALID", status=400
        )
    return value


async def _preflight(request: web.Request) -> web.Response:
    try:
        origin = _authorize(request, require_confirm=False)
        return web.Response(status=204, headers=_headers(origin, preflight=True))
    except OriginalClientCompanionMutationError as exc:
        return _error(exc)


async def _correct_memory(request: web.Request) -> web.Response:
    origin: str | None = None
    try:
        origin = _authorize(request, require_confirm=True)
        value = await _body(
            request,
            fields=frozenset({"memory_id", "replacement_text", "request_id", "reason"}),
        )
        result = await asyncio.to_thread(
            _backend(request).correct_memory,
            memory_id=_identifier(value["memory_id"], code="MEMORY_ID_INVALID"),
            replacement_text=_text(
                value["replacement_text"], maximum=2_000, code="MEMORY_TEXT_INVALID"
            ),
            request_id=_identifier(
                value["request_id"], code="COMPANION_REQUEST_ID_INVALID", request=True
            ),
            reason=_text(value["reason"], maximum=500, code="COMPANION_REASON_INVALID"),
        )
        if not isinstance(result, CompanionMutationResult):
            raise OriginalClientCompanionMutationError(
                "COMPANION_MUTATION_INVALID", status=503
            )
        return web.json_response(result.to_dict(), headers=_headers(origin))
    except OriginalClientCompanionMutationError as exc:
        return _error(exc, origin)
    except (OSError, RuntimeError, ValueError, TypeError):
        return _error(
            OriginalClientCompanionMutationError(
                "COMPANION_MUTATION_UNAVAILABLE", status=503
            ),
            origin,
        )


async def _delete_memory(request: web.Request) -> web.Response:
    origin: str | None = None
    try:
        origin = _authorize(request, require_confirm=True)
        value = await _body(
            request,
            fields=frozenset({"memory_id", "request_id", "reason"}),
        )
        result = await asyncio.to_thread(
            _backend(request).delete_memory,
            memory_id=_identifier(value["memory_id"], code="MEMORY_ID_INVALID"),
            request_id=_identifier(
                value["request_id"], code="COMPANION_REQUEST_ID_INVALID", request=True
            ),
            reason=_text(value["reason"], maximum=500, code="COMPANION_REASON_INVALID"),
        )
        if not isinstance(result, CompanionMutationResult):
            raise OriginalClientCompanionMutationError(
                "COMPANION_MUTATION_INVALID", status=503
            )
        return web.json_response(result.to_dict(), headers=_headers(origin))
    except OriginalClientCompanionMutationError as exc:
        return _error(exc, origin)
    except (OSError, RuntimeError, ValueError, TypeError):
        return _error(
            OriginalClientCompanionMutationError(
                "COMPANION_MUTATION_UNAVAILABLE", status=503
            ),
            origin,
        )


async def _decide_candidate(request: web.Request) -> web.Response:
    origin: str | None = None
    try:
        origin = _authorize(request, require_confirm=True)
        decision = request.match_info.get("decision", "")
        if decision not in _ALLOWED_DECISIONS:
            raise OriginalClientCompanionMutationError(
                "COMPANION_DECISION_INVALID", status=404
            )
        candidate_id = _identifier(
            request.match_info.get("candidate_id", ""), code="CANDIDATE_ID_INVALID"
        )
        value = await _body(
            request,
            fields=frozenset({"request_id", "reason", "decided_at"}),
        )
        result = await asyncio.to_thread(
            _backend(request).decide_candidate,
            candidate_id=candidate_id,
            decision=decision,
            request_id=_identifier(
                value["request_id"], code="COMPANION_REQUEST_ID_INVALID", request=True
            ),
            reason=_text(value["reason"], maximum=500, code="COMPANION_REASON_INVALID"),
            decided_at=_timestamp(value["decided_at"]),
        )
        if not isinstance(result, CompanionMutationResult):
            raise OriginalClientCompanionMutationError(
                "COMPANION_MUTATION_INVALID", status=503
            )
        return web.json_response(result.to_dict(), headers=_headers(origin))
    except OriginalClientCompanionMutationError as exc:
        return _error(exc, origin)
    except (OSError, RuntimeError, ValueError, TypeError):
        return _error(
            OriginalClientCompanionMutationError(
                "COMPANION_MUTATION_UNAVAILABLE", status=503
            ),
            origin,
        )



async def _private_world_nickname(request: web.Request) -> web.Response:
    origin: str | None = None
    try:
        origin = _authorize(request, require_confirm=True)
        value = await _body(
            request,
            fields=frozenset(
                {"action", "nickname", "request_id", "reason", "occurred_at"}
            ),
        )
        action = _text(
            value["action"],
            maximum=16,
            code="PRIVATE_WORLD_NICKNAME_ACTION_INVALID",
        )
        if action not in _ALLOWED_NICKNAME_ACTIONS:
            raise OriginalClientCompanionMutationError(
                "PRIVATE_WORLD_NICKNAME_ACTION_INVALID",
                status=400,
            )
        result = await asyncio.to_thread(
            _private_world_backend(request).execute_private_world,
            operation="nickname",
            payload={
                "action": action,
                "nickname": _text(
                    value["nickname"],
                    maximum=40,
                    code="PRIVATE_WORLD_NICKNAME_INVALID",
                ),
            },
            request_id=_identifier(
                value["request_id"],
                code="COMPANION_REQUEST_ID_INVALID",
                request=True,
            ),
            reason=_text(
                value["reason"],
                maximum=500,
                code="COMPANION_REASON_INVALID",
            ),
            occurred_at=_timestamp(
                value["occurred_at"],
                code="PRIVATE_WORLD_OCCURRED_AT_INVALID",
            ),
        )
        if not isinstance(result, CompanionMutationResult):
            raise OriginalClientCompanionMutationError(
                "COMPANION_MUTATION_INVALID",
                status=503,
            )
        return web.json_response(result.to_dict(), headers=_headers(origin))
    except OriginalClientCompanionMutationError as exc:
        return _error(exc, origin)
    except (OSError, RuntimeError, ValueError, TypeError):
        return _error(
            OriginalClientCompanionMutationError(
                "COMPANION_MUTATION_UNAVAILABLE",
                status=503,
            ),
            origin,
        )


async def _private_world_home_access(request: web.Request) -> web.Response:
    origin: str | None = None
    try:
        origin = _authorize(request, require_confirm=True)
        value = await _body(
            request,
            fields=frozenset(
                {"home_access", "request_id", "reason", "occurred_at"}
            ),
        )
        home_access = _text(
            value["home_access"],
            maximum=32,
            code="PRIVATE_WORLD_HOME_ACCESS_INVALID",
        )
        if home_access not in _ALLOWED_HOME_ACCESS:
            raise OriginalClientCompanionMutationError(
                "PRIVATE_WORLD_HOME_ACCESS_INVALID",
                status=400,
            )
        result = await asyncio.to_thread(
            _private_world_backend(request).execute_private_world,
            operation="home_access",
            payload={"home_access": home_access},
            request_id=_identifier(
                value["request_id"],
                code="COMPANION_REQUEST_ID_INVALID",
                request=True,
            ),
            reason=_text(
                value["reason"],
                maximum=500,
                code="COMPANION_REASON_INVALID",
            ),
            occurred_at=_timestamp(
                value["occurred_at"],
                code="PRIVATE_WORLD_OCCURRED_AT_INVALID",
            ),
        )
        if not isinstance(result, CompanionMutationResult):
            raise OriginalClientCompanionMutationError(
                "COMPANION_MUTATION_INVALID",
                status=503,
            )
        return web.json_response(result.to_dict(), headers=_headers(origin))
    except OriginalClientCompanionMutationError as exc:
        return _error(exc, origin)
    except (OSError, RuntimeError, ValueError, TypeError):
        return _error(
            OriginalClientCompanionMutationError(
                "COMPANION_MUTATION_UNAVAILABLE",
                status=503,
            ),
            origin,
        )


async def _private_world_continuation(request: web.Request) -> web.Response:
    origin: str | None = None
    try:
        origin = _authorize(request, require_confirm=True)
        value = await _json_body(request)
        action = _text(
            value.get("action"),
            maximum=32,
            code="PRIVATE_WORLD_CONTINUATION_ACTION_INVALID",
        )
        if action not in _ALLOWED_CONTINUATION_ACTIONS:
            raise OriginalClientCompanionMutationError(
                "PRIVATE_WORLD_CONTINUATION_ACTION_INVALID",
                status=400,
            )
        common_fields = {
            "action",
            "fact_id",
            "request_id",
            "reason",
            "occurred_at",
        }
        payload: dict[str, object] = {
            "action": action,
            "fact_id": _identifier(
                value.get("fact_id"),
                code="PRIVATE_WORLD_CONTINUATION_ID_INVALID",
            ),
        }
        if action == "upsert":
            expected = common_fields | {
                "statement",
                "awareness",
                "confirm_character_known",
            }
            if set(value) != expected:
                raise OriginalClientCompanionMutationError(
                    "COMPANION_FIELDS_INVALID",
                    status=400,
                )
            payload["statement"] = _text(
                value["statement"],
                maximum=2_000,
                code="PRIVATE_WORLD_CONTINUATION_STATEMENT_INVALID",
            )
        elif action == "set_awareness":
            expected = common_fields | {
                "awareness",
                "confirm_character_known",
            }
            if set(value) != expected:
                raise OriginalClientCompanionMutationError(
                    "COMPANION_FIELDS_INVALID",
                    status=400,
                )
        elif set(value) != common_fields:
            raise OriginalClientCompanionMutationError(
                "COMPANION_FIELDS_INVALID",
                status=400,
            )

        if action in {"upsert", "set_awareness"}:
            awareness = _text(
                value["awareness"],
                maximum=32,
                code="PRIVATE_WORLD_CONTINUATION_AWARENESS_INVALID",
            )
            if awareness not in _ALLOWED_CONTINUATION_AWARENESS:
                raise OriginalClientCompanionMutationError(
                    "PRIVATE_WORLD_CONTINUATION_AWARENESS_INVALID",
                    status=400,
                )
            confirmation = value["confirm_character_known"]
            if type(confirmation) is not bool:
                raise OriginalClientCompanionMutationError(
                    "PRIVATE_WORLD_CHARACTER_KNOWN_CONFIRMATION_INVALID",
                    status=400,
                )
            if awareness == "character_known" and confirmation is not True:
                raise OriginalClientCompanionMutationError(
                    "PRIVATE_WORLD_CHARACTER_KNOWN_CONFIRMATION_REQUIRED",
                    status=403,
                )
            payload["awareness"] = awareness

        result = await asyncio.to_thread(
            _private_world_backend(request).execute_private_world,
            operation="continuation",
            payload=payload,
            request_id=_identifier(
                value["request_id"],
                code="COMPANION_REQUEST_ID_INVALID",
                request=True,
            ),
            reason=_text(
                value["reason"],
                maximum=500,
                code="COMPANION_REASON_INVALID",
            ),
            occurred_at=_timestamp(
                value["occurred_at"],
                code="PRIVATE_WORLD_OCCURRED_AT_INVALID",
            ),
        )
        if not isinstance(result, CompanionMutationResult):
            raise OriginalClientCompanionMutationError(
                "COMPANION_MUTATION_INVALID",
                status=503,
            )
        return web.json_response(result.to_dict(), headers=_headers(origin))
    except OriginalClientCompanionMutationError as exc:
        return _error(exc, origin)
    except (OSError, RuntimeError, ValueError, TypeError):
        return _error(
            OriginalClientCompanionMutationError(
                "COMPANION_MUTATION_UNAVAILABLE",
                status=503,
            ),
            origin,
        )

def mount_original_client_companion_mutation_api(
    app: web.Application,
    backend: OriginalClientCompanionMutationBackend,
    *,
    private_world_backend: OriginalClientPrivateWorldMutationBackend | None = None,
    trusted_origins: tuple[str, ...] = (),
) -> None:
    """Mount the bounded mutation contract on an existing local application."""

    if not isinstance(app, web.Application):
        raise TypeError("an aiohttp application is required")
    if not isinstance(backend, OriginalClientCompanionMutationBackend):
        raise TypeError("a typed companion mutation backend is required")
    if private_world_backend is not None and not isinstance(
        private_world_backend,
        OriginalClientPrivateWorldMutationBackend,
    ):
        raise TypeError("a typed PrivateWorld mutation backend is required")
    if app.get(_MOUNTED_KEY, False):
        raise RuntimeError("ORIGINAL_COMPANION_MUTATION_ALREADY_MOUNTED")
    app[_BACKEND_KEY] = backend
    if private_world_backend is not None:
        app[_PRIVATE_WORLD_BACKEND_KEY] = private_world_backend
    app[_TRUSTED_ORIGINS_KEY] = _normalize_origins(tuple(trusted_origins))
    app[_MOUNTED_KEY] = True
    app.router.add_post(MEMORY_CORRECT_PATH, _correct_memory)
    app.router.add_options(MEMORY_CORRECT_PATH, _preflight)
    app.router.add_post(MEMORY_DELETE_PATH, _delete_memory)
    app.router.add_options(MEMORY_DELETE_PATH, _preflight)
    app.router.add_post(CANDIDATE_DECISION_PATH, _decide_candidate)
    app.router.add_options(CANDIDATE_DECISION_PATH, _preflight)
    app.router.add_post(PRIVATE_WORLD_NICKNAME_PATH, _private_world_nickname)
    app.router.add_options(PRIVATE_WORLD_NICKNAME_PATH, _preflight)
    app.router.add_post(
        PRIVATE_WORLD_HOME_ACCESS_PATH,
        _private_world_home_access,
    )
    app.router.add_options(PRIVATE_WORLD_HOME_ACCESS_PATH, _preflight)
    app.router.add_post(
        PRIVATE_WORLD_CONTINUATION_PATH,
        _private_world_continuation,
    )
    app.router.add_options(PRIVATE_WORLD_CONTINUATION_PATH, _preflight)


__all__ = [
    "CANDIDATE_DECISION_PATH",
    "COMPANION_MUTATION_SCHEMA",
    "CONFIRM_HEADER",
    "CONFIRM_VALUE",
    "CompanionMutationResult",
    "MEMORY_CORRECT_PATH",
    "MEMORY_DELETE_PATH",
    "PRIVATE_WORLD_CONTINUATION_PATH",
    "PRIVATE_WORLD_HOME_ACCESS_PATH",
    "PRIVATE_WORLD_NICKNAME_PATH",
    "OriginalClientCompanionMutationBackend",
    "OriginalClientPrivateWorldMutationBackend",
    "OriginalClientCompanionMutationError",
    "mount_original_client_companion_mutation_api",
]
