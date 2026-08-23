from __future__ import annotations

import pytest

from control_center.original_client_private_world_commands import (
    OriginalClientPrivateWorldCommandServiceBackend,
)
from control_center.private_world_api import (
    PrivateWorldApiCommand,
    PrivateWorldApiError,
    PrivateWorldCommandResult,
)
from original_client_companion_mutation_api import (
    OriginalClientCompanionMutationError,
)


class RecordingBackend:
    def __init__(self, result: PrivateWorldCommandResult | None = None) -> None:
        self.commands: list[PrivateWorldApiCommand] = []
        self.result = result or PrivateWorldCommandResult(
            command_id="command.fixture.1",
            status="committed",
            applied=True,
            reason_code="NICKNAME_GRANTED",
            change_fields=("nickname_permissions",),
            snapshot_version=2,
        )

    def snapshot_summary(self):
        raise AssertionError("command adapter must not read snapshot")

    def event_summaries(self, *, limit: int):
        raise AssertionError("command adapter must not read events")

    def execute(self, command: PrivateWorldApiCommand) -> PrivateWorldCommandResult:
        self.commands.append(command)
        return self.result


def test_adapter_builds_exact_canonical_typed_command() -> None:
    backend = RecordingBackend()
    adapter = OriginalClientPrivateWorldCommandServiceBackend(backend)

    result = adapter.execute_private_world(
        operation="grant_nickname",
        payload={"nickname": "小河豚"},
        request_id="request.private.grant.1",
        reason="用户明确授权。",
        occurred_at="2026-08-23T12:00:00+00:00",
    )

    assert result.status == "APPLIED"
    assert result.affected_count == 1
    assert result.reason_code == "NICKNAME_GRANTED"
    assert result.request_id == "request.private.grant.1"
    assert len(backend.commands) == 1
    command = backend.commands[0]
    assert command.operation == "grant_nickname"
    assert command.idempotency_key == "request.private.grant.1"
    assert command.actor == "local_user"
    assert command.source == "control_center"
    assert command.payload == {"nickname": "小河豚"}
    assert command.evidence_refs == ("control:request.private.grant.1",)


@pytest.mark.parametrize(
    ("service_status", "applied", "public_status", "affected"),
    [
        ("committed", True, "APPLIED", 1),
        ("duplicate", False, "DUPLICATE", 0),
        ("noop", False, "NOOP", 0),
        ("rejected", False, "REJECTED", 0),
    ],
)
def test_adapter_preserves_canonical_result_states(
    service_status: str,
    applied: bool,
    public_status: str,
    affected: int,
) -> None:
    backend = RecordingBackend(
        PrivateWorldCommandResult(
            command_id="command.fixture.1",
            status=service_status,
            applied=applied,
            reason_code="FIXTURE_RESULT",
            change_fields=(),
            snapshot_version=1,
        )
    )
    result = OriginalClientPrivateWorldCommandServiceBackend(
        backend
    ).execute_private_world(
        operation="set_home_access",
        payload={"home_access": "visit_access"},
        request_id="request.private.home.1",
        reason="用户明确设置。",
        occurred_at="2026-08-23T12:00:00+00:00",
    )
    assert result.status == public_status
    assert result.affected_count == affected


def test_disabled_backend_is_honest() -> None:
    adapter = OriginalClientPrivateWorldCommandServiceBackend(None)
    with pytest.raises(OriginalClientCompanionMutationError) as error:
        adapter.execute_private_world(
            operation="delete_continuation_fact",
            payload={"fact_id": "fact.fixture.1"},
            request_id="request.private.delete.1",
            reason="用户明确删除。",
            occurred_at="2026-08-23T12:00:00+00:00",
        )
    assert error.value.code == "PRIVATE_WORLD_MUTATION_DISABLED"
    assert error.value.status == 503


def test_typed_backend_errors_remain_stable_and_path_free() -> None:
    class FailingBackend(RecordingBackend):
        def execute(self, command: PrivateWorldApiCommand) -> PrivateWorldCommandResult:
            raise PrivateWorldApiError("PRIVATE_WORLD_FACT_NOT_FOUND", status=404)

    adapter = OriginalClientPrivateWorldCommandServiceBackend(FailingBackend())
    with pytest.raises(OriginalClientCompanionMutationError) as error:
        adapter.execute_private_world(
            operation="delete_continuation_fact",
            payload={"fact_id": "fact.fixture.1"},
            request_id="request.private.delete.1",
            reason="用户明确删除。",
            occurred_at="2026-08-23T12:00:00+00:00",
        )
    assert error.value.code == "PRIVATE_WORLD_FACT_NOT_FOUND"
    assert error.value.status == 404
    assert "path" not in str(error.value).casefold()


def test_adapter_does_not_forward_hidden_fields() -> None:
    backend = RecordingBackend()
    adapter = OriginalClientPrivateWorldCommandServiceBackend(backend)
    adapter.execute_private_world(
        operation="upsert_continuation_fact",
        payload={
            "fact_id": "fact.fixture.1",
            "statement": "林离知道这次旅行计划。",
            "awareness": "character_known",
        },
        request_id="request.private.fact.1",
        reason="用户明确确认。",
        occurred_at="2026-08-23T12:00:00+00:00",
    )
    command = backend.commands[0]
    assert set(command.payload) == {"fact_id", "statement", "awareness"}
    assert "trust" not in command.payload
    assert "database" not in str(command.payload).casefold()
