from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from private_world_port import (
    AcknowledgedAffection,
    ActiveBoundary,
    AffectionIntensity,
    AffectionScope,
    PrivateWorldSnapshot,
)
from private_world_reducer import (
    ReducerEventKind,
)
from private_world_ledger import LedgerEvent, SQLitePrivateWorldLedger
from runtime.memory.private_world_delivery import (
    DeliveryEvent,
    DeliveryStatus,
    PrivateWorldDeliveryCommitter,
)
from runtime.memory.private_world_relationship import (
    PrivateWorldRelationshipCommitter,
    RelationshipFactCommand,
    RelationshipFactStatus,
)
from runtime.memory.private_world_projection import project_private_world
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_policy import SharedHistoryClaim, ViolationCode, scan_reply


NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)
REPLY_SHA256 = hashlib.sha256(b"synthetic canonical reply").hexdigest()


def test_canonical_interaction_evidence_moves_affection_not_permissions(tmp_path):
    ledger = SQLitePrivateWorldLedger(tmp_path / "world.sqlite3")
    deliveries = PrivateWorldDeliveryCommitter(ledger)
    relationships = PrivateWorldRelationshipCommitter(ledger)
    user, reply = "不用赶，按你自己的节奏练。", "听到你这么说我轻松多了。"
    signal = {"kind": "support_received", "user_quote": user, "reply_quote": reply}
    assert relationships.commit_exchange("letter:1", user, reply, signal, occurred_at=NOW) is RelationshipFactStatus.REJECTED
    deliveries.commit(DeliveryEvent(delivery_id="letter:1", occurred_at=NOW, semantic_key="letter:1",
        canonical_reply_sha256=hashlib.sha256(reply.encode()).hexdigest()))
    assert relationships.commit_exchange("letter:1", user, reply, signal, occurred_at=NOW) is RelationshipFactStatus.COMMITTED
    first = ledger.snapshot()
    assert (first.trust, first.comfort, first.familiarity) == (1, 1, 1)
    assert relationships.commit_exchange("letter:1", user, reply, signal, occurred_at=NOW) is RelationshipFactStatus.DUPLICATE
    assert ledger.snapshot() == first
    changed = reply + "另一段正文"
    assert relationships.commit_exchange("letter:1", user, changed, signal, occurred_at=NOW) is RelationshipFactStatus.REJECTED
    for kind, expected in (("conflict", (0, 0, 3)), ("repair", (1, 1, 1))):
        delivery_id = "letter:" + kind
        when = NOW + timedelta(minutes=1)
        deliveries.commit(DeliveryEvent(delivery_id=delivery_id, occurred_at=when, semantic_key=delivery_id,
            canonical_reply_sha256=hashlib.sha256(reply.encode()).hexdigest()))
        relationships.commit_exchange(delivery_id, user, reply, {**signal, "kind":kind}, occurred_at=when)
        current = ledger.snapshot()
        assert (current.trust, current.comfort, current.tension) == expected
        assert current.relationship_stage == "unknown"
        assert current.intimacy_grants == ()
        assert current.nickname_permissions == ()


