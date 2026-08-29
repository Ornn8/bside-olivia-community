from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from conversation_memory_port import MemoryWriteResult, MemoryWriteStatus
from llm_gateway import GatewayResponse
from private_world_commands import InitializeHistoricalRelationship
from private_world_service import CommandExecutionResult, CommandExecutionStatus
from runtime.imports.historical_memory import (
    HistoricalExchange,
    apply_historical_private_world,
    assess_historical_relationship,
    exchanges_from_legacy_payload,
    migrate_historical_exchanges,
)


class RecordingMemory:
    enabled = True

    def __init__(self, statuses: list[MemoryWriteStatus]) -> None:
        self.statuses = list(statuses)
        self.events: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    def remember_exchange(self, **kwargs: object) -> MemoryWriteResult:
        source_id = str(kwargs["source_id"])
        self.events.append(("write", str(kwargs["user_message"])))
        status = self.statuses.pop(0)
        return MemoryWriteResult(
            status,
            source_id,
            (f"memory.{len(self.events)}",)
            if status is MemoryWriteStatus.WRITTEN
            else (),
            "MEM0_WRITE_FAILED"
            if status is MemoryWriteStatus.UNAVAILABLE
            else None,
        )

    def status(self):
        return type("Status", (), {"status": "available"})()

    def delete_memory(self, memory_id: str, *, user_id: str) -> bool:
        assert user_id == "local-user"
        self.deleted.append(memory_id)
        return True


def _exchange(name: str, timestamp: int) -> HistoricalExchange:
    return HistoricalExchange(
        source_record_id=f"official:user:{name}",
        occurred_at=datetime.fromtimestamp(timestamp, timezone.utc),
        user_message=f"user-{name}",
        assistant_message=f"assistant-{name}",
    )


def test_migration_writes_each_exchange_in_strict_chronological_order_then_finalizes_once() -> None:
    memory = RecordingMemory(
        [MemoryWriteStatus.WRITTEN, MemoryWriteStatus.SKIPPED, MemoryWriteStatus.DUPLICATE]
    )
    finalizations: list[tuple[str, ...]] = []

    result = migrate_historical_exchanges(
        (_exchange("third", 30), _exchange("first", 10), _exchange("second", 20)),
        memory=memory,
        user_id="local-user",
        finalize_private_world=lambda ordered: finalizations.append(
            tuple(item.user_message for item in ordered)
        )
        or "initialized",
    )

    assert memory.events == [
        ("write", "user-first"),
        ("write", "user-second"),
        ("write", "user-third"),
    ]
    assert finalizations == [("user-first", "user-second", "user-third")]
    assert result.to_dict() == {
        "status": "completed",
        "total": 3,
        "processed": 3,
        "written": 1,
        "duplicates": 1,
        "skipped": 1,
        "private_world_status": "initialized",
        "error_code": None,
    }


def test_migration_reports_progress_after_each_persisted_exchange() -> None:
    memory = RecordingMemory(
        [MemoryWriteStatus.WRITTEN, MemoryWriteStatus.DUPLICATE]
    )
    progress: list[tuple[int, int]] = []

    result = migrate_historical_exchanges(
        (_exchange("second", 20), _exchange("first", 10)),
        memory=memory,
        user_id="local-user",
        on_progress=lambda processed, total: progress.append((processed, total)),
    )

    assert result.status == "completed"
    assert progress == [(0, 2), (1, 2), (2, 2)]


def test_migration_stops_at_first_failed_write_and_does_not_finalize() -> None:
    memory = RecordingMemory(
        [MemoryWriteStatus.WRITTEN, MemoryWriteStatus.UNAVAILABLE, MemoryWriteStatus.WRITTEN]
    )
    finalizations: list[tuple[HistoricalExchange, ...]] = []

    result = migrate_historical_exchanges(
        (_exchange("first", 10), _exchange("second", 20), _exchange("third", 30)),
        memory=memory,
        user_id="local-user",
        finalize_private_world=lambda ordered: finalizations.append(ordered) or "initialized",
    )

    assert memory.events == [("write", "user-first"), ("write", "user-second")]
    assert finalizations == []
    assert result.status == "partial"
    assert result.processed == 1
    assert result.error_code == "MEM0_WRITE_FAILED"


