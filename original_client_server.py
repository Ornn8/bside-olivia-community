"""Run the original Olivia local backend with its in-client companion settings.

The original client remains the only user-facing shell.  This module mounts the
bounded Memory / PrivateWorld read and explicit-mutation APIs plus the original
Collection wire adapter before the existing catch-all toy API handler, then
launches every surface in one loopback-only aiohttp process.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import os
import platform
from pathlib import Path
import sys
import time
from types import ModuleType
from typing import Any

from aiohttp import web

import http_contract as contract
from control_center.private_world_candidate_api import CandidateReviewBackend
from control_center.private_world_candidate_backend import (
    SQLiteCandidateReviewBackend,
)
from conversation_memory_admin import (
    ConversationMemoryAdminError,
    ConversationMemoryAdminService,
)
from conversation_memory_port import ConversationMemoryPort
from mem0_capability_install import (
    Mem0CapabilityInstaller,
    create_mem0_capability_installer,
)
from mem0_embedding_install import Mem0EmbeddingInstaller
from mem0_memory import Mem0Config
from music_reply import video_reply_dependency_status
from runtime.media.background_video_readiness import BackgroundVideoReadiness
from original_client_companion_api import mount_original_companion_read_api
from original_client_companion_backend import (
    OriginalClientCompanionServiceBackend,
)
from original_client_companion_mutation_api import (
    mount_original_client_companion_mutation_api,
)
from original_client_companion_mutation_backend import (
    DirectOriginalClientCompanionMutationBackend,
    MemoryAdminMutationService,
)
from original_client_diagnostics_api import mount_original_client_diagnostics_api
from original_client_capability_api import mount_original_client_capability_api
from original_client_video_capability_api import mount_original_client_video_capability_api
from video_capability_install import (
    VideoCapabilityInstaller,
    load_video_manifest,
)
from original_client_setup_api import (
    LLMSetupService,
    mount_original_client_setup_api,
)
from original_client_update_api import (
    ComponentUpdater,
    LocalComponentUpdater,
    mount_original_client_update_api,
)
from original_client_letter_contract import (
    OriginalClientContractError,
    serialize_letter_detail,
    serialize_letter_list,
    serialize_letter_summary,
    serialize_unread_count,
)
from private_world_candidates import (
    PrivateWorldCandidateError,
    SQLitePrivateWorldCandidateStore,
)
from private_world_ledger import LedgerWriteError, SQLitePrivateWorldLedger
from private_world_port import PrivateWorldPort, PrivateWorldSnapshot
from runtime.memory.private_world_projection import project_private_world
from runtime.memory.private_world_runtime import resolve_private_world_database
from runtime.private_world.daily_life_runtime import DailyLifeRuntime
from runtime.media.media_paths import configured_media_path
from private_world_service import PrivateWorldCommandService


FallbackHandler = Callable[[web.Request], Awaitable[web.StreamResponse]]
LetterCollection = Callable[[str], Sequence[Mapping[str, object]]]
_RUNTIME_KEY = web.AppKey("original_client.server_runtime", object)
_MAILBOX_MOUNTED_KEY = web.AppKey("original_client.mailbox_wire_mounted", bool)
_MEMORY_ADMIN_FILENAME = "memory_admin_audit.sqlite3"
_DIAGNOSTIC_TAIL_LIMIT = 200
_DIAGNOSTIC_LOG_MAX_BYTES = 1 << 20
_DIAGNOSTIC_PROFILES = ("core", "llm", "memory", "asr")
_DIAGNOSTIC_CAPABILITIES = {
    "settings.video_reply": "settings_video_reply",
    "native.tts": "native_tts",
}


class OriginalClientServerError(RuntimeError):
    """Stable assembly failure without a path or provider payload."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PrivateWorldControlReadAdapter:
    """Project typed PrivateWorld state into the qualitative settings view."""

    def __init__(self, port: PrivateWorldPort) -> None:
        if not isinstance(port, PrivateWorldPort):
            raise TypeError("a PrivateWorld port is required")
        self._port = port

    def snapshot(self) -> Mapping[str, object]:
        try:
            snapshot = self._port.snapshot()
        except Exception as exc:
            raise OriginalClientServerError(
                "COMPANION_PRIVATE_WORLD_UNAVAILABLE"
            ) from exc
        if not isinstance(snapshot, PrivateWorldSnapshot):
            raise OriginalClientServerError(
                "COMPANION_PRIVATE_WORLD_INVALID"
            )
        projected = project_private_world(snapshot)
        behavior = projected.behavior.to_dict()
        return {
            "version": snapshot.version,
            "relationship_stage": behavior["relationship_stage"],
            "levels": {
                name: behavior[name]
                for name in (
                    "familiarity",
                    "trust",
                    "comfort",
                    "closeness",
                    "tension",
                )
            },
            "nickname_permissions": list(snapshot.nickname_permissions),
            "home_access": snapshot.home_access.value,
            "continuation_facts": [
                fact.to_dict() for fact in snapshot.continuation_facts
            ],
        }


