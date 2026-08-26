from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading

import pytest

from conversation_memory_admin import (
    ConversationMemoryAdminError,
    ConversationMemoryAdminService,
    MemoryAdminMutationStatus,
)
from conversation_memory_port import (
    ConversationMemoryRecord,
    ConversationMemoryStatus,
)


NOW = datetime(2026, 8, 23, 7, 0, tzinfo=timezone.utc)
SECRET_OLD = "用户以前住在大阪。"
SECRET_NEW = "用户现在住在东京。"


class FakeMemory:
    enabled = True

    def __init__(self, *, provider_status: str = "available") -> None:
        self.provider_status = provider_status
        self.records: dict[str, ConversationMemoryRecord] = {}
        self.operations: list[tuple[str, str]] = []
        self.delete_failures = 0
        self._sequence = 0

    def status(self) -> ConversationMemoryStatus:
        enabled = self.provider_status not in {"disabled", "unavailable"}
        return ConversationMemoryStatus(
            self.provider_status,
            enabled,
            "mem0" if enabled else "none",
            "qdrant-local" if enabled else "none",
            reason_code=(
                "MEM0_IMPORT_FAILED"
                if self.provider_status == "unavailable"
                else None
            ),
            memory_count=len(self.records) if enabled else None,
        )

    def list_memories(self, *, user_id: str, limit: int = 100):
        return tuple(
            record
            for record in self.records.values()
            if record.user_id == user_id
        )[:limit]

    def search_context(self, query: str, *, user_id: str, limit: int):
        needle = query.casefold()
        return tuple(
            record
            for record in self.list_memories(user_id=user_id, limit=1000)
            if needle in record.text.casefold()
        )[:limit]

    def add_manual_memory(self, text: str, *, user_id: str, source_id: str):
        self.operations.append(("add", source_id))
        for record in self.records.values():
            if record.user_id == user_id and record.source_id == source_id:
                return record
        self._sequence += 1
        memory_id = f"memory-{self._sequence}"
        record = ConversationMemoryRecord(
            memory_id=memory_id,
            text=text,
            user_id=user_id,
            source_id=source_id,
            occurred_at=NOW,
            created_at=NOW,
            metadata={"manual": True, "actor": "local_user"},
        )
        self.records[memory_id] = record
        return record

    def delete_memory(self, memory_id: str, *, user_id: str) -> bool:
        self.operations.append(("delete", memory_id))
        if self.delete_failures:
            self.delete_failures -= 1
            return False
        record = self.records.get(memory_id)
        if record is None or record.user_id != user_id:
            return False
        del self.records[memory_id]
        return True

    def clear_user(self, *, user_id: str) -> int:
        self.operations.append(("clear", user_id))
        matching = [
            memory_id
            for memory_id, record in self.records.items()
            if record.user_id == user_id
        ]
        for memory_id in matching:
            del self.records[memory_id]
        return len(matching)

    def export_user(self, *, user_id: str):
        return {
            "schema_version": "p03.conversation-memory-export.v1",
            "user_id": user_id,
            "provider": "mem0",
            "records": [
                record.to_prompt_dict()
                for record in self.list_memories(user_id=user_id, limit=1000)
            ],
        }


def _service(tmp_path: Path, memory: FakeMemory) -> ConversationMemoryAdminService:
    return ConversationMemoryAdminService(
        memory,
        tmp_path / "memory" / "admin.sqlite3",
    )


def _seed(memory: FakeMemory, text: str = SECRET_OLD) -> ConversationMemoryRecord:
    return memory.add_manual_memory(
        text,
        user_id="local-user",
        source_id="manual:seed",
    )


def test_list_search_status_and_export_use_only_provider_port(tmp_path: Path) -> None:
    memory = FakeMemory()
    seeded = _seed(memory)
    service = _service(tmp_path, memory)

    assert service.list_memories() == (seeded,)
    assert service.list_memories(query="大阪") == (seeded,)
    assert service.list_memories(query="东京") == ()
    assert service.status().to_dict() == {
        "status": "available",
        "provider": "mem0",
        "enabled": True,
        "audit_count": 0,
        "pending_correction_count": 0,
        "memory_count": 1,
    }
    exported = service.export()
    assert exported["records"][0]["memory_id"] == seeded.memory_id
    assert exported["records"][0]["text"] == SECRET_OLD