def test_official_history_strict_migration_accepts_uninformative_skips() -> None:
    memory = RecordingMemory(
        [MemoryWriteStatus.SKIPPED, MemoryWriteStatus.WRITTEN]
    )

    result = migrate_historical_exchanges(
        (_exchange("first", 10), _exchange("second", 20)),
        memory=memory,
        user_id="local-user",
        require_persisted=True,
    )

    assert result.status == "completed"
    assert result.processed == 2
    assert result.written == 1
    assert result.skipped == 1
    assert result.error_code is None


def test_official_history_strict_migration_rolls_back_new_writes_on_failure() -> None:
    memory = RecordingMemory(
        [MemoryWriteStatus.WRITTEN, MemoryWriteStatus.UNAVAILABLE]
    )

    result = migrate_historical_exchanges(
        (_exchange("first", 10), _exchange("second", 20)),
        memory=memory,
        user_id="local-user",
        require_persisted=True,
    )

    assert result.status == "partial"
    assert result.error_code == "MEM0_WRITE_FAILED"
    assert memory.deleted == ["memory.1"]


def test_official_history_strict_migration_reconciles_a_timed_out_write() -> None:
    class TimedOutThenSettledMemory(RecordingMemory):
        def remember_exchange(self, **kwargs: object) -> MemoryWriteResult:
            if len(self.events) == 1:
                self.events.append(("write", str(kwargs["user_message"])))
                return MemoryWriteResult(
                    MemoryWriteStatus.UNAVAILABLE,
                    str(kwargs["source_id"]),
                    error_code="MEM0_WRITE_TIMEOUT",
                )
            return super().remember_exchange(**kwargs)

        def settle_exchange_write(self, **kwargs: object) -> MemoryWriteResult:
            return MemoryWriteResult(
                MemoryWriteStatus.WRITTEN,
                str(kwargs["source_id"]),
                ("memory.late",),
            )

    memory = TimedOutThenSettledMemory([MemoryWriteStatus.WRITTEN])

    result = migrate_historical_exchanges(
        (_exchange("first", 10), _exchange("second", 20)),
        memory=memory,
        user_id="local-user",
        require_persisted=True,
    )

    assert result.status == "completed"
    assert result.processed == 2
    assert result.written == 2
    assert memory.deleted == []


def test_official_history_does_not_rollback_a_timed_out_duplicate() -> None:
    class TimedOutDuplicateMemory(RecordingMemory):
        def remember_exchange(self, **kwargs: object) -> MemoryWriteResult:
            self.events.append(("write", str(kwargs["user_message"])))
            if len(self.events) == 1:
                return MemoryWriteResult(
                    MemoryWriteStatus.UNAVAILABLE,
                    str(kwargs["source_id"]),
                    error_code="MEM0_WRITE_TIMEOUT",
                )
            return MemoryWriteResult(
                MemoryWriteStatus.UNAVAILABLE,
                str(kwargs["source_id"]),
                error_code="MEM0_WRITE_FAILED",
            )

        def settle_exchange_write(self, **kwargs: object) -> MemoryWriteResult:
            return MemoryWriteResult(
                MemoryWriteStatus.DUPLICATE,
                str(kwargs["source_id"]),
            )

    memory = TimedOutDuplicateMemory([])

    result = migrate_historical_exchanges(
        (_exchange("first", 10), _exchange("second", 20)),
        memory=memory,
        user_id="local-user",
        require_persisted=True,
    )

    assert result.status == "partial"
    assert result.duplicates == 1
    assert memory.deleted == []


