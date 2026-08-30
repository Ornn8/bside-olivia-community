"""Atomic, auditable execution for typed PrivateWorld commands."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from threading import RLock
from typing import Protocol, runtime_checkable

from .commands import (
    ApplyHistoricalRelationshipEvidence,
    ConfirmRelationshipStage,
    GrantIntimacy,
    PrivateWorldActor,
    PrivateWorldCommand,
    PrivateWorldCommandKind,
    PrivateWorldCommandSource,
    RecordBoundaryRespected,
    RecordConflict,
    RecordRepair,
)
from .ledger import LedgerEvent, LedgerWriteError
from .port import PrivateWorldSnapshot
from .reducer import reduce_private_world_command


PRIVATE_WORLD_COMMAND_AUDIT_SCHEMA = (
    "p03.private-world-command-audit.v1"
)


class PrivateWorldCommandServiceError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CommandExecutionStatus(StrEnum):
    APPLIED = "APPLIED"
    NOOP = "NOOP"
    DUPLICATE = "DUPLICATE"


@dataclass(frozen=True)
class CommandExecutionResult:
    status: CommandExecutionStatus
    command_id: str
    event_id: str
    reason_code: str
    snapshot_version: int
    change_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "command_id": self.command_id,
            "event_id": self.event_id,
            "reason_code": self.reason_code,
            "snapshot_version": self.snapshot_version,
            "change_fields": list(self.change_fields),
        }


@runtime_checkable
class PrivateWorldCommandLedger(Protocol):
    def snapshot(self) -> PrivateWorldSnapshot: ...

    def events(self) -> tuple[LedgerEvent, ...]: ...

    def apply_once(
        self,
        event: LedgerEvent,
        snapshot: PrivateWorldSnapshot,
    ) -> bool: ...


_RELATIONSHIP_CANDIDATE_COMMANDS = (
    RecordBoundaryRespected,
    RecordConflict,
    RecordRepair,
)
_STAGE_BASIS_EVENT_TYPES = frozenset(
    {
        "boundary_respected",
        "conflict",
        "repair",
        PrivateWorldCommandKind.RECORD_BOUNDARY_RESPECTED.value,
        PrivateWorldCommandKind.RECORD_CONFLICT.value,
        PrivateWorldCommandKind.RECORD_REPAIR.value,
    }
)


def _digest(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{prefix}.{digest}"


def _fingerprint(command: PrivateWorldCommand) -> str:
    encoded = json.dumps(
        command.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _event_identity(
    command: PrivateWorldCommand,
) -> tuple[str, str]:
    return (
        _digest("command", command.command_id),
        _digest("idempotency", command.idempotency_key),
    )


def _result_from_audit(
    event: LedgerEvent,
    *,
    duplicate: bool,
) -> CommandExecutionResult:
    payload = event.payload
    try:
        status = (
            CommandExecutionStatus.DUPLICATE
            if duplicate
            else (
                CommandExecutionStatus.APPLIED
                if payload["applied"] is True
                else CommandExecutionStatus.NOOP
            )
        )
        command_id = str(payload["command_id"])
        reason_code = str(payload["reason_code"])
        snapshot_version = int(payload["snapshot_version"])
        raw_fields = payload.get("change_fields", ())
        if not isinstance(raw_fields, list) or any(
            not isinstance(field, str) for field in raw_fields
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise PrivateWorldCommandServiceError(
            "PRIVATE_WORLD_COMMAND_AUDIT_INVALID"
        ) from exc
    return CommandExecutionResult(
        status,
        command_id,
        event.event_id,
        reason_code,
        snapshot_version,
        tuple(raw_fields),
    )


class PrivateWorldCommandService:
    """Serialize, authorize, reduce, and atomically audit commands."""

    def __init__(self, ledger: PrivateWorldCommandLedger) -> None:
        if not isinstance(ledger, PrivateWorldCommandLedger):
            raise TypeError("a PrivateWorld command ledger is required")
        self._ledger = ledger
        self._lock = RLock()

    @staticmethod
    def _authorize(command: PrivateWorldCommand) -> None:
        if isinstance(command, ApplyHistoricalRelationshipEvidence):
            if (
                command.actor is not PrivateWorldActor.MIGRATION
                or command.source is not PrivateWorldCommandSource.IMPORT
            ):
                raise PrivateWorldCommandServiceError(
                    "PRIVATE_WORLD_COMMAND_SOURCE_FORBIDDEN"
                )
            return
        if isinstance(command, GrantIntimacy):
            if (
                command.actor is not PrivateWorldActor.LOCAL_USER
                or command.source
                is not PrivateWorldCommandSource.CONTROL_CENTER
            ):
                raise PrivateWorldCommandServiceError(
                    "PRIVATE_WORLD_COMMAND_SOURCE_FORBIDDEN"
                )
            return
        if command.actor is PrivateWorldActor.SYSTEM_CANDIDATE:
            raise PrivateWorldCommandServiceError(
                "PRIVATE_WORLD_COMMAND_APPROVAL_REQUIRED"
            )
        if command.actor is PrivateWorldActor.MIGRATION:
            if command.source not in {
                PrivateWorldCommandSource.MIGRATION,
                PrivateWorldCommandSource.IMPORT,
            }:
                raise PrivateWorldCommandServiceError(
                    "PRIVATE_WORLD_COMMAND_SOURCE_FORBIDDEN"
                )
            return
        if command.actor is not PrivateWorldActor.LOCAL_USER:
            raise PrivateWorldCommandServiceError(
                "PRIVATE_WORLD_COMMAND_SOURCE_FORBIDDEN"
            )
        if command.source not in {
            PrivateWorldCommandSource.CONTROL_CENTER,
            PrivateWorldCommandSource.APPROVED_CANDIDATE,
        }:
            raise PrivateWorldCommandServiceError(
                "PRIVATE_WORLD_COMMAND_SOURCE_FORBIDDEN"
            )
        if command.source is PrivateWorldCommandSource.APPROVED_CANDIDATE:
            if not isinstance(
                command,
                _RELATIONSHIP_CANDIDATE_COMMANDS,
            ):
                raise PrivateWorldCommandServiceError(
                    "PRIVATE_WORLD_COMMAND_SOURCE_FORBIDDEN"
                )
            if not command.evidence_refs:
                raise PrivateWorldCommandServiceError(
                    "PRIVATE_WORLD_COMMAND_EVIDENCE_REQUIRED"
                )

    def _ledger_events(self) -> tuple[LedgerEvent, ...]:
        try:
            return self._ledger.events()
        except (
            LedgerWriteError,
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            raise PrivateWorldCommandServiceError(
                "PRIVATE_WORLD_COMMAND_STORAGE_UNAVAILABLE"
            ) from exc

    def _ledger_snapshot(self) -> PrivateWorldSnapshot:
        try:
            snapshot = self._ledger.snapshot()
        except (
            LedgerWriteError,
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            raise PrivateWorldCommandServiceError(
                "PRIVATE_WORLD_COMMAND_STORAGE_UNAVAILABLE"
            ) from exc
        if not isinstance(snapshot, PrivateWorldSnapshot):
            raise PrivateWorldCommandServiceError(
                "PRIVATE_WORLD_COMMAND_STORAGE_UNAVAILABLE"
            )
        return snapshot

    def _matching_identity_events(
        self,
        event_id: str,
        delivery_id: str,
    ) -> tuple[LedgerEvent, ...]:
        return tuple(
            event
            for event in self._ledger_events()
            if event.event_id == event_id
            or event.delivery_id == delivery_id
        )

    @staticmethod
    def _same_command(
        event: LedgerEvent,
        command: PrivateWorldCommand,
        fingerprint: str,
    ) -> bool:
        payload = event.payload
        return (
            payload.get("schema_version")
            == PRIVATE_WORLD_COMMAND_AUDIT_SCHEMA
            and payload.get("command_id") == command.command_id
            and payload.get("idempotency_key")
            == command.idempotency_key
            and payload.get("command_fingerprint") == fingerprint
        )

    def _resolve_existing(
        self,
        command: PrivateWorldCommand,
        event_id: str,
        delivery_id: str,
        fingerprint: str,
    ) -> CommandExecutionResult | None:
        matches = self._matching_identity_events(
            event_id,
            delivery_id,
        )
        if not matches:
            return None
        if len(matches) == 1 and self._same_command(
            matches[0],
            command,
            fingerprint,
        ):
            return _result_from_audit(
                matches[0],
                duplicate=True,
            )
        raise PrivateWorldCommandServiceError(
            "PRIVATE_WORLD_COMMAND_IDENTITY_CONFLICT"
        )

    def _validate_stage_basis(
        self,
        command: PrivateWorldCommand,
    ) -> None:
        if not isinstance(command, ConfirmRelationshipStage):
            return
        events = {
            event.event_id: event
            for event in self._ledger_events()
            if event.event_id in command.basis_event_ids
        }
        if set(events) != set(command.basis_event_ids):
            raise PrivateWorldCommandServiceError(
                "PRIVATE_WORLD_COMMAND_EVIDENCE_INVALID"
            )
        if any(
            event.event_type not in _STAGE_BASIS_EVENT_TYPES
            for event in events.values()
        ):
            raise PrivateWorldCommandServiceError(
                "PRIVATE_WORLD_COMMAND_EVIDENCE_INVALID"
            )

    def lookup_command(
        self,
        command_id: str,
    ) -> CommandExecutionResult | None:
        """Return bounded persisted command metadata without its audit body."""

        if not isinstance(command_id, str):
            raise TypeError("a PrivateWorld command id is required")
        event_id = _digest("command", command_id)
        with self._lock:
            matches = tuple(
                event
                for event in self._ledger_events()
                if event.event_id == event_id
            )
        if not matches:
            return None
        if (
            len(matches) != 1
            or matches[0].payload.get("schema_version")
            != PRIVATE_WORLD_COMMAND_AUDIT_SCHEMA
            or matches[0].payload.get("command_id") != command_id
        ):
            raise PrivateWorldCommandServiceError(
                "PRIVATE_WORLD_COMMAND_AUDIT_INVALID"
            )
        return _result_from_audit(matches[0], duplicate=False)

    @staticmethod
    def _audit_payload(
        command: PrivateWorldCommand,
        fingerprint: str,
        *,
        applied: bool,
        reason_code: str,
        change_fields: tuple[str, ...],
        snapshot_version: int,
    ) -> dict[str, object]:
        return {
            "schema_version": PRIVATE_WORLD_COMMAND_AUDIT_SCHEMA,
            "command_id": command.command_id,
            "idempotency_key": command.idempotency_key,
            "command_kind": command.kind.value,
            "actor": command.actor.value,
            "source": command.source.value,
            "reason": command.reason,
            "evidence_refs": list(command.evidence_refs),
            "payload_fields": sorted(command.payload()),
            "command_fingerprint": fingerprint,
            "applied": applied,
            "reason_code": reason_code,
            "change_fields": list(change_fields),
            "snapshot_version": snapshot_version,
        }

    def execute(
        self,
        command: PrivateWorldCommand,
    ) -> CommandExecutionResult:
        if not isinstance(command, PrivateWorldCommand):
            raise TypeError(
                "a typed PrivateWorld command is required"
            )
        self._authorize(command)
        event_id, delivery_id = _event_identity(command)
        fingerprint = _fingerprint(command)

        with self._lock:
            existing = self._resolve_existing(
                command,
                event_id,
                delivery_id,
                fingerprint,
            )
            if existing is not None:
                return existing
            self._validate_stage_basis(command)
            reduced = reduce_private_world_command(
                self._ledger_snapshot(),
                command,
            )
            change_fields = tuple(
                change.field for change in reduced.delta.changes
            )
            audit = self._audit_payload(
                command,
                fingerprint,
                applied=reduced.delta.applied,
                reason_code=reduced.delta.reason_code,
                change_fields=change_fields,
                snapshot_version=reduced.snapshot.version,
            )
            event = LedgerEvent(
                event_id=event_id,
                delivery_id=delivery_id,
                event_type=command.kind.value,
                payload=audit,
                occurred_at=command.occurred_at.isoformat(),
            )
            try:
                applied = self._ledger.apply_once(
                    event,
                    reduced.snapshot,
                )
            except (
                LedgerWriteError,
                OSError,
                RuntimeError,
                ValueError,
                KeyError,
                TypeError,
            ) as exc:
                raise PrivateWorldCommandServiceError(
                    "PRIVATE_WORLD_COMMAND_STORAGE_UNAVAILABLE"
                ) from exc
            if not applied:
                duplicate = self._resolve_existing(
                    command,
                    event_id,
                    delivery_id,
                    fingerprint,
                )
                if duplicate is not None:
                    return duplicate
                raise PrivateWorldCommandServiceError(
                    "PRIVATE_WORLD_COMMAND_IDENTITY_CONFLICT"
                )
            return CommandExecutionResult(
                (
                    CommandExecutionStatus.APPLIED
                    if reduced.delta.applied
                    else CommandExecutionStatus.NOOP
                ),
                command.command_id,
                event_id,
                reduced.delta.reason_code,
                reduced.snapshot.version,
                change_fields,
            )


__all__ = [
    "CommandExecutionResult",
    "CommandExecutionStatus",
    "PRIVATE_WORLD_COMMAND_AUDIT_SCHEMA",
    "PrivateWorldCommandLedger",
    "PrivateWorldCommandService",
    "PrivateWorldCommandServiceError",
]
