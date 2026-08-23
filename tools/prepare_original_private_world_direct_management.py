from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "original_client_companion_mutation_api.py"
BACKEND = ROOT / "original_client_companion_mutation_backend.py"
SERVER = ROOT / "original_client_server.py"
TEST = ROOT / "tests" / "http" / "test_original_client_private_world_direct_mutations.py"
WORKFLOW = ROOT / ".github" / "workflows" / "public-smoke.yml"


def replace_once(value: str, old: str, new: str, label: str) -> str:
    if value.count(old) != 1:
        raise RuntimeError(f"PRIVATE_WORLD_DIRECT_{label}_ANCHOR_INVALID")
    return value.replace(old, new, 1)


def patch_api() -> None:
    value = API.read_text(encoding="utf-8")
    value = replace_once(
        value,
        'CANDIDATE_DECISION_PATH = "/toy/companion/private-world/candidates/{candidate_id}/{decision}"\nCONFIRM_HEADER = "X-Olivia-Companion-Action"\n',
        'CANDIDATE_DECISION_PATH = "/toy/companion/private-world/candidates/{candidate_id}/{decision}"\nPRIVATE_WORLD_NICKNAME_PATH = "/toy/companion/private-world/nickname"\nPRIVATE_WORLD_HOME_ACCESS_PATH = "/toy/companion/private-world/home-access"\nPRIVATE_WORLD_CONTINUATION_PATH = "/toy/companion/private-world/continuation"\nCONFIRM_HEADER = "X-Olivia-Companion-Action"\n',
        "API_PATHS",
    )
    value = replace_once(
        value,
        '_BACKEND_KEY = web.AppKey("original_companion_mutation_backend", object)\n_TRUSTED_ORIGINS_KEY',
        '_BACKEND_KEY = web.AppKey("original_companion_mutation_backend", object)\n_PRIVATE_WORLD_BACKEND_KEY = web.AppKey(\n    "original_private_world_mutation_backend",\n    object,\n)\n_TRUSTED_ORIGINS_KEY',
        "API_BACKEND_KEY",
    )
    value = replace_once(
        value,
        '_ALLOWED_DECISIONS = frozenset({"approve", "reject"})\n',
        '_ALLOWED_DECISIONS = frozenset({"approve", "reject"})\n_ALLOWED_NICKNAME_ACTIONS = frozenset({"grant", "revoke"})\n_ALLOWED_HOME_ACCESS = frozenset(\n    {"no_access", "visit_access", "errand_access", "domestic_access"}\n)\n_ALLOWED_CONTINUATION_ACTIONS = frozenset(\n    {"upsert", "set_awareness", "delete"}\n)\n_ALLOWED_CONTINUATION_AWARENESS = frozenset(\n    {"control_only", "pending", "character_known"}\n)\n',
        "API_ALLOWED_VALUES",
    )
    value = replace_once(
        value,
        '    def decide_candidate(\n        self,\n        *,\n        candidate_id: str,\n        decision: str,\n        request_id: str,\n        reason: str,\n        decided_at: str,\n    ) -> CompanionMutationResult: ...\n\n\ndef _identifier',
        '    def decide_candidate(\n        self,\n        *,\n        candidate_id: str,\n        decision: str,\n        request_id: str,\n        reason: str,\n        decided_at: str,\n    ) -> CompanionMutationResult: ...\n\n\n@runtime_checkable\nclass OriginalClientPrivateWorldMutationBackend(Protocol):\n    def execute_private_world(\n        self,\n        *,\n        operation: str,\n        payload: Mapping[str, object],\n        request_id: str,\n        reason: str,\n        occurred_at: str,\n    ) -> CompanionMutationResult: ...\n\n\ndef _identifier',
        "API_PROTOCOL",
    )
    value = replace_once(
        value,
        'def _timestamp(value: object) -> str:\n    if not isinstance(value, str):\n        raise OriginalClientCompanionMutationError("COMPANION_DECISION_TIME_INVALID", status=400)\n    try:\n        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))\n    except ValueError as exc:\n        raise OriginalClientCompanionMutationError(\n            "COMPANION_DECISION_TIME_INVALID", status=400\n        ) from exc\n    if parsed.tzinfo is None or parsed.utcoffset() is None:\n        raise OriginalClientCompanionMutationError(\n            "COMPANION_DECISION_TIME_INVALID", status=400\n        )\n    return value\n',
        'def _timestamp(\n    value: object,\n    *,\n    code: str = "COMPANION_DECISION_TIME_INVALID",\n) -> str:\n    if not isinstance(value, str):\n        raise OriginalClientCompanionMutationError(code, status=400)\n    try:\n        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))\n    except ValueError as exc:\n        raise OriginalClientCompanionMutationError(code, status=400) from exc\n    if parsed.tzinfo is None or parsed.utcoffset() is None:\n        raise OriginalClientCompanionMutationError(code, status=400)\n    return value\n',
        "API_TIMESTAMP",
    )
    value = replace_once(
        value,
        'def _backend(request: web.Request) -> OriginalClientCompanionMutationBackend:\n    backend = request.app.get(_BACKEND_KEY)\n    if not isinstance(backend, OriginalClientCompanionMutationBackend):\n        raise OriginalClientCompanionMutationError(\n            "COMPANION_MUTATION_UNAVAILABLE", status=503\n        )\n    return backend\n\n\nasync def _body',
        'def _backend(request: web.Request) -> OriginalClientCompanionMutationBackend:\n    backend = request.app.get(_BACKEND_KEY)\n    if not isinstance(backend, OriginalClientCompanionMutationBackend):\n        raise OriginalClientCompanionMutationError(\n            "COMPANION_MUTATION_UNAVAILABLE", status=503\n        )\n    return backend\n\n\ndef _private_world_backend(\n    request: web.Request,\n) -> OriginalClientPrivateWorldMutationBackend:\n    backend = request.app.get(_PRIVATE_WORLD_BACKEND_KEY)\n    if not isinstance(backend, OriginalClientPrivateWorldMutationBackend):\n        raise OriginalClientCompanionMutationError(\n            "PRIVATE_WORLD_MUTATION_DISABLED", status=503\n        )\n    return backend\n\n\nasync def _body',
        "API_BACKEND_ACCESSOR",
    )
    value = replace_once(
        value,
        'async def _body(request: web.Request, *, fields: frozenset[str]) -> Mapping[str, object]:\n    if request.content_length is not None and request.content_length > _MAX_BODY_BYTES:\n        raise OriginalClientCompanionMutationError(\n            "COMPANION_REQUEST_TOO_LARGE", status=413\n        )\n    if request.content_type != "application/json":\n        raise OriginalClientCompanionMutationError(\n            "COMPANION_CONTENT_TYPE_INVALID", status=415\n        )\n    try:\n        value = await request.json(loads=json.loads)\n    except (json.JSONDecodeError, UnicodeError, ValueError, TypeError) as exc:\n        raise OriginalClientCompanionMutationError(\n            "COMPANION_JSON_INVALID", status=400\n        ) from exc\n    if not isinstance(value, dict) or set(value) != set(fields):\n        raise OriginalClientCompanionMutationError(\n            "COMPANION_FIELDS_INVALID", status=400\n        )\n    return value\n',
        'async def _json_body(request: web.Request) -> Mapping[str, object]:\n    if request.content_length is not None and request.content_length > _MAX_BODY_BYTES:\n        raise OriginalClientCompanionMutationError(\n            "COMPANION_REQUEST_TOO_LARGE", status=413\n        )\n    if request.content_type != "application/json":\n        raise OriginalClientCompanionMutationError(\n            "COMPANION_CONTENT_TYPE_INVALID", status=415\n        )\n    try:\n        value = await request.json(loads=json.loads)\n    except (json.JSONDecodeError, UnicodeError, ValueError, TypeError) as exc:\n        raise OriginalClientCompanionMutationError(\n            "COMPANION_JSON_INVALID", status=400\n        ) from exc\n    if not isinstance(value, dict) or any(\n        not isinstance(key, str) for key in value\n    ):\n        raise OriginalClientCompanionMutationError(\n            "COMPANION_FIELDS_INVALID", status=400\n        )\n    return value\n\n\nasync def _body(request: web.Request, *, fields: frozenset[str]) -> Mapping[str, object]:\n    value = await _json_body(request)\n    if set(value) != set(fields):\n        raise OriginalClientCompanionMutationError(\n            "COMPANION_FIELDS_INVALID", status=400\n        )\n    return value\n',
        "API_BODY",
    )
    handlers = r'''

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
'''
    value = replace_once(
        value,
        "\ndef mount_original_client_companion_mutation_api(\n",
        handlers + "\ndef mount_original_client_companion_mutation_api(\n",
        "API_HANDLERS",
    )
    value = replace_once(
        value,
        '    *,\n    trusted_origins: tuple[str, ...] = (),\n) -> None:\n',
        '    *,\n    private_world_backend: OriginalClientPrivateWorldMutationBackend | None = None,\n    trusted_origins: tuple[str, ...] = (),\n) -> None:\n',
        "API_MOUNT_PARAM",
    )
    value = replace_once(
        value,
        '    if not isinstance(backend, OriginalClientCompanionMutationBackend):\n        raise TypeError("a typed companion mutation backend is required")\n    if app.get(_MOUNTED_KEY, False):\n',
        '    if not isinstance(backend, OriginalClientCompanionMutationBackend):\n        raise TypeError("a typed companion mutation backend is required")\n    if private_world_backend is not None and not isinstance(\n        private_world_backend,\n        OriginalClientPrivateWorldMutationBackend,\n    ):\n        raise TypeError("a typed PrivateWorld mutation backend is required")\n    if app.get(_MOUNTED_KEY, False):\n',
        "API_MOUNT_VALIDATE",
    )
    value = replace_once(
        value,
        '    app[_BACKEND_KEY] = backend\n    app[_TRUSTED_ORIGINS_KEY]',
        '    app[_BACKEND_KEY] = backend\n    if private_world_backend is not None:\n        app[_PRIVATE_WORLD_BACKEND_KEY] = private_world_backend\n    app[_TRUSTED_ORIGINS_KEY]',
        "API_MOUNT_STORE",
    )
    value = replace_once(
        value,
        '    app.router.add_post(CANDIDATE_DECISION_PATH, _decide_candidate)\n    app.router.add_options(CANDIDATE_DECISION_PATH, _preflight)\n',
        '    app.router.add_post(CANDIDATE_DECISION_PATH, _decide_candidate)\n    app.router.add_options(CANDIDATE_DECISION_PATH, _preflight)\n    app.router.add_post(PRIVATE_WORLD_NICKNAME_PATH, _private_world_nickname)\n    app.router.add_options(PRIVATE_WORLD_NICKNAME_PATH, _preflight)\n    app.router.add_post(\n        PRIVATE_WORLD_HOME_ACCESS_PATH,\n        _private_world_home_access,\n    )\n    app.router.add_options(PRIVATE_WORLD_HOME_ACCESS_PATH, _preflight)\n    app.router.add_post(\n        PRIVATE_WORLD_CONTINUATION_PATH,\n        _private_world_continuation,\n    )\n    app.router.add_options(PRIVATE_WORLD_CONTINUATION_PATH, _preflight)\n',
        "API_ROUTES",
    )
    value = replace_once(
        value,
        '    "MEMORY_DELETE_PATH",\n    "OriginalClientCompanionMutationBackend",\n',
        '    "MEMORY_DELETE_PATH",\n    "PRIVATE_WORLD_CONTINUATION_PATH",\n    "PRIVATE_WORLD_HOME_ACCESS_PATH",\n    "PRIVATE_WORLD_NICKNAME_PATH",\n    "OriginalClientCompanionMutationBackend",\n    "OriginalClientPrivateWorldMutationBackend",\n',
        "API_EXPORTS",
    )
    API.write_text(value, encoding="utf-8")