def test_official_history_strict_migration_rolls_back_after_invalid_result() -> None:
    class InvalidSecondResultMemory(RecordingMemory):
        def remember_exchange(self, **kwargs: object):
            if len(self.events) == 1:
                self.events.append(("write", str(kwargs["user_message"])))
                return object()
            return super().remember_exchange(**kwargs)

    memory = InvalidSecondResultMemory([MemoryWriteStatus.WRITTEN])

    result = migrate_historical_exchanges(
        (_exchange("first", 10), _exchange("second", 20)),
        memory=memory,
        user_id="local-user",
        require_persisted=True,
    )

    assert result.status == "partial"
    assert result.error_code == "MEM0_WRITE_RESULT_INVALID"
    assert memory.deleted == ["memory.1"]


def test_official_history_rollback_attempts_every_new_memory_id() -> None:
    class PartlyFailingDeleteMemory(RecordingMemory):
        def delete_memory(self, memory_id: str, *, user_id: str) -> bool:
            super().delete_memory(memory_id, user_id=user_id)
            return memory_id != "memory.2"

    memory = PartlyFailingDeleteMemory(
        [
            MemoryWriteStatus.WRITTEN,
            MemoryWriteStatus.WRITTEN,
            MemoryWriteStatus.UNAVAILABLE,
        ]
    )

    result = migrate_historical_exchanges(
        (_exchange("first", 10), _exchange("second", 20), _exchange("third", 30)),
        memory=memory,
        user_id="local-user",
        require_persisted=True,
    )

    assert result.error_code == "MEM0_ROLLBACK_FAILED"
    assert memory.deleted == ["memory.2", "memory.1"]


def test_official_history_rolls_back_pending_ids_from_failed_current_write() -> None:
    class PendingMismatchMemory(RecordingMemory):
        def remember_exchange(self, **kwargs: object) -> MemoryWriteResult:
            self.events.append(("write", str(kwargs["user_message"])))
            return MemoryWriteResult(
                MemoryWriteStatus.UNAVAILABLE,
                str(kwargs["source_id"]),
                ("memory.pending",),
                "MEM0_LANGUAGE_MISMATCH_ROLLBACK_FAILED",
            )

    memory = PendingMismatchMemory([])

    result = migrate_historical_exchanges(
        (_exchange("first", 10),),
        memory=memory,
        user_id="local-user",
        require_persisted=True,
    )

    assert result.status == "partial"
    assert memory.deleted == ["memory.pending"]


def test_migration_uses_stable_source_ids_so_a_retry_can_resume_by_deduplication() -> None:
    exchange = _exchange("same", 10)
    first = RecordingMemory([MemoryWriteStatus.WRITTEN])
    second = RecordingMemory([MemoryWriteStatus.DUPLICATE])

    first_result = migrate_historical_exchanges(
        (exchange,), memory=first, user_id="local-user"
    )
    second_result = migrate_historical_exchanges(
        (exchange,), memory=second, user_id="local-user"
    )

    assert first_result.status == second_result.status == "completed"
    assert first_result.written == 1
    assert second_result.duplicates == 1


def test_migration_rejects_untyped_input_before_any_memory_write() -> None:
    memory = RecordingMemory([MemoryWriteStatus.WRITTEN])

    with pytest.raises(TypeError, match="historical exchanges must be typed"):
        migrate_historical_exchanges(
            (_exchange("first", 10), object()),
            memory=memory,
            user_id="local-user",
        )

    assert memory.events == []


def test_official_legacy_payload_becomes_typed_exchanges_without_combined_archive_text() -> None:
    exchanges = exchanges_from_legacy_payload(
        {
            "mode": "read_only",
            "letters": [
                {
                    "source_record_id": "official:account:later",
                    "occurred_at": 20,
                    "content": "combined archive display text",
                    "metadata": {
                        "import_kind": "official_text_reply",
                        "user_content": "later user text",
                        "reply_text": "later reply text",
                    },
                },
                {
                    "source_record_id": "official:account:first",
                    "occurred_at": 10,
                    "content": "another combined display text",
                    "metadata": {
                        "import_kind": "official_text_reply",
                        "user_content": "first user text",
                        "reply_text": "first reply text",
                    },
                },
            ],
        }
    )

    assert [item.user_message for item in exchanges] == [
        "first user text",
        "later user text",
    ]
    assert [item.assistant_message for item in exchanges] == [
        "first reply text",
        "later reply text",
    ]
    assert all("combined archive" not in item.user_message for item in exchanges)