@pytest.mark.parametrize("offset_hours", [8, -5])
def test_exchange_accepts_same_instant_in_local_timezone_and_remains_idempotent(tmp_path, offset_hours):
    ledger = SQLitePrivateWorldLedger(tmp_path / "world.sqlite3")
    deliveries = PrivateWorldDeliveryCommitter(ledger)
    relationships = PrivateWorldRelationshipCommitter(ledger)
    user, reply = "不用赶，按你自己的节奏练。", "听到你这么说我轻松多了。"
    signal = {"kind": "support_received", "user_quote": user, "reply_quote": reply}
    deliveries.commit(DeliveryEvent(delivery_id="letter:offset", occurred_at=NOW,
        semantic_key="letter:offset", canonical_reply_sha256=hashlib.sha256(reply.encode()).hexdigest()))
    local = NOW.astimezone(timezone(timedelta(hours=offset_hours)))
    assert relationships.commit_exchange("letter:offset", user, reply, signal,
        occurred_at=local - timedelta(seconds=1)) is RelationshipFactStatus.REJECTED
    assert relationships.commit_exchange("letter:offset", user, reply, signal,
        occurred_at=local) is RelationshipFactStatus.COMMITTED
    first = ledger.snapshot()
    assert (first.familiarity, first.trust, first.comfort) == (1, 1, 1)
    assert relationships.commit_exchange("letter:offset", user, reply, signal,
        occurred_at=NOW) is RelationshipFactStatus.DUPLICATE
    assert ledger.snapshot() == first
    assert datetime.fromisoformat(ledger.events()[-1].occurred_at) == NOW
    assert datetime.fromisoformat(ledger.events()[-1].occurred_at).utcoffset() == timedelta(0)


def test_distinct_support_evidence_is_not_a_same_day_duplicate(tmp_path):
    ledger = SQLitePrivateWorldLedger(tmp_path / "world.sqlite3")
    deliveries = PrivateWorldDeliveryCommitter(ledger)
    relationships = PrivateWorldRelationshipCommitter(ledger)
    first = ("今天身体不舒服就先休息。", "谢谢关心，我安心多了。")
    second = ("你的画我认真看了，颜色选得很好。", "你仔细看过我很开心，谢谢鼓励。")
    for index, (evidence, when, expected) in enumerate([
        (first, NOW, 1),
        (second, NOW + timedelta(minutes=1), 2),
        (first, NOW + timedelta(minutes=2), 2),
        (first, NOW + timedelta(hours=24), 3),
    ]):
        user, reply = evidence
        delivery_id = f"letter:{index}"
        deliveries.commit(DeliveryEvent(delivery_id=delivery_id, occurred_at=when,
            semantic_key=delivery_id, canonical_reply_sha256=hashlib.sha256(reply.encode()).hexdigest()))
        signal = {"kind": "support_received", "user_quote": user, "reply_quote": reply}
        assert relationships.commit_exchange(delivery_id, user, reply, signal,
            occurred_at=when) is RelationshipFactStatus.COMMITTED
        snapshot = ledger.snapshot()
        assert (snapshot.familiarity, snapshot.trust, snapshot.comfort) == (expected,) * 3
        assert relationships.commit_exchange(delivery_id, user, reply, signal,
            occurred_at=when) is RelationshipFactStatus.DUPLICATE
        assert ledger.snapshot() == snapshot
        assert snapshot.relationship_stage == "unknown"
        assert snapshot.intimacy_grants == () and snapshot.nickname_permissions == ()
    assert ledger.events()[5].payload["reason_code"] == "SEMANTIC_DUPLICATE"
    # Semantic identifiers must not expose either participant's private words.
    assert all(first[0] not in str(event.payload) and second[0] not in str(event.payload)
               for event in ledger.events())


def test_legacy_kind_only_cooldown_expires_without_rewriting_old_events(tmp_path):
    ledger = SQLitePrivateWorldLedger(tmp_path / "world.sqlite3")
    ledger.apply_once(LedgerEvent(event_id="legacy", delivery_id="legacy",
        event_type="support_received", occurred_at=NOW.isoformat(),
        payload={"semantic_key": "canonical-interaction:support_received", "applied": True}),
        PrivateWorldSnapshot(version=2, familiarity=1, trust=1, comfort=1))
    deliveries = PrivateWorldDeliveryCommitter(ledger)
    relationships = PrivateWorldRelationshipCommitter(ledger)
    user, reply = "不舒服就先休息。", "谢谢关心，我安心多了。"
    for index, (when, expected) in enumerate([(NOW + timedelta(minutes=1), 1),
                                             (NOW + timedelta(hours=24), 2)]):
        delivery_id = f"new:{index}"
        deliveries.commit(DeliveryEvent(delivery_id=delivery_id, occurred_at=when,
            semantic_key=delivery_id, canonical_reply_sha256=hashlib.sha256(reply.encode()).hexdigest()))
        relationships.commit_exchange(delivery_id, user, reply,
            {"kind": "support_received", "user_quote": user, "reply_quote": reply}, occurred_at=when)
        assert ledger.snapshot().trust == expected
    assert ledger.events()[0].payload["semantic_key"] == "canonical-interaction:support_received"