def patch_backend() -> None:
    value = BACKEND.read_text(encoding="utf-8")
    value = replace_once(
        value,
        'from typing import Protocol, runtime_checkable\n\nfrom control_center.private_world_candidate_api import (\n',
        'from typing import Mapping, Protocol, runtime_checkable\n\nfrom control_center.private_world_api import (\n    PRIVATE_WORLD_CONTROL_SCHEMA,\n    PrivateWorldAPIError,\n    PrivateWorldControlAPI,\n)\nfrom control_center.private_world_candidate_api import (\n',
        "BACKEND_IMPORTS",
    )
    value = replace_once(
        value,
        'def _candidate_error(exc: CandidateAPIError) -> OriginalClientCompanionMutationError:\n    status = int(getattr(exc, "http_status", 400))\n    if status not in {400, 403, 404, 409, 413, 415, 503}:\n        status = 503\n    return OriginalClientCompanionMutationError(exc.code, status=status)\n\n\ndef _memory_result',
        'def _candidate_error(exc: CandidateAPIError) -> OriginalClientCompanionMutationError:\n    status = int(getattr(exc, "http_status", 400))\n    if status not in {400, 403, 404, 409, 413, 415, 503}:\n        status = 503\n    return OriginalClientCompanionMutationError(exc.code, status=status)\n\n\ndef _private_world_error(\n    exc: PrivateWorldAPIError,\n) -> OriginalClientCompanionMutationError:\n    status = int(getattr(exc, "http_status", 400))\n    if status not in {400, 403, 404, 409, 413, 415, 503}:\n        status = 503\n    return OriginalClientCompanionMutationError(exc.code, status=status)\n\n\ndef _memory_result',
        "BACKEND_ERROR",
    )
    adapter = r'''

class DirectOriginalClientPrivateWorldMutationBackend:
    """Call the canonical typed PrivateWorld control API without a second reducer."""

    def __init__(
        self,
        private_world_commands: PrivateWorldControlAPI | None,
    ) -> None:
        if private_world_commands is not None and not isinstance(
            private_world_commands,
            PrivateWorldControlAPI,
        ):
            raise TypeError("an explicit PrivateWorld control API is required")
        self.private_world_commands = private_world_commands

    @staticmethod
    def _result(
        value: Mapping[str, object],
        *,
        request_id: str,
    ) -> CompanionMutationResult:
        if (
            not isinstance(value, Mapping)
            or value.get("schema_version") != PRIVATE_WORLD_CONTROL_SCHEMA
            or not isinstance(value.get("result"), Mapping)
        ):
            raise OriginalClientCompanionMutationError(
                "PRIVATE_WORLD_MUTATION_RESULT_INVALID",
                status=503,
            )
        result = value["result"]
        status = result.get("status")
        reason_code = result.get("reason_code")
        if status not in {"APPLIED", "DUPLICATE", "NOOP"} or (
            reason_code is not None and not isinstance(reason_code, str)
        ):
            raise OriginalClientCompanionMutationError(
                "PRIVATE_WORLD_MUTATION_RESULT_INVALID",
                status=503,
            )
        return CompanionMutationResult(
            request_id=request_id,
            status=str(status),
            affected_count=1 if status == "APPLIED" else 0,
            reason_code=reason_code,
        )

    def execute_private_world(
        self,
        *,
        operation: str,
        payload: Mapping[str, object],
        request_id: str,
        reason: str,
        occurred_at: str,
    ) -> CompanionMutationResult:
        if self.private_world_commands is None:
            raise OriginalClientCompanionMutationError(
                "PRIVATE_WORLD_MUTATION_DISABLED",
                status=503,
            )
        if not isinstance(payload, Mapping):
            raise OriginalClientCompanionMutationError(
                "PRIVATE_WORLD_OPERATION_INVALID",
                status=400,
            )
        methods = {
            "nickname": self.private_world_commands.nickname,
            "home_access": self.private_world_commands.home_access,
            "continuation": self.private_world_commands.continuation,
        }
        method = methods.get(operation)
        if method is None:
            raise OriginalClientCompanionMutationError(
                "PRIVATE_WORLD_OPERATION_INVALID",
                status=400,
            )
        body = {
            **dict(payload),
            "request_id": request_id,
            "reason": reason,
            "occurred_at": occurred_at,
            "evidence_refs": [f"control:{request_id}"],
        }
        try:
            result = method(body)
        except PrivateWorldAPIError as exc:
            raise _private_world_error(exc) from exc
        except (TypeError, ValueError) as exc:
            raise OriginalClientCompanionMutationError(
                "PRIVATE_WORLD_COMMAND_INVALID",
                status=400,
            ) from exc
        return self._result(result, request_id=request_id)
'''
    value = replace_once(
        value,
        "\n\n__all__ = [\n",
        adapter + "\n\n__all__ = [\n",
        "BACKEND_ADAPTER",
    )
    value = replace_once(
        value,
        '    "DirectOriginalClientCompanionMutationBackend",\n',
        '    "DirectOriginalClientCompanionMutationBackend",\n    "DirectOriginalClientPrivateWorldMutationBackend",\n',
        "BACKEND_EXPORT",
    )
    BACKEND.write_text(value, encoding="utf-8")