def _response_payload(response: web.StreamResponse) -> dict[str, object] | None:
    if not isinstance(response, web.Response):
        return None
    body = response.body
    if isinstance(body, str):
        raw = body.encode("utf-8")
    elif isinstance(body, (bytes, bytearray, memoryview)):
        raw = bytes(body)
    else:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _replace_response_payload(
    response: web.Response,
    payload: Mapping[str, object],
    *,
    status: int | None = None,
) -> web.Response:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    response.headers.pop("Content-Length", None)
    response.body = encoded
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    if status is not None:
        response.set_status(status)
    return response


def _mailbox_failure(response: web.Response) -> web.Response:
    return _replace_response_payload(
        response,
        {
            "code": 503,
            "message": "ORIGINAL_CLIENT_MAILBOX_UNAVAILABLE",
            "data": {
                "status": "UNAVAILABLE",
                "error_code": "ORIGINAL_CLIENT_MAILBOX_UNAVAILABLE",
            },
        },
        status=503,
    )


def _find_letter(
    values: Sequence[Mapping[str, object]],
    letter_id: object,
) -> Mapping[str, object] | None:
    if not isinstance(letter_id, str) or not letter_id:
        return None
    for value in values:
        if value.get("letter_id", value.get("letterId")) == letter_id:
            return value
    return None


def _non_negative_int(value: object, *, default: int) -> int:
    return value if type(value) is int and value >= 0 else default


def _adapt_mailbox_payload(
    request: web.Request,
    payload: dict[str, object],
    letter_collection: LetterCollection,
) -> dict[str, object]:
    path = request.path.rstrip("/")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return payload
    scope = request.query.get("scope", "current")

    if path == "/toy/letter/list" and payload.get("code") == 0:
        letters = tuple(letter_collection(scope))
        remaining = _non_negative_int(
            data.get("remaining_today", data.get("remainingToday")),
            default=99 if scope == "current" else 0,
        )
        payload["data"] = serialize_letter_list(
            letters,
            remaining_today=remaining,
            scope=scope,
            include_legacy_aliases=True,
        )
        return payload

    if path == "/toy/letter/unread_count" and payload.get("code") == 0:
        letters = tuple(letter_collection(scope))
        unread = sum(1 for letter in letters if not letter.get("is_read", 1))
        payload["data"] = serialize_unread_count(
            unread,
            scope=scope,
            include_legacy_aliases=True,
        )
        return payload

    if path == "/toy/letter/detail" and payload.get("code") == 0:
        letter_id = data.get("letter_id", data.get("letterId"))
        letter = _find_letter(tuple(letter_collection(scope)), letter_id)
        if letter is None:
            return payload
        serialized = serialize_letter_detail(
            letter,
            scope=scope,
            include_legacy_aliases=True,
        )
        for field in ("error_code", "retryable"):
            if field in data:
                serialized[field] = data[field]
        payload["data"] = serialized
        return payload

    if path == "/toy/letter/send":
        letter_id = data.get("letter_id", data.get("letterId"))
        letter = _find_letter(tuple(letter_collection("current")), letter_id)
        if letter is None:
            return payload
        original = serialize_letter_summary(
            letter,
            include_legacy_aliases=True,
        )
        merged = dict(data)
        merged.update(original)
        payload["data"] = merged
    return payload


