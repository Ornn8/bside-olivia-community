"""Typed relationship-fact commits bound to an earlier canonical delivery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
import hashlib
import re
import sqlite3

from private_world_ledger import LedgerEvent, LedgerWriteError, SQLitePrivateWorldLedger
from private_world_port import AcknowledgedAffection, ActiveBoundary, AffectionScope
from private_world_reducer import ReducerEvent, ReducerEventKind, reduce_private_world


_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_INTERACTION_KINDS = {
    ReducerEventKind.BOUNDARY_RESPECTED, ReducerEventKind.SUPPORT_RECEIVED,
    ReducerEventKind.CONFLICT, ReducerEventKind.REPAIR,
}
_RELATIONSHIP_FACT_KINDS = frozenset(
    {
        ReducerEventKind.CHARACTER_BOUNDARY_SET,
        ReducerEventKind.CHARACTER_BOUNDARY_WITHDRAWN,
        ReducerEventKind.CHARACTER_AFFECTION_ACKNOWLEDGED,
        ReducerEventKind.BOUNDARY_RESPECTED,
        ReducerEventKind.SUPPORT_RECEIVED,
        ReducerEventKind.CONFLICT,
        ReducerEventKind.REPAIR,
    }
)


def validate_exchange_relationship(signal: object, user_text: str, reply_text: str) -> dict | None:
    if signal is None:
        return None
    if not isinstance(signal, dict) or set(signal) != {"kind", "user_quote", "reply_quote"}:
        raise ValueError("DAILY_LIFE_RELATIONSHIP_INVALID")
    if signal["kind"] not in {"support_received", "boundary_respected", "conflict", "repair"}:
        raise ValueError("DAILY_LIFE_RELATIONSHIP_INVALID")
    for field, source in (("user_quote", user_text), ("reply_quote", reply_text)):
        quote = signal[field]
        if not isinstance(quote, str) or not quote.strip() or len(quote) > 240 or quote not in source:
            raise ValueError("DAILY_LIFE_RELATIONSHIP_EVIDENCE_INVALID")
    return dict(signal)


class RelationshipFactStatus(StrEnum):
    COMMITTED = "COMMITTED"
    DUPLICATE = "DUPLICATE"
    UNAVAILABLE = "UNAVAILABLE"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class RelationshipFactCommand:
    command_id: str
    kind: ReducerEventKind
    occurred_at: datetime
    semantic_key: str
    canonical_delivery_id: str
    canonical_reply_sha256: str
    evidence_ref_id: str
    last_equivalent_at: datetime | None = None
    boundary: ActiveBoundary | None = None
    boundary_id: str | None = None
    acknowledged_affection: AcknowledgedAffection | None = None
    asserted_affection_scope: AffectionScope | None = None

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not _ID_RE.fullmatch(value)
            for value in (
                self.command_id,
                self.semantic_key,
                self.canonical_delivery_id,
                self.evidence_ref_id,
            )
        ):
            raise ValueError("relationship fact identifiers are invalid")
        if not self.evidence_ref_id.startswith(f"{self.canonical_delivery_id}."):
            raise ValueError("relationship fact evidence is not bound to delivery")
        if not isinstance(self.canonical_reply_sha256, str) or not _SHA256_RE.fullmatch(
            self.canonical_reply_sha256
        ):
            raise ValueError("canonical reply digest is invalid")
        if self.kind not in _RELATIONSHIP_FACT_KINDS:
            raise ValueError("typed character relationship fact is required")
        if (
            self.kind is ReducerEventKind.CHARACTER_AFFECTION_ACKNOWLEDGED
            and self.acknowledged_affection is not None
            and self.acknowledged_affection.statement_ref_id != self.evidence_ref_id
        ):
            raise ValueError("affection evidence does not match character statement")
        if (
            not isinstance(self.occurred_at, datetime)
            or self.occurred_at.tzinfo is None
            or self.occurred_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("relationship fact time must be UTC")
        ReducerEvent(
            kind=self.kind,
            occurred_at=self.occurred_at,
            semantic_key=self.semantic_key,
            last_equivalent_at=self.last_equivalent_at,
            canonical_reply_id=self.canonical_delivery_id if self.kind not in _INTERACTION_KINDS else None,
            boundary=self.boundary,
            boundary_id=self.boundary_id,
            acknowledged_affection=self.acknowledged_affection,
            asserted_affection_scope=self.asserted_affection_scope,
        )


class PrivateWorldRelationshipCommitter:
    def __init__(self, ledger: SQLitePrivateWorldLedger) -> None:
        self.ledger = ledger

    def commit_exchange(self, delivery_id: str, user_text: str, reply_text: str, signal: dict, *, occurred_at: datetime) -> RelationshipFactStatus:
        """Project grounded canonical interaction, never manual permission commands."""
        signal = validate_exchange_relationship(signal, user_text, reply_text)
        if signal is None:
            return RelationshipFactStatus.REJECTED
        semantic_key = "canonical-interaction:" + signal["kind"]
        try:
            previous = [datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
                        for event in self.ledger.events()
                        if event.payload.get("semantic_key") == semantic_key
                        and event.payload.get("applied") is True]
            last_equivalent = max(previous, default=None)
            return self.commit(RelationshipFactCommand(
                command_id="exchange." + hashlib.sha256(delivery_id.encode()).hexdigest(),
                kind=ReducerEventKind(signal["kind"]), occurred_at=occurred_at,
                semantic_key=semantic_key, canonical_delivery_id=delivery_id,
                canonical_reply_sha256=hashlib.sha256(reply_text.encode("utf-8")).hexdigest(),
                evidence_ref_id=delivery_id + ".interaction", last_equivalent_at=last_equivalent,
            ))
        except (ValueError, OSError, sqlite3.Error, LedgerWriteError):
            return RelationshipFactStatus.UNAVAILABLE

    def commit(self, command: RelationshipFactCommand) -> RelationshipFactStatus:
        if type(command) is not RelationshipFactCommand:
            raise TypeError("typed relationship fact command is required")
        RelationshipFactCommand.__post_init__(command)
        try:
            canonical = next(
                (
                    item
                    for item in self.ledger.events()
                    if item.delivery_id == command.canonical_delivery_id
                    and item.event_type
                    == ReducerEventKind.CANONICAL_REPLY_DELIVERED.value
                ),
                None,
            )
        except (
            AttributeError,
            KeyError,
            LedgerWriteError,
            OSError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ):
            return RelationshipFactStatus.UNAVAILABLE
        if (
            canonical is None
            or canonical.payload.get("canonical_reply_sha256")
            != command.canonical_reply_sha256
        ):
            return RelationshipFactStatus.REJECTED
        delivered_at = datetime.fromisoformat(
            canonical.occurred_at.replace("Z", "+00:00")
        )
        if command.occurred_at < delivered_at:
            return RelationshipFactStatus.REJECTED
        event = ReducerEvent(
            kind=command.kind,
            occurred_at=command.occurred_at,
            semantic_key=command.semantic_key,
            last_equivalent_at=command.last_equivalent_at,
            canonical_reply_id=command.canonical_delivery_id if command.kind not in _INTERACTION_KINDS else None,
            boundary=command.boundary,
            boundary_id=command.boundary_id,
            acknowledged_affection=command.acknowledged_affection,
            asserted_affection_scope=command.asserted_affection_scope,
        )
        try:
            snapshot = self.ledger.snapshot()
            reduced = reduce_private_world(snapshot, event)
            digest = hashlib.sha256(command.command_id.encode("utf-8")).hexdigest()
            applied = self.ledger.apply_once(
                LedgerEvent(
                    event_id=f"relationship.{digest}",
                    delivery_id=command.command_id,
                    event_type=event.kind.value,
                    payload={
                        "applied": reduced.delta.applied,
                        "reason_code": reduced.delta.reason_code,
                        "change_fields": [
                            change.field for change in reduced.delta.changes
                        ],
                        "canonical_delivery_id": command.canonical_delivery_id,
                        "canonical_reply_sha256": command.canonical_reply_sha256,
                        "evidence_ref_id": command.evidence_ref_id,
                        "semantic_key": command.semantic_key,
                    },
                    occurred_at=command.occurred_at.isoformat(),
                ),
                reduced.snapshot,
                expected_snapshot_version=snapshot.version,
            )
        except (
            AttributeError,
            KeyError,
            LedgerWriteError,
            OSError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ):
            return RelationshipFactStatus.UNAVAILABLE
        return (
            RelationshipFactStatus.COMMITTED
            if applied
            else RelationshipFactStatus.DUPLICATE
        )


__all__ = [
    "PrivateWorldRelationshipCommitter",
    "RelationshipFactCommand",
    "RelationshipFactStatus",
]