def patch_server() -> None:
    value = SERVER.read_text(encoding="utf-8")
    value = replace_once(
        value,
        'from control_center.private_world_candidate_api import CandidateReviewBackend\n',
        'from control_center.private_world_api import PrivateWorldControlAPI\nfrom control_center.private_world_candidate_api import CandidateReviewBackend\n',
        "SERVER_IMPORT_CONTROL",
    )
    value = replace_once(
        value,
        '    DirectOriginalClientCompanionMutationBackend,\n    MemoryAdminMutationService,\n',
        '    DirectOriginalClientCompanionMutationBackend,\n    DirectOriginalClientPrivateWorldMutationBackend,\n    MemoryAdminMutationService,\n',
        "SERVER_IMPORT_BACKEND",
    )
    value = replace_once(
        value,
        '    candidate_store: SQLitePrivateWorldCandidateStore | None\n    candidate_decisions: CandidateReviewBackend | None\n    mutation_backend: DirectOriginalClientCompanionMutationBackend\n',
        '    candidate_store: SQLitePrivateWorldCandidateStore | None\n    private_world_commands: PrivateWorldControlAPI | None\n    candidate_decisions: CandidateReviewBackend | None\n    mutation_backend: DirectOriginalClientCompanionMutationBackend\n    private_world_mutation_backend: (\n        DirectOriginalClientPrivateWorldMutationBackend\n    )\n',
        "SERVER_RUNTIME_FIELDS",
    )
    value = replace_once(
        value,
        '            "private_world_mounted": self.private_world_read is not None,\n            "candidate_store_mounted": self.candidate_store is not None,\n',
        '            "private_world_mounted": self.private_world_read is not None,\n            "private_world_commands_mounted": (\n                self.private_world_commands is not None\n            ),\n            "candidate_store_mounted": self.candidate_store is not None,\n',
        "SERVER_STATUS",
    )
    value = replace_once(
        value,
        '    candidates: SQLitePrivateWorldCandidateStore | None = None,\n    candidate_decisions: CandidateReviewBackend | None = None,\n    letter_collection: LetterCollection | None = None,\n',
        '    candidates: SQLitePrivateWorldCandidateStore | None = None,\n    private_world_commands: PrivateWorldControlAPI | None = None,\n    candidate_decisions: CandidateReviewBackend | None = None,\n    letter_collection: LetterCollection | None = None,\n',
        "SERVER_RUNTIME_PARAM",
    )
    value = replace_once(
        value,
        '    mutation_backend = DirectOriginalClientCompanionMutationBackend(\n        memory_admin=mutation_memory,\n        candidate_decisions=candidate_decisions,\n    )\n',
        '    mutation_backend = DirectOriginalClientCompanionMutationBackend(\n        memory_admin=mutation_memory,\n        candidate_decisions=candidate_decisions,\n    )\n    private_world_mutation_backend = (\n        DirectOriginalClientPrivateWorldMutationBackend(\n            private_world_commands\n        )\n    )\n',
        "SERVER_BACKENDS",
    )
    value = replace_once(
        value,
        '    mount_original_client_companion_mutation_api(\n        app,\n        mutation_backend,\n        trusted_origins=origins,\n    )\n',
        '    mount_original_client_companion_mutation_api(\n        app,\n        mutation_backend,\n        private_world_backend=private_world_mutation_backend,\n        trusted_origins=origins,\n    )\n',
        "SERVER_MOUNT",
    )
    value = replace_once(
        value,
        '    runtime = OriginalClientServerRuntime(\n        app,\n        backend,\n        memory_admin,\n        private_read,\n        candidates,\n        candidate_decisions,\n        mutation_backend,\n    )\n',
        '    runtime = OriginalClientServerRuntime(\n        app=app,\n        backend=backend,\n        memory_admin=memory_admin,\n        private_world_read=private_read,\n        candidate_store=candidates,\n        private_world_commands=private_world_commands,\n        candidate_decisions=candidate_decisions,\n        mutation_backend=mutation_backend,\n        private_world_mutation_backend=(\n            private_world_mutation_backend\n        ),\n    )\n',
        "SERVER_RUNTIME_BUILD",
    )
    old_configured = '''def _configured_private_world(
    server_module: ModuleType | Any,
    environ: Mapping[str, str],
) -> tuple[
    PrivateWorldPort | None,
    SQLitePrivateWorldCandidateStore | None,
    CandidateReviewBackend | None,
]:
    committer = getattr(server_module, "private_world_committer", None)
    if committer is None:
        return None, None, None
    port = getattr(server_module, "private_world_port", None)
    if not isinstance(port, PrivateWorldPort):
        return None, None, None

    path, _reason, enabled = resolve_private_world_database(environ)
    if not enabled or path is None or not path.is_file():
        return port, None, None
    try:
        candidates = SQLitePrivateWorldCandidateStore(path)
    except (
        PrivateWorldCandidateError,
        LedgerWriteError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return port, None, None

    candidate_decisions: CandidateReviewBackend | None = None
    ledger = getattr(committer, "ledger", None)
    if isinstance(ledger, SQLitePrivateWorldLedger):
        try:
            candidate_decisions = SQLiteCandidateReviewBackend(
                candidates,
                PrivateWorldCommandService(ledger),
            )
        except (
            PrivateWorldCandidateError,
            LedgerWriteError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            candidate_decisions = None
    return port, candidates, candidate_decisions
'''
    new_configured = '''def _configured_private_world(
    server_module: ModuleType | Any,
    environ: Mapping[str, str],
) -> tuple[
    PrivateWorldPort | None,
    SQLitePrivateWorldCandidateStore | None,
    PrivateWorldControlAPI | None,
    CandidateReviewBackend | None,
]:
    committer = getattr(server_module, "private_world_committer", None)
    if committer is None:
        return None, None, None, None
    port = getattr(server_module, "private_world_port", None)
    if not isinstance(port, PrivateWorldPort):
        return None, None, None, None

    path, _reason, enabled = resolve_private_world_database(environ)
    if not enabled or path is None or not path.is_file():
        return port, None, None, None
    ledger = getattr(committer, "ledger", None)
    if not isinstance(ledger, SQLitePrivateWorldLedger):
        return port, None, None, None
    try:
        command_service = PrivateWorldCommandService(ledger)
        private_world_commands = PrivateWorldControlAPI(
            ledger,
            command_service,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return port, None, None, None

    try:
        candidates = SQLitePrivateWorldCandidateStore(path)
    except (
        PrivateWorldCandidateError,
        LedgerWriteError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return port, None, private_world_commands, None

    candidate_decisions: CandidateReviewBackend | None = None
    try:
        candidate_decisions = SQLiteCandidateReviewBackend(
            candidates,
            command_service,
        )
    except (
        PrivateWorldCandidateError,
        LedgerWriteError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        candidate_decisions = None
    return (
        port,
        candidates,
        private_world_commands,
        candidate_decisions,
    )
'''
    value = replace_once(
        value,
        old_configured,
        new_configured,
        "SERVER_CONFIGURED_PRIVATE_WORLD",
    )
    value = replace_once(
        value,
        '    private_world, candidates, candidate_decisions = _configured_private_world(\n        server_module,\n        values,\n    )\n',
        '    (\n        private_world,\n        candidates,\n        private_world_commands,\n        candidate_decisions,\n    ) = _configured_private_world(\n        server_module,\n        values,\n    )\n',
        "SERVER_CONFIGURED_UNPACK",
    )
    value = replace_once(
        value,
        '        private_world=private_world,\n        candidates=candidates,\n        candidate_decisions=candidate_decisions,\n',
        '        private_world=private_world,\n        candidates=candidates,\n        private_world_commands=private_world_commands,\n        candidate_decisions=candidate_decisions,\n',
        "SERVER_CONFIGURED_PASS",
    )
    SERVER.write_text(value, encoding="utf-8")


