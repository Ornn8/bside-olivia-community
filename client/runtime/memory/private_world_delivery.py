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
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
class DeliveryStatus(StrEnum):
    COMMITTED = "COMMITTED"
    DUPLICATE = "DUPLICATE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class DeliveryEvent:
    delivery_id: str
    occurred_at: datetime
    semantic_key: str
    last_equivalent_at: datetime | None = None
    canonical_reply_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.delivery_id, str) or not _ID_RE.fullmatch(
            self.delivery_id
        ):
            raise ValueError("delivery_id is invalid")
        if self.canonical_reply_sha256 is not None and (
            not isinstance(self.canonical_reply_sha256, str)
            or not _SHA256_RE.fullmatch(self.canonical_reply_sha256)
        ):
            raise ValueError("canonical reply digest is invalid")
        ReducerEvent(
            kind=ReducerEventKind.CANONICAL_REPLY_DELIVERED,
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
        DeliveryEvent.__post_init__(delivery)
        event = ReducerEvent(
            kind=ReducerEventKind.CANONICAL_REPLY_DELIVERED,
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
                        **(
                            {
                                "canonical_reply_sha256": (
                                    delivery.canonical_reply_sha256
                                )
                            }
                            if delivery.canonical_reply_sha256 is not None
                            else {}
                        ),
                    },
                    occurred_at=delivery.occurred_at.isoformat(),
                ),
                reduced.snapshot,
            )
        except (LedgerWriteError, OSError, sqlite3.Error):
            return DeliveryStatus.UNAVAILABLE
        return DeliveryStatus.COMMITTED if applied else DeliveryStatus.DUPLICATE
