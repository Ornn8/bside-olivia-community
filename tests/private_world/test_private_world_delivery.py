import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from runtime.memory.private_world_delivery import (
    DeliveryEvent,
    DeliveryStatus,
    PrivateWorldDeliveryCommitter,
)
from private_world_ledger import LedgerEvent, SQLitePrivateWorldLedger
from private_world_port import IntimacyGrant, PrivateWorldSnapshot
from private_world_reducer import ReducerEventKind
from reply_orchestrator import ReplyState
from runtime.reply.reply_pipeline import PipelineResult
from runtime.reply.reply_context import IntimacyTier


NOW = datetime(2026, 8, 22, tzinfo=timezone.utc)


def test_delivery_event_validates_intimacy_payload_without_stage_fields(
    tmp_path: Path,
) -> None:
    grant = IntimacyGrant(
        grant_id="intimacy.synthetic-delivery",
        tier=IntimacyTier.LIGHT_CONTACT,
        statement="A synthetic consent statement.",
    )
    delivery = DeliveryEvent(
        delivery_id="letter-intimacy:1",
        kind=ReducerEventKind.INTIMACY_GRANTED,
        occurred_at=NOW,
        semantic_key="intimacy.synthetic-delivery",
        intimacy_grant=grant,
    )

    with pytest.raises(ValueError):
        DeliveryEvent(
            delivery_id="letter-intimacy-missing:1",
            kind=ReducerEventKind.INTIMACY_GRANTED,
            occurred_at=NOW,
            semantic_key="intimacy.synthetic-missing",
        )
    with pytest.raises(ValueError):
        DeliveryEvent(
            delivery_id="letter-intimacy-stage:1",
            kind=ReducerEventKind.INTIMACY_GRANTED,
            occurred_at=NOW,
            semantic_key="intimacy.synthetic-stage",
            target_stage="close",
            basis_event_ids=("basis.synthetic",),
            intimacy_grant=grant,
        )

    ledger = SQLitePrivateWorldLedger(tmp_path / "private.sqlite3")
    ledger.apply_once(
        LedgerEvent(
            event_id="seed.intimacy-stage",
            delivery_id="seed.intimacy-stage",
            event_type="stage_confirmed",
            payload={"synthetic": True},
            occurred_at=NOW.isoformat(),
        ),
        PrivateWorldSnapshot(relationship_stage="close"),
    )
    committer = PrivateWorldDeliveryCommitter(ledger)
    assert committer.commit(delivery) is DeliveryStatus.COMMITTED
    assert committer.commit(delivery) is DeliveryStatus.DUPLICATE
    reopened = SQLitePrivateWorldLedger(tmp_path / "private.sqlite3")
    assert reopened.snapshot().intimacy_grants == (grant,)
    assert reopened.snapshot().closeness == 2
    assert reopened.snapshot().growth_used == 2


def test_delivery_commits_once_with_stable_delivery_id(tmp_path: Path) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private.sqlite3")
    committer = PrivateWorldDeliveryCommitter(ledger)
    delivery = DeliveryEvent(
        delivery_id="letter-1:1",
        kind=ReducerEventKind.CANONICAL_REPLY_DELIVERED,
        occurred_at=NOW,
        semantic_key="canonical.letter-1",
    )

    assert committer.commit(delivery) is DeliveryStatus.COMMITTED
    assert committer.commit(delivery) is DeliveryStatus.DUPLICATE
    assert ledger.health() == {
        "status": "READY",
        "event_count": 1,
        "snapshot_count": 1,
    }


def test_explicit_relationship_event_reduces_then_persists_atomically(
    tmp_path: Path,
) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private.sqlite3")
    committer = PrivateWorldDeliveryCommitter(ledger)

    status = committer.commit(
        DeliveryEvent(
            delivery_id="letter-2:1",
            kind=ReducerEventKind.BOUNDARY_RESPECTED,
            occurred_at=NOW,
            semantic_key="boundary.synthetic",
        )
    )

    assert status is DeliveryStatus.COMMITTED
    assert ledger.snapshot().trust == 1
    assert ledger.snapshot().comfort == 1
    assert ledger.snapshot().version == 2


def test_delivery_degrades_when_sqlite_snapshot_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private.sqlite3")
    committer = PrivateWorldDeliveryCommitter(ledger)
    delivery = DeliveryEvent(
        delivery_id="letter-sqlite-failure:1",
        kind=ReducerEventKind.CANONICAL_REPLY_DELIVERED,
        occurred_at=NOW,
        semantic_key="canonical.sqlite-failure",
    )

    def unavailable_snapshot() -> object:
        raise sqlite3.DatabaseError("synthetic sqlite failure")

    monkeypatch.setattr(ledger, "snapshot", unavailable_snapshot)

    assert committer.commit(delivery) is DeliveryStatus.UNAVAILABLE


