from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from private_world_ledger import SQLitePrivateWorldLedger
from runtime.memory.private_world_delivery import DeliveryEvent, PrivateWorldDeliveryCommitter
from runtime.memory.private_world_relationship import PrivateWorldRelationshipCommitter


def exchange(ledger, index, kind, *, day=0, quote=None):
    when = datetime(2026, 9, 13, tzinfo=timezone.utc) + timedelta(days=day)
    user = quote or f"我今天读到第{index}章，喜欢那个开放结尾。"
    reply = f"第{index}章我也有自己的看法，我们聊聊那个结尾。"
    delivery = f"synthetic:{index}"
    PrivateWorldDeliveryCommitter(ledger).commit(DeliveryEvent(
        delivery_id=delivery, occurred_at=when, semantic_key=delivery,
        canonical_reply_sha256=hashlib.sha256(reply.encode()).hexdigest()))
    return PrivateWorldRelationshipCommitter(ledger).commit_exchange(
        delivery, user, reply, {"kind": kind, "user_quote": user, "reply_quote": reply}, occurred_at=when)


@pytest.mark.parametrize("kind,expected", [
    ("meaningful_exchange", (2, 0, 1, 0)),
    ("shared_experience", (2, 1, 1, 2)),
])
def test_daily_growth_reaches_ledger_without_granting_permissions(tmp_path, kind, expected):
    ledger = SQLitePrivateWorldLedger(tmp_path / "world.sqlite3")
    exchange(ledger, 1, kind)
    snapshot = ledger.snapshot()
    assert (snapshot.familiarity, snapshot.trust, snapshot.comfort, snapshot.closeness) == expected
    assert snapshot.relationship_stage == "unknown"
    assert snapshot.intimacy_grants == snapshot.nickname_permissions == ()
    exchange(ledger, 1, kind)
    assert ledger.snapshot() == snapshot


def test_new_growth_is_bounded_and_reopens_after_seven_days(tmp_path):
    ledger = SQLitePrivateWorldLedger(tmp_path / "world.sqlite3")
    for i in range(30):
        exchange(ledger, i, "shared_experience")
    before = ledger.snapshot()
    assert (before.familiarity, before.trust, before.comfort, before.closeness) == (42, 21, 21, 42)
    exchange(ledger, 31, "shared_experience", day=7)
    assert ledger.snapshot().closeness == 44


def test_same_user_evidence_cannot_grow_again_with_reworded_reply(tmp_path):
    ledger = SQLitePrivateWorldLedger(tmp_path / "world.sqlite3")
    exchange(ledger, 1, "meaningful_exchange", quote="今天我读完了那本书，结尾让我有点难过。")
    exchange(ledger, 2, "meaningful_exchange", quote="今天我读完了那本书，结尾让我有点难过。")
    assert ledger.snapshot().familiarity == 2