def test_add_is_idempotent_and_audit_contains_no_memory_text(tmp_path: Path) -> None:
    memory = FakeMemory()
    service = _service(tmp_path, memory)

    first = service.add(
        SECRET_NEW,
        request_id="add.request-1",
        reason="用户明确要求记住当前居住地。",
    )
    second = service.add(
        SECRET_NEW,
        request_id="add.request-1",
        reason="用户明确要求记住当前居住地。",
    )

    assert first.status is MemoryAdminMutationStatus.APPLIED
    assert second.status is MemoryAdminMutationStatus.DUPLICATE
    assert first.replacement_memory_id == second.replacement_memory_id
    assert memory.operations == [("add", "manual:add.request-1")]
    audit_bytes = service.audit_path.read_bytes()
    assert SECRET_NEW.encode("utf-8") not in audit_bytes
    assert service.status().audit_count == 1


def test_delete_missing_is_noop_and_existing_delete_is_idempotent(tmp_path: Path) -> None:
    memory = FakeMemory()
    service = _service(tmp_path, memory)

    missing = service.delete(
        "memory-missing",
        request_id="delete.missing-1",
        reason="清理不存在的测试记忆。",
    )
    assert missing.status is MemoryAdminMutationStatus.NOOP
    assert memory.operations == []

    seeded = _seed(memory)
    deleted = service.delete(
        seeded.memory_id,
        request_id="delete.existing-1",
        reason="用户确认删除错误记忆。",
    )
    repeated = service.delete(
        seeded.memory_id,
        request_id="delete.existing-1",
        reason="用户确认删除错误记忆。",
    )
    assert deleted.status is MemoryAdminMutationStatus.APPLIED
    assert repeated.status is MemoryAdminMutationStatus.DUPLICATE
    assert seeded.memory_id not in memory.records
    assert memory.operations.count(("delete", seeded.memory_id)) == 1


def test_correction_adds_replacement_before_deleting_old_memory(tmp_path: Path) -> None:
    memory = FakeMemory()
    original = _seed(memory)
    memory.operations.clear()
    service = _service(tmp_path, memory)

    result = service.correct(
        original.memory_id,
        SECRET_NEW,
        request_id="correct.request-1",
        reason="用户纠正了居住地。",
    )
    duplicate = service.correct(
        original.memory_id,
        SECRET_NEW,
        request_id="correct.request-1",
        reason="用户纠正了居住地。",
    )

    assert result.status is MemoryAdminMutationStatus.APPLIED
    assert duplicate.status is MemoryAdminMutationStatus.DUPLICATE
    assert memory.operations == [
        ("add", "correction:correct.request-1"),
        ("delete", original.memory_id),
    ]
    assert original.memory_id not in memory.records
    replacement = memory.records[result.replacement_memory_id]
    assert replacement.text == SECRET_NEW
    assert replacement.source_id == "correction:correct.request-1"
    assert service.status().pending_correction_count == 0


def test_partial_correction_retries_delete_without_writing_second_replacement(
    tmp_path: Path,
) -> None:
    memory = FakeMemory()
    original = _seed(memory)
    memory.operations.clear()
    memory.delete_failures = 1
    service = _service(tmp_path, memory)

    with pytest.raises(ConversationMemoryAdminError) as failed:
        service.correct(
            original.memory_id,
            SECRET_NEW,
            request_id="correct.recovery-1",
            reason="用户纠正了居住地。",
        )
    assert failed.value.code == "MEMORY_ADMIN_CORRECTION_DELETE_FAILED"
    assert service.status().pending_correction_count == 1
    assert original.memory_id in memory.records
    assert sum(
        record.source_id == "correction:correct.recovery-1"
        for record in memory.records.values()
    ) == 1

    recovered = service.correct(
        original.memory_id,
        SECRET_NEW,
        request_id="correct.recovery-1",
        reason="用户纠正了居住地。",
    )
    assert recovered.status is MemoryAdminMutationStatus.APPLIED
    assert original.memory_id not in memory.records
    assert memory.operations.count(("add", "correction:correct.recovery-1")) == 1
    assert memory.operations.count(("delete", original.memory_id)) == 2
    assert service.status().pending_correction_count == 0


def test_clear_requires_confirmation_and_is_idempotent(tmp_path: Path) -> None:
    memory = FakeMemory()
    _seed(memory)
    memory.add_manual_memory(
        "用户喜欢黑胶。",
        user_id="local-user",
        source_id="manual:seed-2",
    )
    memory.operations.clear()
    service = _service(tmp_path, memory)

    with pytest.raises(ConversationMemoryAdminError) as unconfirmed:
        service.clear(
            request_id="clear.request-1",
            reason="用户选择清空新对话长期记忆。",
            confirmed=False,
        )
    assert unconfirmed.value.code == "MEMORY_ADMIN_CONFIRMATION_REQUIRED"

    result = service.clear(
        request_id="clear.request-1",
        reason="用户选择清空新对话长期记忆。",
        confirmed=True,
    )
    duplicate = service.clear(
        request_id="clear.request-1",
        reason="用户选择清空新对话长期记忆。",
        confirmed=True,
    )
    assert result.status is MemoryAdminMutationStatus.APPLIED
    assert result.affected_count == 2
    assert duplicate.status is MemoryAdminMutationStatus.DUPLICATE
    assert memory.records == {}
    assert memory.operations == [("delete", "memory-1"), ("delete", "memory-2")]


