"""Default local PrivateWorld runtime construction and sanitized status."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping

from private_world_delivery import PrivateWorldDeliveryCommitter
from private_world_ledger import (
    LedgerWriteError,
    SQLitePrivateWorldLedger,
)
from private_world_port import NullPrivateWorldPort, PrivateWorldPort


PRIVATE_WORLD_DEFAULT_RELATIVE_PATH = Path(
    "private_world/private_world.sqlite3"
)


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
                event_count = int(counts["event_count"])
                snapshot_count = int(counts["snapshot_count"])
            except (OSError, RuntimeError, ValueError):
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
        return path.resolve(), None, True

    root_value = str(values.get("OLIVIA_LOCAL_DATA_ROOT", "")).strip()
    if not root_value:
        return None, "PRIVATE_WORLD_DATA_ROOT_NOT_CONFIGURED", True
    root = Path(root_value).expanduser().resolve()
    return (root / PRIVATE_WORLD_DEFAULT_RELATIVE_PATH), None, True


def create_private_world_runtime(
    environ: Mapping[str, str] | None = None,
) -> PrivateWorldRuntime:
    """Create the optional local ledger without blocking the reply runtime."""

    path, reason, enabled = resolve_private_world_database(environ)
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
    except (OSError, ValueError, RuntimeError, LedgerWriteError):
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
