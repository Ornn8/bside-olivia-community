from datetime import datetime, timezone
from pathlib import Path

import pytest

from private_world_admin import AdminOperationError, PrivateWorldAdmin
from private_world_commands import (
    PrivateWorldActor,
    PrivateWorldCommandSource,
    RecordBoundaryRespected,
)
from private_world_service import PrivateWorldCommandService
from runtime.memory.private_world_delivery import DeliveryEvent, DeliveryStatus
from runtime.memory.private_world_runtime import create_private_world_runtime


NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)


def _commit(runtime) -> None:
    assert runtime.committer is not None
    assert runtime.committer.commit(
        DeliveryEvent(
            delivery_id="same-delivery:1",
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

    _commit(user_a)
    PrivateWorldCommandService(user_b.port).execute(
        RecordBoundaryRespected(
            command_id="command.synthetic-boundary",
            idempotency_key="idempotency.synthetic-boundary",
            actor=PrivateWorldActor.LOCAL_USER,
            source=PrivateWorldCommandSource.CONTROL_CENTER,
            occurred_at=NOW,
            reason="synthetic confirmed boundary",
            evidence_refs=("letter:synthetic-boundary",),
        )
    )
    assert user_b.port.snapshot().trust == 1
    events_before_reset = user_b.port.events()

    first = PrivateWorldAdmin.reset_current_user(
        environ=environment,
        user_id="USER-A",
        request_id="reset.current-user:1",
        reason="synthetic reset",
        confirmed=True,
    )
    duplicate = PrivateWorldAdmin.reset_current_user(
        environ=environment,
        user_id="user-a",
        request_id="reset.current-user:1",
        reason="synthetic reset",
        confirmed=True,
    )
    with pytest.raises(AdminOperationError) as conflict:
        PrivateWorldAdmin.reset_current_user(
            environ=environment,
            user_id="user-a",
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

    assert PrivateWorldAdmin.reset_current_user(
        environ=environment,
        user_id="user-b",
        request_id="reset.current-user:1",
        reason="synthetic reset",
        confirmed=True,
    ).status == "APPLIED"