@pytest.mark.parametrize("field,value", [("user_quote", "她说的话"), ("reply_quote", "编造的原文"), ("kind", "stage_confirmed")])
def test_canonical_interaction_rejects_wrong_evidence_or_permission_kind(tmp_path, field, value):
    ledger = SQLitePrivateWorldLedger(tmp_path / "world.sqlite3")
    committer = PrivateWorldRelationshipCommitter(ledger)
    with pytest.raises(ValueError):
        committer.commit_exchange("letter:1", "不用赶", "谢谢理解", {
            **{"kind":"support_received", "user_quote":"不用赶", "reply_quote":"谢谢理解"}, field:value,
        }, occurred_at=NOW)
    assert ledger.events() == ()


def test_projection_authorizes_only_ledger_backed_character_statements() -> None:
    affection = AcknowledgedAffection(
        intensity=AffectionIntensity.CARE,
        statement_ref_id="reply.canonical.1.line.3",
        scope=AffectionScope.ONGOING_CORRESPONDENCE,
    )
    projected = project_private_world(
        PrivateWorldSnapshot(acknowledged_affection=affection)
    )
    context = ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(NOW),
        private_behavior=projected.behavior,
    )
    candidate = "我已经说过，我很在意这些来信。"
    backed = SharedHistoryClaim(
        affection.statement_ref_id,
        0,
        len(candidate),
        True,
    )
    user_only = SharedHistoryClaim(
        "user.claimed.affection",
        0,
        len(candidate),
        True,
    )

    assert scan_reply(
        candidate,
        context,
        shared_history_claims=(backed,),
    ).passed
    blocked = scan_reply(
        candidate,
        context,
        shared_history_claims=(user_only,),
    )
    assert tuple(item.code for item in blocked.violations) == (
        ViolationCode.UNAUTHORIZED_SHARED_HISTORY,
    )


def test_relationship_fact_commit_requires_an_already_committed_canonical_reply(
    tmp_path,
) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private-world.sqlite3")
    delivery_committer = PrivateWorldDeliveryCommitter(ledger)
    relationship_committer = PrivateWorldRelationshipCommitter(ledger)
    boundary = ActiveBoundary(
        "boundary.synthetic",
        NOW.isoformat(),
        "offline_meeting",
    )
    effect = RelationshipFactCommand(
        command_id="effect.boundary.1",
        kind=ReducerEventKind.CHARACTER_BOUNDARY_SET,
        occurred_at=NOW,
        semantic_key="effect.boundary.1",
        canonical_delivery_id="canonical.delivery.1",
        canonical_reply_sha256=REPLY_SHA256,
        evidence_ref_id="canonical.delivery.1.boundary.1",
        boundary=boundary,
    )

    assert relationship_committer.commit(effect) is RelationshipFactStatus.REJECTED
    assert ledger.snapshot().active_boundaries == ()

    assert delivery_committer.commit(
        DeliveryEvent(
            delivery_id="canonical.delivery.1",
            occurred_at=NOW,
            semantic_key="canonical.delivery.1",
            canonical_reply_sha256=REPLY_SHA256,
        )
    ) is DeliveryStatus.COMMITTED
    assert relationship_committer.commit(effect) is RelationshipFactStatus.COMMITTED
    assert ledger.snapshot().active_boundaries == (boundary,)
    committed = ledger.events()[-1]
    assert committed.payload["canonical_delivery_id"] == "canonical.delivery.1"
    assert committed.payload["canonical_reply_sha256"] == REPLY_SHA256
    assert committed.payload["evidence_ref_id"] == "canonical.delivery.1.boundary.1"