def test_delivery_degrades_when_snapshot_json_is_semantically_corrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private.sqlite3")
    committer = PrivateWorldDeliveryCommitter(ledger)
    delivery = DeliveryEvent(
        delivery_id="letter-corrupt-snapshot:1",
        kind=ReducerEventKind.CANONICAL_REPLY_DELIVERED,
        occurred_at=NOW,
        semantic_key="canonical.corrupt-snapshot",
    )

    def corrupt_snapshot() -> object:
        raise json.JSONDecodeError("synthetic corrupt snapshot", "{", 1)

    monkeypatch.setattr(ledger, "snapshot", corrupt_snapshot)

    assert committer.commit(delivery) is DeliveryStatus.UNAVAILABLE


def test_delivery_degrades_when_snapshot_json_has_the_wrong_shape(
    tmp_path: Path,
) -> None:
    database = tmp_path / "private.sqlite3"
    ledger = SQLitePrivateWorldLedger(database)
    ledger.apply_once(
        LedgerEvent(
            event_id="wrong-shape-seed-event",
            delivery_id="wrong-shape-seed-delivery",
            event_type="canonical_reply_delivered",
            payload={"applied": False},
            occurred_at=NOW.isoformat(),
        ),
        PrivateWorldSnapshot(version=1),
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE private_world_snapshots SET payload_json = ?",
            ("[]",),
        )
    committer = PrivateWorldDeliveryCommitter(ledger)

    assert committer.commit(
        DeliveryEvent(
            delivery_id="wrong-shape-delivery:1",
            kind=ReducerEventKind.CANONICAL_REPLY_DELIVERED,
            occurred_at=NOW,
            semantic_key="canonical.wrong-shape",
        )
    ) is DeliveryStatus.UNAVAILABLE


class AcceptedPipeline:
    async def run(self, request: object, context: object) -> PipelineResult:
        return PipelineResult(
            "letter-3",
            ReplyState.COMPLETED,
            text="canonical reply",
            quality_status="accepted",
        )


class TextTriage:
    reply_mode = "text"

    def to_dict(self) -> dict[str, str]:
        return {"reply_mode": "text"}


class TextTriageService:
    async def classify(self, content: str) -> TextTriage:
        return TextTriage()


def test_canonical_reply_is_persisted_pending_before_private_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    letter = {"letter_id": "letter-3", "reply_text": "", "letter_status": "PENDING"}
    sequence: list[tuple[str, str | None]] = []

    class RecordingCommitter:
        def commit(self, delivery: DeliveryEvent) -> DeliveryStatus:
            sequence.append(("commit", letter.get("private_world_status")))
            return DeliveryStatus.COMMITTED

    monkeypatch.setattr(local_server.store, "letters", [letter])
    monkeypatch.setattr(local_server, "emotion_triage", TextTriageService())
    monkeypatch.setattr(local_server, "reply_pipeline", AcceptedPipeline())
    monkeypatch.setattr(local_server, "private_world_committer", RecordingCommitter())
    monkeypatch.setattr(local_server, "_schedule_text_reply_delay", lambda *args: None)
    monkeypatch.setattr(
        local_server,
        "_persist_store_state",
        lambda: sequence.append(("persist", letter.get("private_world_status"))),
    )
    monkeypatch.setattr(local_server.letters_adapter, "remember_conversation", lambda *args: None)

    assert asyncio.run(local_server.generate_reply("letter-3", "input")) is True
    assert ("persist", "PENDING") in sequence
    pending_index = sequence.index(("persist", "PENDING"))
    commit_index = sequence.index(("commit", "PENDING"))
    assert pending_index < commit_index
    assert letter["private_world_status"] == "COMMITTED"
    assert letter["private_world_delivery_id"] == "letter-3:1"


def test_pending_delivery_recovers_without_changing_canonical_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import local_server

    ledger = SQLitePrivateWorldLedger(tmp_path / "private.sqlite3")
    letter = {
        "letter_id": "letter-4",
        "reply_text": "already persisted canonical reply",
        "letter_status": "COMPLETED",
        "reply_revision": 1,
        "private_world_status": "PENDING",
        "private_world_delivery_id": "letter-4:1",
        "private_world_occurred_at": NOW.isoformat(),
        "private_world_event_kind": "canonical_reply_delivered",
        "private_world_semantic_key": "canonical.letter-4",
    }
    monkeypatch.setattr(local_server.store, "letters", [letter])
    monkeypatch.setattr(
        local_server,
        "private_world_committer",
        PrivateWorldDeliveryCommitter(ledger),
    )
    monkeypatch.setattr(local_server, "_persist_store_state", lambda: None)

    assert local_server.recover_pending_private_world() == 1
    assert local_server.recover_pending_private_world() == 0
    assert letter["reply_text"] == "already persisted canonical reply"
    assert letter["private_world_status"] == "COMMITTED"
    assert ledger.health()["event_count"] == 1
