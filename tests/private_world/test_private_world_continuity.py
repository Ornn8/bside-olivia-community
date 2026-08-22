import json
import sqlite3
from pathlib import Path

from private_world_ledger import LedgerEvent, SQLitePrivateWorldLedger
from private_world_port import (
    ContinuationAwareness,
    LocalContinuationFact,
    PrivateWorldSnapshot,
)


def _event(number: int) -> LedgerEvent:
    return LedgerEvent(
        event_id=f"event-{number}",
        delivery_id=f"delivery-{number}",
        event_type="continuation_updated",
        payload={"changed_fields": ["continuation_facts"]},
        occurred_at="2026-08-22T00:00:00+00:00",
    )


def test_ledger_round_trips_unicode_nickname_and_continuation_facts(
    tmp_path: Path,
) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private.sqlite3")
    snapshot = PrivateWorldSnapshot(
        nickname_permissions=("小河豚",),
        continuation_facts=(
            LocalContinuationFact(
                "trip.pending",
                "下个月可能有一段旅行安排。",
                ContinuationAwareness.PENDING,
            ),
            LocalContinuationFact(
                "class.known",
                "她已经知道下周课程会调整。",
                ContinuationAwareness.CHARACTER_KNOWN,
            ),
        ),
    )

    assert ledger.apply_once(_event(1), snapshot)
    assert ledger.snapshot() == snapshot
    assert ledger.events()[0].payload == {
        "changed_fields": ["continuation_facts"]
    }


def test_ledger_loads_legacy_snapshot_without_continuation_facts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.sqlite3"
    ledger = SQLitePrivateWorldLedger(database)
    legacy = PrivateWorldSnapshot(trust=3).to_dict()
    legacy.pop("continuation_facts")
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO private_world_events
               (event_id, delivery_id, event_type, payload_json, occurred_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                "legacy-event",
                "legacy-delivery",
                "legacy",
                "{}",
                "2026-08-22T00:00:00+00:00",
            ),
        )
        connection.execute(
            """INSERT INTO private_world_snapshots
               (version, payload_json, event_id) VALUES (?, ?, ?)""",
            (1, json.dumps(legacy), "legacy-event"),
        )

    loaded = ledger.snapshot()
    assert loaded.trust == 3
    assert loaded.continuation_facts == ()