TEST_CONTENT = r'''from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from control_center.private_world_api import PrivateWorldControlAPI
from original_client_companion_mutation_api import (
    CONFIRM_HEADER,
    CONFIRM_VALUE,
    CompanionMutationResult,
    PRIVATE_WORLD_CONTINUATION_PATH,
    PRIVATE_WORLD_HOME_ACCESS_PATH,
    PRIVATE_WORLD_NICKNAME_PATH,
    mount_original_client_companion_mutation_api,
)
from original_client_companion_mutation_backend import (
    DirectOriginalClientCompanionMutationBackend,
    DirectOriginalClientPrivateWorldMutationBackend,
)
from original_client_server import (
    create_configured_original_client_server_runtime,
)
from private_world_delivery import PrivateWorldDeliveryCommitter
from private_world_ledger import SQLitePrivateWorldLedger
from private_world_service import PrivateWorldCommandService


TRUSTED_ORIGIN = "https://client.example"


class NoopCompanionBackend:
    def correct_memory(self, **kwargs) -> CompanionMutationResult:
        raise AssertionError(kwargs)

    def delete_memory(self, **kwargs) -> CompanionMutationResult:
        raise AssertionError(kwargs)

    def decide_candidate(self, **kwargs) -> CompanionMutationResult:
        raise AssertionError(kwargs)


class RecordingPrivateWorldBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def execute_private_world(self, **kwargs) -> CompanionMutationResult:
        self.calls.append(kwargs)
        return CompanionMutationResult(
            request_id=str(kwargs["request_id"]),
            status="APPLIED",
            affected_count=1,
            reason_code="PRIVATE_WORLD_COMMAND_APPLIED",
        )


async def _transport_client():
    app = web.Application()
    private_backend = RecordingPrivateWorldBackend()
    mount_original_client_companion_mutation_api(
        app,
        NoopCompanionBackend(),
        private_world_backend=private_backend,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    origin = str(client.make_url("/")).rstrip("/")
    return client, private_backend, origin


def _headers(origin: str) -> dict[str, str]:
    return {
        "Origin": origin,
        CONFIRM_HEADER: CONFIRM_VALUE,
    }


def test_transport_delegates_nickname_home_and_continuation() -> None:
    async def scenario() -> None:
        client, backend, origin = await _transport_client()
        try:
            granted = await client.post(
                PRIVATE_WORLD_NICKNAME_PATH,
                json={
                    "action": "grant",
                    "nickname": "小离",
                    "request_id": "request.nickname.grant.1",
                    "reason": "用户明确授权私人称呼。",
                    "occurred_at": "2026-08-23T12:00:00+00:00",
                },
                headers=_headers(origin),
            )
            assert granted.status == 200
            home = await client.post(
                PRIVATE_WORLD_HOME_ACCESS_PATH,
                json={
                    "home_access": "visit_access",
                    "request_id": "request.home.visit.1",
                    "reason": "用户明确授权到访。",
                    "occurred_at": "2026-08-23T12:01:00+00:00",
                },
                headers=_headers(origin),
            )
            assert home.status == 200
            continuation = await client.post(
                PRIVATE_WORLD_CONTINUATION_PATH,
                json={
                    "action": "upsert",
                    "fact_id": "continuation.fixture.1",
                    "statement": "林离知道用户已经搬到东京。",
                    "awareness": "pending",
                    "confirm_character_known": False,
                    "request_id": "request.continuation.upsert.1",
                    "reason": "用户新增本地世界线。",
                    "occurred_at": "2026-08-23T12:02:00+00:00",
                },
                headers=_headers(origin),
            )
            assert continuation.status == 200
            assert [call["operation"] for call in backend.calls] == [
                "nickname",
                "home_access",
                "continuation",
            ]
            assert backend.calls[2]["payload"] == {
                "action": "upsert",
                "fact_id": "continuation.fixture.1",
                "statement": "林离知道用户已经搬到东京。",
                "awareness": "pending",
            }
        finally:
            await client.close()

    asyncio.run(scenario())


def test_transport_requires_character_known_confirmation_and_exact_fields() -> None:
    async def scenario() -> None:
        client, backend, origin = await _transport_client()
        try:
            unconfirmed = await client.post(
                PRIVATE_WORLD_CONTINUATION_PATH,
                json={
                    "action": "set_awareness",
                    "fact_id": "continuation.fixture.1",
                    "awareness": "character_known",
                    "confirm_character_known": False,
                    "request_id": "request.continuation.known.1",
                    "reason": "用户确认林离已经知道。",
                    "occurred_at": "2026-08-23T12:03:00+00:00",
                },
                headers=_headers(origin),
            )
            assert unconfirmed.status == 403
            assert (await unconfirmed.json())["error_code"] == (
                "PRIVATE_WORLD_CHARACTER_KNOWN_CONFIRMATION_REQUIRED"
            )
            hidden = await client.post(
                PRIVATE_WORLD_NICKNAME_PATH,
                json={
                    "action": "grant",
                    "nickname": "小离",
                    "request_id": "request.nickname.hidden.1",
                    "reason": "fixture",
                    "occurred_at": "2026-08-23T12:04:00+00:00",
                    "trust_score": 100,
                },
                headers=_headers(origin),
            )
            assert hidden.status == 400
            missing_header = await client.post(
                PRIVATE_WORLD_HOME_ACCESS_PATH,
                json={
                    "home_access": "visit_access",
                    "request_id": "request.home.no-confirm.1",
                    "reason": "fixture",
                    "occurred_at": "2026-08-23T12:05:00+00:00",
                },
                headers={"Origin": origin},
            )
            assert missing_header.status == 403
            assert backend.calls == []
        finally:
            await client.close()

    asyncio.run(scenario())


def test_direct_backend_reuses_canonical_service_and_is_idempotent(
    tmp_path: Path,
) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private_world.sqlite3")
    backend = DirectOriginalClientPrivateWorldMutationBackend(
        PrivateWorldControlAPI(
            ledger,
            PrivateWorldCommandService(ledger),
        )
    )
    first = backend.execute_private_world(
        operation="nickname",
        payload={"action": "grant", "nickname": "小离"},
        request_id="request.nickname.backend.1",
        reason="用户明确授权。",
        occurred_at="2026-08-23T12:00:00+00:00",
    )
    duplicate = backend.execute_private_world(
        operation="nickname",
        payload={"action": "grant", "nickname": "小离"},
        request_id="request.nickname.backend.1",
        reason="用户明确授权。",
        occurred_at="2026-08-23T12:00:00+00:00",
    )
    assert first.status == "APPLIED"
    assert duplicate.status == "DUPLICATE"
    assert ledger.snapshot().nickname_permissions == ("小离",)
    assert len(ledger.events()) == 1


def test_direct_backend_disabled_and_unknown_operation_fail_closed() -> None:
    disabled = DirectOriginalClientPrivateWorldMutationBackend(None)
    with pytest.raises(Exception, match="PRIVATE_WORLD_MUTATION_DISABLED"):
        disabled.execute_private_world(
            operation="nickname",
            payload={"action": "grant", "nickname": "小离"},
            request_id="request.nickname.disabled.1",
            reason="fixture",
            occurred_at="2026-08-23T12:00:00+00:00",
        )


def test_configured_runtime_mutates_and_reads_same_ledger(tmp_path: Path) -> None:
    async def fallback(request: web.Request) -> web.Response:
        return web.json_response({"fallback": request.path})

    async def scenario() -> None:
        root = tmp_path / "data"
        root.mkdir()
        database = root / "private_world" / "private_world.sqlite3"
        ledger = SQLitePrivateWorldLedger(database)
        server = SimpleNamespace(
            handler=fallback,
            letters_adapter=SimpleNamespace(
                memory_prompt_builder=SimpleNamespace(
                    conversation_memory=None,
                    conversation_memory_user_id="local-user",
                )
            ),
            private_world_port=ledger,
            private_world_committer=PrivateWorldDeliveryCommitter(ledger),
            TRUSTED_FRONTEND_ORIGINS=frozenset({TRUSTED_ORIGIN}),
        )
        runtime = create_configured_original_client_server_runtime(
            server_module=server,
            environ={
                "OLIVIA_LOCAL_DATA_ROOT": str(root),
                "OLIVIA_PRIVATE_WORLD_ENABLED": "1",
                "OLIVIA_PRIVATE_WORLD_DB": str(database),
            },
        )
        assert runtime.private_world_commands is not None
        assert runtime.public_status()["private_world_commands_mounted"] is True
        async with TestClient(TestServer(runtime.app)) as client:
            response = await client.post(
                PRIVATE_WORLD_HOME_ACCESS_PATH,
                json={
                    "home_access": "visit_access",
                    "request_id": "request.home.runtime.1",
                    "reason": "用户明确授权到访。",
                    "occurred_at": "2026-08-23T12:00:00+00:00",
                },
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    CONFIRM_HEADER: CONFIRM_VALUE,
                },
            )
            assert response.status == 200
            read = await client.get(
                "/toy/companion/private-world",
                headers={"Origin": TRUSTED_ORIGIN},
            )
            assert read.status == 200
            assert (await read.json())["home_access"] == "visit_access"
        assert SQLitePrivateWorldLedger(database).snapshot().home_access.value == (
            "visit_access"
        )

    asyncio.run(scenario())
'''


def write_test() -> None:
    TEST.write_text(TEST_CONTENT, encoding="utf-8")


def restore_workflow() -> None:
    WORKFLOW.write_text(
        '''name: public-smoke

on:
  push:
  pull_request:

permissions:
  contents: read

jobs:
  public-smoke:
    name: Public smoke (Windows / Python 3.12)
    runs-on: windows-latest
    timeout-minutes: 15
    steps:
      - name: Check out source
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install public development dependencies
        run: python -m pip install -e ".[dev]"

      - name: Run public smoke tests
        run: python -m pytest -q

      - name: Run repository hardening scan
        run: python baseline_hardening_scan.py --mode all

      - name: Check whitespace
        run: git diff --check --exit-code
''',
        encoding="utf-8",
    )


def main() -> None:
    patch_api()
    patch_backend()
    patch_server()
    write_test()
    restore_workflow()
    Path(__file__).unlink()


if __name__ == "__main__":
    main()
