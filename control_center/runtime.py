"""Construct the complete local Companion Control Center runtime.

The factory owns one PrivateWorld ledger, one typed command service, one
candidate store, one candidate-review backend, and one session manager.  It
never opens a socket or browser by itself; Windows launcher wiring remains a
separate concern.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import quote

from aiohttp import web

from private_world_candidates import SQLitePrivateWorldCandidateStore
from private_world_ledger import LedgerWriteError, SQLitePrivateWorldLedger
from private_world_runtime import resolve_private_world_database
from private_world_service import PrivateWorldCommandService

from .app import AUTH_KEY, create_control_app
from .auth import ControlSessionManager
from .private_world_candidate_backend import SQLiteCandidateReviewBackend
from .private_world_candidate_ui import mount_candidate_control


CONTROL_CENTER_RUNTIME_SCHEMA = "p03.control-center-runtime.v1"
CONTROL_CENTER_DEFAULT_PORT = 8900
CONTROL_CENTER_HOST = "127.0.0.1"


class ControlCenterRuntimeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ControlCenterRuntime:
    app: web.Application
    ledger: SQLitePrivateWorldLedger
    command_service: PrivateWorldCommandService
    candidate_store: SQLitePrivateWorldCandidateStore
    candidate_backend: SQLiteCandidateReviewBackend
    session_manager: ControlSessionManager

    def public_status(self) -> dict[str, object]:
        """Return path-free, content-free runtime status."""

        try:
            ledger = self.ledger.health()
            candidates = self.candidate_store.health()
            sessions = self.session_manager.status()
        except (OSError, RuntimeError, ValueError, LedgerWriteError) as exc:
            raise ControlCenterRuntimeError(
                "CONTROL_CENTER_STATUS_UNAVAILABLE"
            ) from exc
        return {
            "schema_version": CONTROL_CENTER_RUNTIME_SCHEMA,
            "status": "available",
            "network_scope": "loopback",
            "provider": "aiohttp-local",
            "private_world": {
                "schema_version": self.ledger.schema_version,
                "event_count": int(ledger["event_count"]),
                "snapshot_count": int(ledger["snapshot_count"]),
            },
            "candidates": {
                "schema_version": candidates["schema_version"],
                "pending": int(candidates["pending"]),
                "approved": int(candidates["approved"]),
                "rejected": int(candidates["rejected"]),
                "expired": int(candidates["expired"]),
                "decisions": int(candidates["decisions"]),
            },
            "sessions": {
                "active": int(sessions["active_sessions"]),
                "pending_bootstraps": int(sessions["pending_bootstraps"]),
            },
        }

    def issue_bootstrap_url(
        self,
        *,
        port: int = CONTROL_CENTER_DEFAULT_PORT,
    ) -> str:
        """Issue a one-use browser URL without placing the token in HTTP logs."""

        if type(port) is not int or not 1 <= port <= 65535:
            raise ControlCenterRuntimeError("CONTROL_CENTER_PORT_INVALID")
        token = self.session_manager.issue_bootstrap_token()
        fragment = quote(token, safe="")
        return (
            f"http://{CONTROL_CENTER_HOST}:{port}/control/"
            f"#bootstrap={fragment}"
        )


CONTROL_RUNTIME_KEY = web.AppKey(
    "control_center.runtime",
    ControlCenterRuntime,
)


def create_control_center_runtime(
    database_path: Path,
    *,
    session_manager: ControlSessionManager | None = None,
) -> ControlCenterRuntime:
    """Build one complete in-process runtime over an explicit SQLite file."""

    path = Path(database_path).expanduser()
    if not path.is_absolute() or path.exists() and path.is_dir():
        raise ControlCenterRuntimeError(
            "CONTROL_CENTER_DATABASE_INVALID"
        )
    manager = session_manager or ControlSessionManager()
    try:
        ledger = SQLitePrivateWorldLedger(path)
        service = PrivateWorldCommandService(ledger)
        candidate_store = SQLitePrivateWorldCandidateStore(path)
        candidate_backend = SQLiteCandidateReviewBackend(
            candidate_store,
            service,
        )
        app = create_control_app(
            ledger,
            service=service,
            session_manager=manager,
        )
        mount_candidate_control(app, candidate_backend)
        runtime = ControlCenterRuntime(
            app,
            ledger,
            service,
            candidate_store,
            candidate_backend,
            manager,
        )
        app[CONTROL_RUNTIME_KEY] = runtime
        return runtime
    except ControlCenterRuntimeError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError, LedgerWriteError) as exc:
        raise ControlCenterRuntimeError(
            "CONTROL_CENTER_INITIALIZATION_FAILED"
        ) from exc


def create_configured_control_center_runtime(
    environ: Mapping[str, str] | None = None,
    *,
    session_manager: ControlSessionManager | None = None,
) -> ControlCenterRuntime:
    """Resolve the installed PrivateWorld database and construct the runtime."""

    path, reason, enabled = resolve_private_world_database(environ)
    if not enabled:
        raise ControlCenterRuntimeError(
            reason or "CONTROL_CENTER_PRIVATE_WORLD_DISABLED"
        )
    if path is None:
        raise ControlCenterRuntimeError(
            reason or "CONTROL_CENTER_PRIVATE_WORLD_UNAVAILABLE"
        )
    return create_control_center_runtime(
        path,
        session_manager=session_manager,
    )


__all__ = [
    "CONTROL_CENTER_DEFAULT_PORT",
    "CONTROL_CENTER_HOST",
    "CONTROL_CENTER_RUNTIME_SCHEMA",
    "CONTROL_RUNTIME_KEY",
    "ControlCenterRuntime",
    "ControlCenterRuntimeError",
    "create_configured_control_center_runtime",
    "create_control_center_runtime",
]
