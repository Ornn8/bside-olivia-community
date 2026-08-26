from datetime import datetime, timezone
from pathlib import Path

import pytest

from private_world_admin import AdminOperationError, PrivateWorldAdmin
from private_world_delivery import DeliveryEvent, DeliveryStatus
from private_world_reducer import ReducerEventKind
from private_world_runtime import (
    create_private_world_runtime,
    resolve_private_world_database,
)


NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)


def _commit(runtime, *, kind: ReducerEventKind) -> None:
    assert runtime.committer is not None
    assert runtime.committer.commit(
        DeliveryEvent(
            delivery_id="same-delivery:1",
            kind=kind,
            occurred_at=NOW,
            semantic_key="same.semantic:1",
        )
    ) is DeliveryStatus.COMMITTED


def test_current_user_reset_is_isolated_idempotent_and_persists(
    tmp_path: Path,
) -> None:
    environment = {"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path / "state")}
    user_a = create_private_world_runtime(environment, user_id="User-A")
    user_b = create_private_world_runtime(environment, user_id="user-b")

    _commit(user_a, kind=ReducerEventKind.CANONICAL_REPLY_DELIVERED)
    _commit(user_b, kind=ReducerEventKind.BOUNDARY_RESPECTED)
    assert user_b.port.snapshot().trust == 1
    events_before_reset = user_b.port.events()

    database, reason, enabled = resolve_private_world_database(
        environment, user_id="USER-A"
    )
    assert database is not None and reason is None and enabled is True
    admin = PrivateWorldAdmin(database, user_id="user-a")

    first = admin.reset_current_user(
        request_id="reset.current-user:1",
        reason="synthetic reset",
        confirmed=True,
    )
    duplicate = admin.reset_current_user(
        request_id="reset.current-user:1",
        reason="synthetic reset",
        confirmed=True,
    )
    with pytest.raises(AdminOperationError) as conflict:
        admin.reset_current_user(
            request_id="reset.current-user:1",
            reason="synthetic conflicting reset",
            confirmed=True,
        )

    restarted_a = create_private_world_runtime(environment, user_id="user-a")
    restarted_b = create_private_world_runtime(environment, user_id="USER-B")
    assert first.status == "APPLIED"
    assert first.affected_event_count == 1
    assert duplicate.status == "DUPLICATE"
    assert conflict.value.code == "PRIVATE_WORLD_ADMIN_REQUEST_CONFLICT"
    assert restarted_a.port.events() == ()
    assert restarted_b.port.events() == events_before_reset
    assert restarted_b.port.snapshot().trust == 1

    database_b, reason_b, enabled_b = resolve_private_world_database(
        environment, user_id="user-b"
    )
    assert database_b is not None and reason_b is None and enabled_b is True
    other_user = PrivateWorldAdmin(database_b, user_id="user-b")
    assert other_user.reset_current_user(
        request_id="reset.current-user:1",
        reason="synthetic reset",
        confirmed=True,
    ).status == "APPLIED"