def test_clear_waits_for_an_inflight_lifecycle_write_before_deleting(tmp_path: Path) -> None:
    memory = FakeMemory()
    _seed(memory)
    service = _service(tmp_path, memory)
    write_entered = threading.Event()
    release_write = threading.Event()
    clear_finished = threading.Event()

    def write() -> None:
        write_entered.set()
        assert release_write.wait(2.0)
        memory.add_manual_memory(
            "synthetic canonical fact",
            user_id="local-user",
            source_id="reply:synthetic:1",
        )

    writer = threading.Thread(target=lambda: service.run_write(write))
    cleared: list[object] = []

    def clear() -> None:
        cleared.append(
            service.clear(
                request_id="clear.lifecycle.1",
                reason="用户确认清空当前长期记忆。",
                confirmed=True,
            )
        )
        clear_finished.set()

    writer.start()
    assert write_entered.wait(2.0)
    clearer = threading.Thread(target=clear)
    clearer.start()
    assert not clear_finished.wait(0.1)
    release_write.set()
    writer.join(2.0)
    clearer.join(2.0)

    assert not writer.is_alive()
    assert not clearer.is_alive()
    assert clear_finished.is_set()
    assert cleared[0].status is MemoryAdminMutationStatus.APPLIED
    assert memory.records == {}


def test_clear_works_while_memory_writes_are_paused(tmp_path: Path) -> None:
    memory = FakeMemory()
    _seed(memory)
    service = _service(tmp_path, memory)
    service.pause(
        request_id="pause.before.clear.1",
        reason="用户先暂停长期记忆写入。",
    )

    cleared = service.clear(
        request_id="clear.while-paused.1",
        reason="用户确认清空当前长期记忆。",
        confirmed=True,
    )

    assert cleared.status is MemoryAdminMutationStatus.APPLIED
    assert memory.records == {}
    assert service.is_paused() is True


def test_clear_request_ids_are_durable_per_normalized_user(tmp_path: Path) -> None:
    memory = FakeMemory()
    memory.add_manual_memory(
        "synthetic fact for user A",
        user_id="user-a",
        source_id="manual:user-a",
    )
    memory.add_manual_memory(
        "synthetic fact for user B",
        user_id="user-b",
        source_id="manual:user-b",
    )
    audit = tmp_path / "memory" / "admin.sqlite3"
    user_a = ConversationMemoryAdminService(memory, audit, user_id="User-A")
    user_b = ConversationMemoryAdminService(memory, audit, user_id="user-b")

    first = user_a.clear(
        request_id="clear.shared-request.1",
        reason="用户 A 确认清空当前长期记忆。",
        confirmed=True,
    )
    retry = ConversationMemoryAdminService(memory, audit, user_id="user-a").clear(
        request_id="clear.shared-request.1",
        reason="用户 A 确认清空当前长期记忆。",
        confirmed=True,
    )
    other_user = user_b.clear(
        request_id="clear.shared-request.1",
        reason="用户 B 使用相同请求编号确认清空。",
        confirmed=True,
    )

    assert first.status is MemoryAdminMutationStatus.APPLIED
    assert retry.status is MemoryAdminMutationStatus.DUPLICATE
    assert other_user.status is MemoryAdminMutationStatus.APPLIED
    assert memory.records == {}
    assert memory.operations.count(("delete", "memory-1")) == 1
    assert memory.operations.count(("delete", "memory-2")) == 1


