"""Default local PrivateWorld runtime construction and sanitized status."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import sqlite3
from typing import Mapping

from runtime.memory.conversation_memory_identity import (
    ConversationMemoryIdentityError,
    normalize_conversation_memory_user_id,
)
from runtime.memory.private_world_delivery import PrivateWorldDeliveryCommitter
from private_world_ledger import (
    LedgerWriteError,
    SQLitePrivateWorldLedger,
)
from private_world_port import NullPrivateWorldPort, PrivateWorldPort


PRIVATE_WORLD_DEFAULT_RELATIVE_PATH = Path(
    "private_world/private_world.sqlite3"
)
_DEFAULT_USER_ID = "local-user"


def _normalized_user_id(value: object) -> str:
    try:
        return normalize_conversation_memory_user_id(value)
    except ConversationMemoryIdentityError as exc:
        raise ValueError("private world user_id is invalid") from exc


def _user_database(path: Path, user_id: object) -> Path:
    """Keep non-default users in an opaque namespace below the state root."""

    normalized = _normalized_user_id(user_id)
    if normalized == _DEFAULT_USER_ID:
        return path
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return path.parent / "users" / digest / path.name


@dataclass(frozen=True)
class PrivateWorldRuntime:
    port: PrivateWorldPort
    committer: PrivateWorldDeliveryCommitter | None
    status: str
    provider: str
    reason_code: str | None
    enabled: bool
    schema_version: int | None = None
    migration_status: str | None = None
    event_count: int = 0
    snapshot_count: int = 0

    def public_status(self) -> dict[str, object]:
        """Return status without paths, scores, names, or continuation text."""

        event_count = self.event_count
        snapshot_count = self.snapshot_count
        status = self.status
        reason_code = self.reason_code
        if status == "available" and isinstance(
            self.port,
            SQLitePrivateWorldLedger,
        ):
            try:
                counts = self.port.health()
                self.port.snapshot()
                event_count = int(counts["event_count"])
                snapshot_count = int(counts["snapshot_count"])
            except (
                AttributeError,
                KeyError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                sqlite3.Error,
            ):
                status = "unavailable"
                reason_code = "PRIVATE_WORLD_STORAGE_UNAVAILABLE"
        return {
            "status": status,
            "provider": self.provider if status == "available" else "none",
            "reason_code": reason_code,
            "enabled": self.enabled,
            "schema_version": self.schema_version,
            "migration_status": self.migration_status,
            "event_count": event_count,
            "snapshot_count": snapshot_count,
            "probe": "in-process" if status == "available" else "not-run",
            "network_called": False,
        }


def _enabled(value: object, *, default: bool) -> bool | None:
    if value is None:
        return default
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def resolve_private_world_database(
    environ: Mapping[str, str] | None = None,
    *,
    user_id: str = _DEFAULT_USER_ID,
) -> tuple[Path | None, str | None, bool]:
    """Resolve an explicit DB or the data-root default without creating it."""

    values = os.environ if environ is None else environ
    enabled = _enabled(
        values.get("OLIVIA_PRIVATE_WORLD_ENABLED"),
        default=True,
    )
    if enabled is None:
        return None, "PRIVATE_WORLD_ENABLED_INVALID", False
    if not enabled:
        return None, "PRIVATE_WORLD_DISABLED", False

    explicit = str(values.get("OLIVIA_PRIVATE_WORLD_DB", "")).strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            return None, "PRIVATE_WORLD_DB_MUST_BE_ABSOLUTE", True
        return _user_database(path.resolve(), user_id), None, True

    root_value = str(values.get("OLIVIA_LOCAL_DATA_ROOT", "")).strip()
    if not root_value:
        return None, "PRIVATE_WORLD_DATA_ROOT_NOT_CONFIGURED", True
    root = Path(root_value).expanduser().resolve()
    return _user_database(
        root / PRIVATE_WORLD_DEFAULT_RELATIVE_PATH,
        user_id,
    ), None, True


def create_private_world_runtime(
    environ: Mapping[str, str] | None = None,
    *,
    user_id: str = _DEFAULT_USER_ID,
) -> PrivateWorldRuntime:
    """Create the optional local ledger without blocking the reply runtime."""

    try:
        path, reason, enabled = resolve_private_world_database(
            environ,
            user_id=user_id,
        )
    except (OSError, TypeError, ValueError, RuntimeError, sqlite3.Error):
        return PrivateWorldRuntime(
            NullPrivateWorldPort(),
            None,
            "unavailable",
            "none",
            "PRIVATE_WORLD_STORAGE_UNAVAILABLE",
            True,
        )
    if not enabled:
        return PrivateWorldRuntime(
            NullPrivateWorldPort(),
            None,
            "disabled",
            "none",
            reason,
            False,
        )
    if path is None:
        return PrivateWorldRuntime(
            NullPrivateWorldPort(),
            None,
            "unavailable",
            "none",
            reason or "PRIVATE_WORLD_STORAGE_UNAVAILABLE",
            True,
        )

    try:
        ledger = SQLitePrivateWorldLedger(path)
        counts = ledger.health()
        return PrivateWorldRuntime(
            ledger,
            PrivateWorldDeliveryCommitter(ledger),
            "available",
            "sqlite",
            None,
            True,
            schema_version=ledger.schema_version,
            migration_status=ledger.migration_status,
            event_count=int(counts["event_count"]),
            snapshot_count=int(counts["snapshot_count"]),
        )
    except (OSError, ValueError, RuntimeError, sqlite3.Error, LedgerWriteError):
        return PrivateWorldRuntime(
            NullPrivateWorldPort(),
            None,
            "unavailable",
            "none",
            "PRIVATE_WORLD_STORAGE_UNAVAILABLE",
            True,
        )


__all__ = [
    "PRIVATE_WORLD_DEFAULT_RELATIVE_PATH",
    "PrivateWorldRuntime",
    "create_private_world_runtime",
    "resolve_private_world_database",
]
