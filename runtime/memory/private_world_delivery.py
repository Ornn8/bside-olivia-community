"""Exactly-once PrivateWorld commit at canonical reply delivery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import hashlib
import re
import sqlite3

from private_world_ledger import LedgerEvent, LedgerWriteError, SQLitePrivateWorldLedger
from private_world_reducer import ReducerEvent, ReducerEventKind, reduce_private_world


_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")


class DeliveryStatus(StrEnum):
    COMMITTED = "COMMITTED"
    DUPLICATE = "DUPLICATE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class DeliveryEvent:
    delivery_id: str
    kind: ReducerEventKind
    occurred_at: datetime
    semantic_key: str
    last_equivalent_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.delivery_id, str) or not _ID_RE.fullmatch(
            self.delivery_id
        ):
            raise ValueError("delivery_id is invalid")
        if self.kind is not ReducerEventKind.CANONICAL_REPLY_DELIVERED:
            raise ValueError("canonical reply delivery kind is required")
        ReducerEvent(
            kind=self.kind,
            occurred_at=self.occurred_at,
            semantic_key=self.semantic_key,
            last_equivalent_at=self.last_equivalent_at,
        )


class PrivateWorldDeliveryCommitter:
    def __init__(self, ledger: SQLitePrivateWorldLedger) -> None:
        self.ledger = ledger

    def commit(self, delivery: DeliveryEvent) -> DeliveryStatus:
        if type(delivery) is not DeliveryEvent:
            raise TypeError("typed delivery is required")
        if delivery.kind is not ReducerEventKind.CANONICAL_REPLY_DELIVERED:
            raise ValueError("canonical reply delivery kind is required")
        DeliveryEvent.__post_init__(delivery)
        event = ReducerEvent(
            kind=delivery.kind,
            occurred_at=delivery.occurred_at,
            semantic_key=delivery.semantic_key,
            last_equivalent_at=delivery.last_equivalent_at,
        )
        try:
            snapshot = self.ledger.snapshot()
        except (
            AttributeError,
            KeyError,
            LedgerWriteError,
            OSError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ):
            return DeliveryStatus.UNAVAILABLE
        try:
            reduced = reduce_private_world(snapshot, event)
            digest = hashlib.sha256(delivery.delivery_id.encode("utf-8")).hexdigest()
            applied = self.ledger.apply_once(
                LedgerEvent(
                    event_id=f"delivery.{digest}",
                    delivery_id=delivery.delivery_id,
                    event_type=event.kind.value,
                    payload={
                        "applied": reduced.delta.applied,
                        "reason_code": reduced.delta.reason_code,
                        "change_fields": [
                            change.field for change in reduced.delta.changes
                        ],
                    },
                    occurred_at=delivery.occurred_at.isoformat(),
                ),
                reduced.snapshot,
            )
        except (LedgerWriteError, OSError, sqlite3.Error):
            return DeliveryStatus.UNAVAILABLE
        return DeliveryStatus.COMMITTED if applied else DeliveryStatus.DUPLICATE