def test_relationship_fact_cannot_precede_its_canonical_delivery(tmp_path) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private-world.sqlite3")
    delivery_committer = PrivateWorldDeliveryCommitter(ledger)
    relationship_committer = PrivateWorldRelationshipCommitter(ledger)
    delivered_at = NOW + timedelta(minutes=1)
    assert delivery_committer.commit(
        DeliveryEvent(
            delivery_id="canonical.delivery.late",
            occurred_at=delivered_at,
            semantic_key="canonical.delivery.late",
            canonical_reply_sha256=REPLY_SHA256,
        )
    ) is DeliveryStatus.COMMITTED

    status = relationship_committer.commit(
        RelationshipFactCommand(
            command_id="effect.boundary.early",
            kind=ReducerEventKind.CHARACTER_BOUNDARY_SET,
            occurred_at=NOW,
            semantic_key="effect.boundary.early",
            canonical_delivery_id="canonical.delivery.late",
            canonical_reply_sha256=REPLY_SHA256,
            evidence_ref_id="canonical.delivery.late.boundary.1",
            boundary=ActiveBoundary(
                "boundary.early",
                NOW.isoformat(),
                "offline_meeting",
            ),
        )
    )

    assert status is RelationshipFactStatus.REJECTED
    assert ledger.snapshot().active_boundaries == ()


def test_affection_command_requires_the_exact_character_statement_evidence() -> None:
    with pytest.raises(ValueError, match="affection evidence"):
        RelationshipFactCommand(
            command_id="effect.affection.mismatch",
            kind=ReducerEventKind.CHARACTER_AFFECTION_ACKNOWLEDGED,
            occurred_at=NOW,
            semantic_key="effect.affection.mismatch",
            canonical_delivery_id="canonical.delivery.affection",
            canonical_reply_sha256=REPLY_SHA256,
            evidence_ref_id="canonical.delivery.affection.line.2",
            acknowledged_affection=AcknowledgedAffection(
                intensity=AffectionIntensity.CARE,
                statement_ref_id="canonical.delivery.affection.line.3",
                scope=AffectionScope.THIS_REPLY,
            ),
            asserted_affection_scope=AffectionScope.THIS_REPLY,
        )


def test_local_server_uses_the_dedicated_relationship_command_path(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    ledger = SQLitePrivateWorldLedger(tmp_path / "private-world.sqlite3")
    assert PrivateWorldDeliveryCommitter(ledger).commit(
        DeliveryEvent(
            delivery_id="canonical.delivery.production",
            occurred_at=NOW,
            semantic_key="canonical.delivery.production",
            canonical_reply_sha256=REPLY_SHA256,
        )
    ) is DeliveryStatus.COMMITTED
    monkeypatch.setattr(
        local_server,
        "private_world_relationship_committer",
        PrivateWorldRelationshipCommitter(ledger),
    )
    boundary = ActiveBoundary(
        "boundary.production",
        NOW.isoformat(),
        "offline_meeting",
    )

    status = local_server.commit_private_world_relationship_fact(
        RelationshipFactCommand(
            command_id="effect.boundary.production",
            kind=ReducerEventKind.CHARACTER_BOUNDARY_SET,
            occurred_at=NOW,
            semantic_key="effect.boundary.production",
            canonical_delivery_id="canonical.delivery.production",
            canonical_reply_sha256=REPLY_SHA256,
            evidence_ref_id="canonical.delivery.production.boundary.1",
            boundary=boundary,
        )
    )

    assert status is RelationshipFactStatus.COMMITTED
    assert ledger.snapshot().active_boundaries == (boundary,)
