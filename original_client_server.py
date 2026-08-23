"""Run the original Olivia local backend with its in-client companion settings.

The original client remains the only user-facing shell.  This module mounts the
bounded Memory / PrivateWorld read and explicit-mutation APIs before the
existing catch-all toy API handler, then launches every surface in one
loopback-only aiohttp process.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
from types import ModuleType
from typing import Any

from aiohttp import web

from control_center.private_world_candidate_api import CandidateReviewBackend
from control_center.private_world_candidate_backend import (
    SQLiteCandidateReviewBackend,
)
from conversation_memory_admin import (
    ConversationMemoryAdminError,
    ConversationMemoryAdminService,
)
from conversation_memory_port import ConversationMemoryPort
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
from private_world_candidates import (
    PrivateWorldCandidateError,
    SQLitePrivateWorldCandidateStore,
)
from private_world_ledger import LedgerWriteError, SQLitePrivateWorldLedger
from private_world_port import PrivateWorldPort, PrivateWorldSnapshot
from private_world_projection import project_private_world
from private_world_runtime import resolve_private_world_database
from private_world_service import PrivateWorldCommandService


FallbackHandler = Callable[[web.Request], Awaitable[web.StreamResponse]]
_RUNTIME_KEY = web.AppKey("original_client.server_runtime", object)
_MEMORY_ADMIN_FILENAME = "memory_admin_audit.sqlite3"


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


@dataclass(frozen=True)
class OriginalClientServerRuntime:
    app: web.Application
    backend: OriginalClientCompanionServiceBackend
    memory_admin: ConversationMemoryAdminService | None
    private_world_read: PrivateWorldControlReadAdapter | None
    candidate_store: SQLitePrivateWorldCandidateStore | None
    candidate_decisions: CandidateReviewBackend | None
    mutation_backend: DirectOriginalClientCompanionMutationBackend

    def public_status(self) -> dict[str, object]:
        """Return component presence only; never paths, content, or hidden scores."""

        return {
            "status": "available",
            "network_scope": "loopback",
            "original_client_only": True,
            "memory_admin_mounted": self.memory_admin is not None,
            "private_world_mounted": self.private_world_read is not None,
            "candidate_store_mounted": self.candidate_store is not None,
        }


def create_original_client_server_runtime(
    fallback_handler: FallbackHandler,
    *,
    memory_admin: ConversationMemoryAdminService | None = None,
    private_world: PrivateWorldPort | None = None,
    candidates: SQLitePrivateWorldCandidateStore | None = None,
    candidate_decisions: CandidateReviewBackend | None = None,
    trusted_origins: Sequence[str] = (),
) -> OriginalClientServerRuntime:
    """Mount companion reads and explicit mutations before the toy catch-all."""

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
    )
    mutation_memory = (
        memory_admin
        if isinstance(memory_admin, MemoryAdminMutationService)
        else None
    )
    mutation_backend = DirectOriginalClientCompanionMutationBackend(
        memory_admin=mutation_memory,
        candidate_decisions=candidate_decisions,
    )
    app = web.Application()
    origins = tuple(trusted_origins)
    mount_original_companion_read_api(
        app,
        backend,
        trusted_origins=origins,
    )
    mount_original_client_companion_mutation_api(
        app,
        mutation_backend,
        trusted_origins=origins,
    )
    # Keep this last.  The original server intentionally owns a catch-all route.
    app.router.add_route("*", "/{tail:.*}", fallback_handler)
    runtime = OriginalClientServerRuntime(
        app,
        backend,
        memory_admin,
        private_read,
        candidates,
        candidate_decisions,
        mutation_backend,
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


def _configured_memory_admin(
    server_module: ModuleType | Any,
    environ: Mapping[str, str],
) -> ConversationMemoryAdminService | None:
    root = _absolute_data_root(environ)
    builder = getattr(
        getattr(server_module, "letters_adapter", None),
        "memory_prompt_builder",
        None,
    )
    memory = getattr(builder, "conversation_memory", None)
    if root is None or not isinstance(memory, ConversationMemoryPort):
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
    private_world, candidates, candidate_decisions = _configured_private_world(
        server_module,
        values,
    )
    return create_original_client_server_runtime(
        fallback,
        memory_admin=memory_admin,
        private_world=private_world,
        candidates=candidates,
        candidate_decisions=candidate_decisions,
        trusted_origins=origins,
    )


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
]