def test_clear_recovers_durable_pending_intent_without_deleting_later_memory(
    tmp_path: Path,
) -> None:
    memory = FakeMemory()
    _seed(memory)
    service = _service(tmp_path, memory)
    with sqlite3.connect(service.audit_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_clear_terminal_audit
            BEFORE INSERT ON memory_admin_operations
            WHEN NEW.operation = 'clear' AND NEW.status = 'completed'
            BEGIN SELECT RAISE(FAIL, 'synthetic clear terminal ledger failure'); END
            """
        )

    with pytest.raises(ConversationMemoryAdminError, match="MEMORY_ADMIN_AUDIT_UNAVAILABLE"):
        service.clear(
            request_id="clear.pending-recovery.1",
            reason="用户确认清空当前长期记忆。",
            confirmed=True,
        )
    assert service.run_write(lambda: "synthetic write must be blocked") is None
    memory.add_manual_memory(
        "synthetic fact added after the failed terminal audit",
        user_id="local-user",
        source_id="manual:after-clear-audit-failure",
    )
    with sqlite3.connect(service.audit_path) as connection:
        connection.execute("DROP TRIGGER fail_clear_terminal_audit")
    with pytest.raises(ConversationMemoryAdminError, match="MEMORY_ADMIN_CLEAR_FAILED"):
        ConversationMemoryAdminService(memory, service.audit_path).clear(
            request_id="clear.pending-recovery.1",
            reason="用户确认清空当前长期记忆。",
            confirmed=True,
        )

    assert len(memory.records) == 1
    assert next(iter(memory.records.values())).source_id == "manual:after-clear-audit-failure"


def test_admin_request_id_is_bound_to_the_normalized_payload(tmp_path: Path) -> None:
    service = _service(tmp_path, FakeMemory())
    service.clear(
        request_id="clear.payload-bound.1",
        reason="synthetic confirmation one",
        confirmed=True,
    )

    with pytest.raises(ConversationMemoryAdminError, match="MEMORY_ADMIN_REQUEST_CONFLICT"):
        service.clear(
            request_id="clear.payload-bound.1",
            reason="synthetic confirmation two",
            confirmed=True,
        )


def test_status_counts_only_the_normalized_current_user_audit_rows(tmp_path: Path) -> None:
    memory = FakeMemory()
    audit = tmp_path / "memory" / "admin.sqlite3"
    user_a = ConversationMemoryAdminService(memory, audit, user_id="User-A")
    user_b = ConversationMemoryAdminService(memory, audit, user_id="user-b")
    user_a.pause(request_id="pause.user-a.1", reason="用户 A 暂停长期记忆。")
    with sqlite3.connect(audit) as connection:
        connection.execute(
            """
            INSERT INTO memory_admin_operations (
                user_id, request_id, operation, payload_fingerprint,
                target_memory_id, target_memory_ids,
                replacement_memory_id, replacement_source_id,
                status, affected_count, reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, 0, ?, ?, ?)
            """,
            (
                "user-a",
                "correct.user-a.pending.1",
                "correct",
                "synthetic-fingerprint",
                "replacement_written_delete_pending",
                "synthetic pending correction",
                "2026-08-26T00:00:00+00:00",
                "2026-08-26T00:00:00+00:00",
            ),
        )

    assert user_a.status().audit_count == 2
    assert user_a.status().pending_correction_count == 1
    assert user_b.status().audit_count == 0
    assert user_b.status().pending_correction_count == 0
    user_b.pause(request_id="pause.user-b.1", reason="用户 B 暂停长期记忆。")
    assert user_a.status().audit_count == 2
    assert user_b.status().audit_count == 1


@pytest.mark.parametrize(
    ("provider_status", "expected"),
    [
        ("disabled", "MEMORY_ADMIN_DISABLED"),
        ("unavailable", "MEMORY_ADMIN_UNAVAILABLE"),
    ],
)
def test_disabled_or_unavailable_provider_rejects_user_operations(
    tmp_path: Path,
    provider_status: str,
    expected: str,
) -> None:
    service = _service(tmp_path, FakeMemory(provider_status=provider_status))
    with pytest.raises(ConversationMemoryAdminError) as raised:
        service.list_memories()
    assert raised.value.code == expected


def test_request_id_cannot_be_reused_for_another_operation(tmp_path: Path) -> None:
    memory = FakeMemory()
    service = _service(tmp_path, memory)
    added = service.add(
        SECRET_NEW,
        request_id="shared.request-1",
        reason="添加一条明确事实。",
    )

    with pytest.raises(ConversationMemoryAdminError) as conflict:
        service.delete(
            added.replacement_memory_id,
            request_id="shared.request-1",
            reason="错误地复用请求编号。",
        )
    assert conflict.value.code == "MEMORY_ADMIN_REQUEST_CONFLICT"
    assert added.replacement_memory_id in memory.records


def test_unsupported_audit_schema_fails_closed(tmp_path: Path) -> None:
    audit = tmp_path / "admin.sqlite3"
    with sqlite3.connect(audit) as connection:
        connection.execute("PRAGMA user_version=99")

    with pytest.raises(ConversationMemoryAdminError) as raised:
        ConversationMemoryAdminService(FakeMemory(), audit)
    assert raised.value.code == "MEMORY_ADMIN_SCHEMA_UNSUPPORTED"