class AssessmentGateway:
    def __init__(self) -> None:
        self.messages: tuple[dict[str, str], ...] = ()

    async def complete(self, messages, *, request_id=None) -> GatewayResponse:
        del request_id
        self.messages = tuple(messages)
        return GatewayResponse(
            '{"relationship_stage":"familiar","familiarity":48,"trust":44,'
            '"comfort":42,"closeness":36,"tension":9,"evidence_indexes":[1,2]}',
            "request-1",
            "fixture",
            "fixture-model",
        )


class RecordingCommandService:
    def __init__(self) -> None:
        self.commands: list[InitializeHistoricalRelationship] = []

    def execute(self, command: InitializeHistoricalRelationship) -> CommandExecutionResult:
        self.commands.append(command)
        return CommandExecutionResult(
            CommandExecutionStatus.APPLIED,
            command.command_id,
            "event.fixture",
            "INITIALIZE_HISTORICAL_RELATIONSHIP",
            2,
            ("relationship_stage",),
        )


def test_private_world_assessment_uses_persona_policy_and_ordered_history_once() -> None:
    import asyncio

    gateway = AssessmentGateway()
    ordered = (_exchange("first", 10), _exchange("second", 20))

    assessment = asyncio.run(
        assess_historical_relationship(
            ordered,
            gateway=gateway,
            persona_policy="AUTHORITATIVE PERSONA POLICY",
        )
    )

    assert assessment.relationship_stage.value == "familiar"
    assert assessment.evidence_indexes == (1, 2)
    assert "AUTHORITATIVE PERSONA POLICY" in gateway.messages[0]["content"]
    assert gateway.messages[1]["content"].index("user-first") < gateway.messages[1][
        "content"
    ].index("user-second")


def test_private_world_assessment_bounds_a_long_history_to_gateway_input_limit() -> None:
    import asyncio

    class BoundedGateway(AssessmentGateway):
        config = type("Config", (), {"max_input_chars": 2_000})()

        async def complete(self, messages, *, request_id=None) -> GatewayResponse:
            del request_id
            self.messages = tuple(messages)
            assert sum(len(item["content"]) for item in self.messages) <= 2_000
            return GatewayResponse(
                '{"relationship_stage":"familiar","familiarity":48,"trust":44,'
                '"comfort":42,"closeness":36,"tension":9,'
                '"evidence_indexes":[1,30]}',
                "request-bounded",
                "fixture",
                "fixture-model",
            )

    ordered = tuple(
        HistoricalExchange(
            source_record_id=f"official:user:{index}",
            occurred_at=datetime.fromtimestamp(index, timezone.utc),
            user_message=(f"user-{index}-" + "u" * 1_000),
            assistant_message=(f"assistant-{index}-" + "a" * 1_000),
        )
        for index in range(1, 31)
    )
    gateway = BoundedGateway()

    assessment = asyncio.run(
        assess_historical_relationship(
            ordered,
            gateway=gateway,
            persona_policy="P" * 12_000,
        )
    )

    prompt = json.loads(gateway.messages[1]["content"])["ordered_exchanges"]
    assert prompt[0]["index"] == 1
    assert prompt[-1]["index"] == 30
    assert [item["index"] for item in prompt] == sorted(
        item["index"] for item in prompt
    )
    assert assessment.evidence_indexes == (1, 30)


def test_private_world_initialization_commits_exactly_one_migration_command() -> None:
    import asyncio

    ordered = (_exchange("first", 10), _exchange("second", 20))
    assessment = asyncio.run(
        assess_historical_relationship(
            ordered,
            gateway=AssessmentGateway(),
            persona_policy="AUTHORITATIVE PERSONA POLICY",
        )
    )
    service = RecordingCommandService()

    status = apply_historical_private_world(
        ordered,
        assessment=assessment,
        command_service=service,
    )

    assert status == "initialized"
    assert len(service.commands) == 1
    assert service.commands[0].actor.value == "migration"
    assert service.commands[0].evidence_refs == (
        ordered[0].memory_source_id,
        ordered[1].memory_source_id,
    )