def mount_original_mailbox_wire_adapter(
    app: web.Application,
    fallback_handler: FallbackHandler,
    letter_collection: LetterCollection,
) -> None:
    """Expose the existing letter runtime through the audited Collection schema."""

    if not isinstance(app, web.Application):
        raise TypeError("an aiohttp application is required")
    if not callable(fallback_handler) or not callable(letter_collection):
        raise TypeError("mailbox fallback and letter collection are required")
    if app.get(_MAILBOX_MOUNTED_KEY, False):
        raise RuntimeError("ORIGINAL_CLIENT_MAILBOX_ALREADY_MOUNTED")

    async def adapted(request: web.Request) -> web.StreamResponse:
        canonical_path = contract.canonical_route_path(request.path)
        canonical_request = request
        if canonical_path != request.path:
            canonical_url = request.rel_url.with_path(canonical_path).with_query(
                request.rel_url.query
            )
            canonical_request = request.clone(rel_url=canonical_url)
        response = await fallback_handler(canonical_request)
        if not isinstance(response, web.Response):
            return response
        payload = _response_payload(response)
        if payload is None:
            return response
        try:
            adapted_payload = _adapt_mailbox_payload(
                canonical_request,
                payload,
                letter_collection,
            )
        except (OriginalClientContractError, OSError, RuntimeError, TypeError, ValueError):
            return _mailbox_failure(response)
        return _replace_response_payload(response, adapted_payload)

    app[_MAILBOX_MOUNTED_KEY] = True
    app.router.add_get("/toy/letter/list", adapted)
    app.router.add_get("/toy/letter/unread_count", adapted)
    app.router.add_get("/toy/letter/detail", adapted)
    app.router.add_post("/toy/letter/send", adapted)
    for native_path, toy_path in contract.NATIVE_LETTER_ROUTE_ALIASES.items():
        spec = contract.route_spec(toy_path)
        if spec is None:
            raise RuntimeError("ORIGINAL_CLIENT_MAILBOX_ALIAS_INVALID")
        for method in spec["methods"]:
            app.router.add_route(method, native_path, adapted)


@dataclass(frozen=True)
class OriginalClientServerRuntime:
    app: web.Application
    backend: OriginalClientCompanionServiceBackend
    memory_admin: ConversationMemoryAdminService | None
    private_world_read: PrivateWorldControlReadAdapter | None
    candidate_store: SQLitePrivateWorldCandidateStore | None
    candidate_decisions: CandidateReviewBackend | None
    mutation_backend: DirectOriginalClientCompanionMutationBackend
    capability_installer: Mem0CapabilityInstaller | None
    video_capability_installer: VideoCapabilityInstaller | None

    def public_status(self) -> dict[str, object]:
        """Return component presence only; never paths, content, or hidden scores."""

        return {
            "status": "available",
            "network_scope": "loopback",
            "original_client_only": True,
            "memory_admin_mounted": self.memory_admin is not None,
            "private_world_mounted": self.private_world_read is not None,
            "candidate_store_mounted": self.candidate_store is not None,
            "capability_installer_mounted": self.capability_installer is not None,
        }


def _launcher_tail(data_root: Path | None) -> tuple[Mapping[str, object], ...]:
    """Read only the bounded launcher event tail; raw content is never exported."""

    if data_root is None:
        return ()
    path = data_root / "logs" / "launcher.jsonl"
    if not path.exists():
        return ()
    if not path.is_file():
        raise RuntimeError("DIAGNOSTIC_LAUNCHER_TAIL_UNAVAILABLE")
    try:
        size = path.stat().st_size
        start = max(0, size - _DIAGNOSTIC_LOG_MAX_BYTES)
        with path.open("rb") as stream:
            stream.seek(start)
            raw = stream.read(_DIAGNOSTIC_LOG_MAX_BYTES)
        if start:
            newline = raw.find(b"\n")
            if newline < 0:
                raise RuntimeError("DIAGNOSTIC_LAUNCHER_TAIL_UNAVAILABLE")
            raw = raw[newline + 1 :]
        lines = raw.decode("utf-8").splitlines()[-_DIAGNOSTIC_TAIL_LIMIT:]
        records = tuple(json.loads(line) for line in lines)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("DIAGNOSTIC_LAUNCHER_TAIL_UNAVAILABLE") from exc
    if any(not isinstance(record, Mapping) for record in records):
        raise RuntimeError("DIAGNOSTIC_LAUNCHER_TAIL_UNAVAILABLE")
    return records


def _diagnostic_source(
    backend: OriginalClientCompanionServiceBackend,
    *,
    setup_service: LLMSetupService | None,
    launcher_tail_provider: Callable[[], Sequence[Mapping[str, object]]] | None,
    runtime_tail_provider: Callable[[], Sequence[Mapping[str, object]]] | None,
    health_profile_provider: Callable[[str], Mapping[str, object]] | None = None,
    task_snapshot_provider: Callable[[], Sequence[Mapping[str, object]]] | None = None,
) -> Callable[[], Mapping[str, object]]:
    """Bind only safe, aggregate collectors for a diagnostic export request."""

    def state(value: object) -> str:
        if not isinstance(value, str):
            return "unknown"
        normalized = value.strip().lower()
        aliases = {
            "healthy": "available",
            "ready": "available",
            "cancelled": "cancelled",
            "canceled": "cancelled",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized and normalized[0].isalpha() and all(
            character.islower() or character.isdigit() or character == "_"
            for character in normalized
        ):
            return normalized[:32]
        return "unknown"

    def code(value: object) -> str | None:
        if not isinstance(value, str) or not 1 <= len(value) <= 96:
            return None
        if not value[0].isalpha() or value != value.upper():
            return None
        if any(not (character.isupper() or character.isdigit() or character == "_") for character in value):
            return None
        return value

    def project_task(value: Mapping[str, object]) -> dict[str, object]:
        letter_state = state(value.get("letter_status"))
        media_state = state(value.get("media_status")) if "media_status" in value else None
        if media_state in {"pending", "queued", "processing"}:
            stage = "media_generation"
        else:
            stage = {
                "pending": "waiting",
                "processing": "reply_generation",
                "completed": "completed",
                "failed": "failed",
                "cancelled": "cancelled",
            }.get(letter_state, "unknown")
        created_at = value.get("created_at")
        elapsed_bucket = "unknown"
        if type(created_at) in {int, float}:
            elapsed = max(0.0, time.time() - float(created_at))
            if elapsed < 60:
                elapsed_bucket = "under_1m"
            elif elapsed < 300:
                elapsed_bucket = "1m_5m"
            elif elapsed < 900:
                elapsed_bucket = "5m_15m"
            elif elapsed < 3_600:
                elapsed_bucket = "15m_1h"
            elif elapsed < 21_600:
                elapsed_bucket = "1h_6h"
            else:
                elapsed_bucket = "over_6h"
        item: dict[str, object] = {
            "status": letter_state,
            "stage": stage,
            "elapsed_bucket": elapsed_bucket,
        }
        error_code = code(value.get("error_code"))
        if error_code is not None:
            item["error_code"] = error_code
        if "media_status" in value:
            item["media_status"] = media_state
        media_error_code = code(value.get("media_error_code"))
        if media_error_code is not None:
            item["media_error_code"] = media_error_code
        reply_mode = value.get("reply_mode")
        if reply_mode in {
            "text",
            "video",
            "text_letter",
            "normal_video",
            "music_video",
            "live",
        }:
            item["reply_mode"] = reply_mode
        for name in ("retryable", "media_retryable"):
            flag = value.get(name)
            if type(flag) is bool:
                item[name] = flag
        return item

    def collect() -> Mapping[str, object]:
        status = backend.read_status().to_dict()
        capabilities = status.get("capabilities")
        if not isinstance(capabilities, Mapping):
            raise RuntimeError("DIAGNOSTIC_HEALTH_UNAVAILABLE")
        checks: dict[str, object] = {}
        for name in ("memory", "private_world", "candidates"):
            value = capabilities.get(name)
            if not isinstance(value, Mapping):
                raise RuntimeError("DIAGNOSTIC_HEALTH_UNAVAILABLE")
            check: dict[str, object] = {"state": value.get("state")}
            reason = value.get("reason_code")
            if reason is not None:
                check["error_code"] = reason
            checks[name] = check

        summary: dict[str, object] = {
            "status": "available",
            "python_version": platform.python_version(),
            "os_name": platform.system() or "unknown",
            "os_release": platform.release() or "unknown",
            "architecture": platform.machine() or "unknown",
        }
        if health_profile_provider is not None:
            core_data: Mapping[str, object] | None = None
            for profile_name in _DIAGNOSTIC_PROFILES:
                envelope = health_profile_provider(profile_name)
                if not isinstance(envelope, Mapping) or envelope.get("code") != 0:
                    raise RuntimeError("DIAGNOSTIC_HEALTH_UNAVAILABLE")
                data = envelope.get("data")
                if not isinstance(data, Mapping):
                    raise RuntimeError("DIAGNOSTIC_HEALTH_UNAVAILABLE")
                profile_state = state(data.get("status"))
                if profile_state == "failed":
                    profile_state = "unavailable"
                checks[f"profile_{profile_name}"] = {
                    "state": profile_state
                }
                if profile_name == "core":
                    core_data = data
            if core_data is not None:
                providers = core_data.get("providers", {})
                memory_provider = providers.get("memory", {}) if isinstance(providers, Mapping) else {}
                conversation = memory_provider.get("conversation", {}) if isinstance(memory_provider, Mapping) else {}
                worker = conversation.get("runtime", {}) if isinstance(conversation, Mapping) else {}
                if isinstance(worker, Mapping) and worker:
                    entry = {"state": state(worker.get("status"))}
                    for field in ("pending_count", "attempt_count", "terminal_count", "worker_running"):
                        if field in worker:
                            entry[field] = worker[field]
                    reason = code(worker.get("reason_code"))
                    if reason:
                        entry["error_code"] = reason
                    checks["memory_worker"] = entry
                for name in ("contract_version",):
                    value = core_data.get(name)
                    if isinstance(value, str):
                        summary[name] = value
                raw_capabilities = core_data.get("capabilities")
                if not isinstance(raw_capabilities, Mapping):
                    raise RuntimeError("DIAGNOSTIC_HEALTH_UNAVAILABLE")
                for public_name, diagnostic_name in _DIAGNOSTIC_CAPABILITIES.items():
                    value = raw_capabilities.get(public_name)
                    if not isinstance(value, Mapping):
                        raise RuntimeError("DIAGNOSTIC_HEALTH_UNAVAILABLE")
                    entry: dict[str, object] = {"state": state(value.get("status"))}
                    reason = code(value.get("reason_code"))
                    if reason is not None:
                        entry["error_code"] = reason
                    checks[diagnostic_name] = entry

        install: dict[str, object] = {
            "status": "available" if setup_service is not None else "disabled"
        }
        if setup_service is not None:
            setup = setup_service.status()
            llm = setup.get("llm")
            if not isinstance(llm, Mapping):
                raise RuntimeError("DIAGNOSTIC_INSTALL_UNAVAILABLE")
            setup_completed = setup.get("setup_completed")
            key_configured = llm.get("key_configured")
            if type(setup_completed) is not bool or type(key_configured) is not bool:
                raise RuntimeError("DIAGNOSTIC_INSTALL_UNAVAILABLE")
            install.update(
                setup_completed=setup_completed,
                key_configured=key_configured,
            )

        raw_tasks = tuple(task_snapshot_provider() if task_snapshot_provider else ())
        if len(raw_tasks) > 20 or any(not isinstance(item, Mapping) for item in raw_tasks):
            raise RuntimeError("DIAGNOSTIC_TASKS_UNAVAILABLE")
        task_items = [project_task(item) for item in raw_tasks]
        pending = sum(
            item.get("status") in {"pending", "processing"}
            or item.get("media_status") in {"pending", "queued", "processing"}
            for item in task_items
        )
        return {
            "summary": summary,
            "health": {"status": "available", "checks": checks},
            "install": install,
            "tasks": {
                "status": "active" if pending else "idle",
                "pending": pending,
                "items": task_items,
            },
            "launcher_tail": list(launcher_tail_provider() if launcher_tail_provider else ()),
            "runtime_tail": (
                list(runtime_tail_provider() if runtime_tail_provider else ())[-110:]
                + list(backend.diagnostic_status_history())
            ),
        }

    return collect


def create_original_client_server_runtime(
    fallback_handler: FallbackHandler,
    *,
    memory_admin: ConversationMemoryAdminService | None = None,
    private_world: PrivateWorldPort | None = None,
    daily_life: DailyLifeRuntime | None = None,
    candidates: SQLitePrivateWorldCandidateStore | None = None,
    candidate_decisions: CandidateReviewBackend | None = None,
    embedding_installer: Mem0EmbeddingInstaller | None = None,
    letter_collection: LetterCollection | None = None,
    setup_service: LLMSetupService | None = None,
    capability_installer: Mem0CapabilityInstaller | None = None,
    video_capability_installer: VideoCapabilityInstaller | None = None,
    component_updater: ComponentUpdater | None = None,
    trusted_origins: Sequence[str] = (),
    launcher_tail_provider: Callable[[], Sequence[Mapping[str, object]]] | None = None,
    runtime_tail_provider: Callable[[], Sequence[Mapping[str, object]]] | None = None,
    health_profile_provider: Callable[[str], Mapping[str, object]] | None = None,
    task_snapshot_provider: Callable[[], Sequence[Mapping[str, object]]] | None = None,
) -> OriginalClientServerRuntime:
    """Mount original-client adapters before the toy catch-all."""

    if not callable(fallback_handler):
        raise TypeError("a fallback request handler is required")
    private_read = (
        PrivateWorldControlReadAdapter(private_world)
        if private_world is not None
        else None
    )
    backend = OriginalClientCompanionServiceBackend(
        memory_admin=memory_admin,
        private_world=private_read,
        candidates=candidates,
        embedding_installer=embedding_installer,
    )
    mutation_memory = (
        memory_admin
        if isinstance(memory_admin, MemoryAdminMutationService)
        else None
    )
    mutation_backend = DirectOriginalClientCompanionMutationBackend(
        memory_admin=mutation_memory,
        candidate_decisions=candidate_decisions,
        embedding_installer=embedding_installer,
    )
    app = web.Application()
    origins = tuple(trusted_origins)
    observed_fallback = fallback_handler
    if setup_service is not None:
        async def observed_fallback(request: web.Request) -> web.StreamResponse:
            response = await fallback_handler(request)
            if request.path.rstrip("/") == "/toy/signIn":
                payload = _response_payload(response)
                code = payload.get("code") if payload else None
                setup_service.observe_login(
                    success=type(code) is int and code == 0
                )
            return response

        mount_original_client_setup_api(
            app,
            setup_service,
            trusted_origins=origins,
        )
        if capability_installer is not None:
            mount_original_client_capability_api(
                app,
                capability_installer,
                trusted_origins=origins,
                authorize_session=setup_service.require_session,
            )
        if video_capability_installer is not None:
            mount_original_client_video_capability_api(
                app,
                video_capability_installer,
                trusted_origins=origins,
                authorize_session=setup_service.require_session,
            )
        if component_updater is not None:
            mount_original_client_update_api(
                app,
                component_updater,
                trusted_origins=origins,
                authorize_session=setup_service.require_session,
            )
    mount_original_companion_read_api(
        app,
        backend,
        trusted_origins=origins,
        daily_life=daily_life,
    )
    mount_original_client_companion_mutation_api(
        app,
        mutation_backend,
        trusted_origins=origins,
    )
    mount_original_client_diagnostics_api(
        app,
        _diagnostic_source(
            backend,
            setup_service=setup_service,
            launcher_tail_provider=launcher_tail_provider,
            runtime_tail_provider=runtime_tail_provider,
            health_profile_provider=health_profile_provider,
            task_snapshot_provider=task_snapshot_provider,
        ),
        trusted_origins=origins,
    )
    if letter_collection is not None:
        mount_original_mailbox_wire_adapter(
            app,
            observed_fallback,
            letter_collection,
        )
    # Keep this last.  The original server intentionally owns a catch-all route.
    app.router.add_route("*", "/{tail:.*}", observed_fallback)
    runtime = OriginalClientServerRuntime(
        app,
        backend,
        memory_admin,
        private_read,
        candidates,
        candidate_decisions,
        mutation_backend,
        capability_installer,
        video_capability_installer,
    )
    app[_RUNTIME_KEY] = runtime
    return runtime


def _absolute_data_root(environ: Mapping[str, str]) -> Path | None:
    raw = str(environ.get("OLIVIA_LOCAL_DATA_ROOT", "")).strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute() or path.exists() and not path.is_dir():
        return None
    return path.resolve()


def _configured_capability_installer(
    environ: Mapping[str, str], data_root: Path | None,
) -> Mem0CapabilityInstaller | None:
    raw = str(environ.get("OLIVIA_INSTALL_ROOT", "")).strip()
    if data_root is None or not raw:
        return None
    patch_root = Path(raw).expanduser()
    if not patch_root.is_absolute() or not patch_root.is_dir():
        return None
    try:
        return create_mem0_capability_installer(
            install_root=patch_root.resolve().parent,
            data_root=data_root,
            python_executable=Path(sys.executable),
            backend_root=Path(__file__).resolve().parent,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _configured_video_capability_installer(
    environ: Mapping[str, str], data_root: Path | None,
) -> VideoCapabilityInstaller | None:
    if data_root is None:
        return None
    try:
        manifest = load_video_manifest(
            Path(__file__).resolve().parent / "installer" / "video-capability-manifest.json"
        )

        def readiness(environment: Mapping[str, str]) -> Mapping[str, object]:
            return video_reply_dependency_status(
                environment,
                performance_video_path=configured_media_path(
                    environment, "OLIVIA_MUSIC_PERFORMANCE_BASE"
                ),
            )

        artifact_roots = tuple(
            path.resolve()
            for raw in str(environ.get("OLIVIA_VIDEO_ARTIFACT_ROOTS", "")).split(
                os.pathsep
            )
            if (path := Path(raw.strip()).expanduser()).is_absolute()
            and path.is_dir()
        )
        runtime_search_roots = list(artifact_roots)
        install_root_raw = str(environ.get("OLIVIA_INSTALL_ROOT", "")).strip()
        if install_root_raw:
            install_root = Path(install_root_raw).expanduser()
            if install_root.is_absolute():
                for candidate in (
                    install_root / "downloads",
                    install_root.parent / "downloads",
                ):
                    runtime_search_roots.append(candidate.resolve())
        explicit_runtime = str(
            environ.get("OLIVIA_VIDEO_RUNTIME_ARCHIVE", "")
        ).strip()
        runtime_archives: list[Path] = []
        if explicit_runtime:
            candidate = Path(explicit_runtime).expanduser()
            if candidate.is_absolute() and candidate.is_file():
                runtime_archives.append(candidate.resolve())
        for root in runtime_search_roots:
            runtime_archives.extend(
                candidate.resolve()
                for candidate in root.glob("Olivia-video-runtime-*.zip")
                if candidate.is_file() and not candidate.is_symlink()
            )

        return VideoCapabilityInstaller(
            data_root=data_root,
            manifest=manifest,
            readiness_probe=BackgroundVideoReadiness(
                readiness,
                fingerprint_paths=(
                    Path(__file__).resolve().parent / "installer" / "video-capability-manifest.json",
                    data_root / "capabilities" / "video" / "runtime-environment.json",
                ),
            ),
            artifact_roots=artifact_roots,
            runtime_archives=tuple(dict.fromkeys(runtime_archives)),
            runtime_archive_roots=tuple(dict.fromkeys(runtime_search_roots)),
            runtime_environment_applier=os.environ.update,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _configured_component_updater(
    environ: Mapping[str, str],
) -> LocalComponentUpdater | None:
    raw = str(environ.get("OLIVIA_INSTALL_ROOT", "")).strip()
    if not raw:
        return None
    install_root = Path(raw).expanduser()
    if not install_root.is_absolute() or not install_root.is_dir():
        return None
    try:
        return LocalComponentUpdater(install_root.resolve())
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _configured_memory_admin(
    server_module: ModuleType | Any,
    environ: Mapping[str, str],
) -> ConversationMemoryAdminService | None:
    builder = getattr(
        getattr(server_module, "letters_adapter", None),
        "memory_prompt_builder",
        None,
    )
    memory = getattr(builder, "conversation_memory", None)
    if not isinstance(memory, ConversationMemoryPort):
        return None
    root = _absolute_data_root(environ)
    if root is None:
        config = getattr(memory, "config", None)
        configured_root = getattr(config, "outbox_data_root", None)
        configured_data = getattr(config, "data_root", None)
        if isinstance(configured_root, Path) and configured_root.is_absolute():
            root = configured_root
        elif isinstance(configured_data, Path) and configured_data.is_absolute():
            memory_root = (
                configured_data.parent
                if configured_data.name.casefold() == "mem0"
                else configured_data
            )
            root = (
                memory_root.parent
                if memory_root.name.casefold() == "memory"
                else memory_root
            )
    if root is None:
        return None
    user_id = getattr(builder, "conversation_memory_user_id", "local-user")
    try:
        return ConversationMemoryAdminService(
            memory,
            root / "memory" / _MEMORY_ADMIN_FILENAME,
            user_id=str(user_id),
        )
    except (
        ConversationMemoryAdminError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return None


def _configured_private_world(
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


def _configured_embedding_installer(
    server_module: ModuleType | Any,
) -> Mem0EmbeddingInstaller | None:
    builder = getattr(
        getattr(server_module, "letters_adapter", None),
        "memory_prompt_builder",
        None,
    )
    config = getattr(getattr(builder, "conversation_memory", None), "config", None)
    if not isinstance(config, Mem0Config) or not config.enabled:
        return None
    try:
        return Mem0EmbeddingInstaller(config)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def create_configured_original_client_server_runtime(
    *,
    server_module: ModuleType | Any | None = None,
    environ: Mapping[str, str] | None = None,
) -> OriginalClientServerRuntime:
    """Assemble configured services already owned by the local reply runtime."""

    if server_module is None:
        import local_server as server_module

    values = os.environ if environ is None else environ
    fallback = getattr(server_module, "handler", None)
    origins = tuple(
        getattr(server_module, "TRUSTED_FRONTEND_ORIGINS", ())
    )
    memory_admin = _configured_memory_admin(server_module, values)
    embedding_installer = _configured_embedding_installer(server_module)
    private_world, candidates, candidate_decisions = _configured_private_world(
        server_module,
        values,
    )
    collection = getattr(server_module, "_letter_collection", None)
    data_root = _absolute_data_root(values)
    apply_runtime = getattr(server_module, "apply_runtime_llm_config", None)
    setup_service = (
        LLMSetupService(
            data_root,
            apply_runtime=apply_runtime if callable(apply_runtime) else None,
        )
        if data_root is not None
        else None
    )
    capability_installer = _configured_capability_installer(values, data_root)
    video_capability_installer = _configured_video_capability_installer(values, data_root)
    component_updater = _configured_component_updater(values)
    runtime_tail = getattr(server_module, "runtime_diagnostic_event_snapshot", None)
    health_profile = getattr(server_module, "_health_result", None)

    def task_snapshot() -> tuple[Mapping[str, object], ...]:
        store = getattr(server_module, "store", None)
        letters = getattr(store, "letters", ())
        if not isinstance(letters, Sequence) or isinstance(
            letters, (str, bytes, bytearray)
        ):
            return ()
        return tuple(
            item
            for item in tuple(letters)[-20:]
            if isinstance(item, Mapping)
        )

    runtime = create_original_client_server_runtime(
        fallback,
        memory_admin=memory_admin,
        private_world=private_world,
        daily_life=getattr(server_module, "daily_life_runtime", None),
        candidates=candidates,
        candidate_decisions=candidate_decisions,
        embedding_installer=embedding_installer,
        letter_collection=collection if callable(collection) else None,
        setup_service=setup_service,
        capability_installer=capability_installer,
        video_capability_installer=video_capability_installer,
        component_updater=component_updater,
        trusted_origins=origins,
        launcher_tail_provider=lambda: _launcher_tail(data_root),
        runtime_tail_provider=runtime_tail if callable(runtime_tail) else None,
        health_profile_provider=health_profile if callable(health_profile) else None,
        task_snapshot_provider=task_snapshot,
    )
    install_reply_task_lifecycle = getattr(
        server_module,
        "install_reply_task_lifecycle",
        None,
    )
    if callable(install_reply_task_lifecycle):
        install_reply_task_lifecycle(runtime.app)
    return runtime


def main() -> int:
    """Launch one loopback process for the original client and companion APIs."""

    import local_server

    local_server.recover_pending_private_world()
    runtime = create_configured_original_client_server_runtime(
        server_module=local_server,
    )
    local_server._safe_log(
        "server_start",
        host="127.0.0.1",
        port=local_server.PORT,
        companion_settings=True,
    )
    web.run_app(
        runtime.app,
        host="127.0.0.1",
        port=local_server.PORT,
        access_log=None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "OriginalClientServerError",
    "OriginalClientServerRuntime",
    "PrivateWorldControlReadAdapter",
    "create_configured_original_client_server_runtime",
    "create_original_client_server_runtime",
    "main",
    "mount_original_mailbox_wire_adapter",
]