def test_server_migration_runs_mem0_in_order_before_one_private_world_commit(
    monkeypatch,
) -> None:
    import asyncio
    import local_server

    memory = RecordingMemory([MemoryWriteStatus.WRITTEN, MemoryWriteStatus.WRITTEN])
    gateway = AssessmentGateway()
    service = RecordingCommandService()
    monkeypatch.setattr(local_server, "conversation_memory_adapter", memory)
    monkeypatch.setattr(local_server, "private_world_command_service", service)
    monkeypatch.setattr(local_server.letters_adapter, "gateway", gateway)
    monkeypatch.setattr(
        local_server.letters_adapter,
        "get_persona_policy",
        lambda: "AUTHORITATIVE PERSONA POLICY",
    )

    result = asyncio.run(
        local_server._migrate_official_history(
            {
                "mode": "read_only",
                "letters": [
                    {
                        "source_record_id": "official:account:later",
                        "occurred_at": 20,
                        "metadata": {
                            "import_kind": "official_text_reply",
                            "user_content": "user-second",
                            "reply_text": "assistant-second",
                        },
                    },
                    {
                        "source_record_id": "official:account:first",
                        "occurred_at": 10,
                        "metadata": {
                            "import_kind": "official_text_reply",
                            "user_content": "user-first",
                            "reply_text": "assistant-first",
                        },
                    },
                ],
            }
        )
    )

    assert result.status == "completed"
    assert result.private_world_status == "initialized"
    assert memory.events == [("write", "user-first"), ("write", "user-second")]
    assert len(service.commands) == 1
    assert "AUTHORITATIVE PERSONA POLICY" in gateway.messages[0]["content"]


def test_server_migration_skips_relationship_llm_when_private_world_already_exists(
    monkeypatch,
) -> None:
    import asyncio
    import local_server
    from private_world_port import PrivateWorldSnapshot

    memory = RecordingMemory([MemoryWriteStatus.DUPLICATE])

    class ExistingWorld:
        def snapshot(self) -> PrivateWorldSnapshot:
            return PrivateWorldSnapshot(version=2, trust=20)

    class FailingGateway:
        async def complete(self, *_args, **_kwargs):
            raise AssertionError("relationship LLM must not run twice")

    monkeypatch.setattr(local_server, "conversation_memory_adapter", memory)
    monkeypatch.setattr(local_server, "private_world_port", ExistingWorld())
    monkeypatch.setattr(local_server.letters_adapter, "gateway", FailingGateway())

    result = asyncio.run(
        local_server._migrate_official_history(
            {
                "mode": "read_only",
                "account_id": "account",
                "letters": [
                    {
                        "source_record_id": "official:account:first",
                        "occurred_at": 10,
                        "metadata": {
                            "import_kind": "official_text_reply",
                            "user_content": "user-first",
                            "reply_text": "assistant-first",
                        },
                    }
                ],
            }
        )
    )

    assert result.status == "completed"
    assert result.private_world_status == "already_initialized"


def test_server_migration_reports_partial_when_private_world_is_unavailable(
    monkeypatch,
) -> None:
    import asyncio
    import local_server

    memory = RecordingMemory([MemoryWriteStatus.WRITTEN])
    monkeypatch.setattr(local_server, "conversation_memory_adapter", memory)
    monkeypatch.setattr(local_server, "private_world_command_service", None)

    result = asyncio.run(
        local_server._migrate_official_history(
            {
                "mode": "read_only",
                "account_id": "account",
                "letters": [
                    {
                        "source_record_id": "official:account:first",
                        "occurred_at": 10,
                        "metadata": {
                            "import_kind": "official_text_reply",
                            "user_content": "user-first",
                            "reply_text": "assistant-first",
                        },
                    }
                ],
            }
        )
    )

    assert result.status == "partial"
    assert result.private_world_status == "unavailable"
    assert result.error_code == "PRIVATE_WORLD_HISTORY_UNAVAILABLE"
