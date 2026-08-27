from __future__ import annotations

from datetime import datetime, timezone
import asyncio
import json
from pathlib import Path
import time
import threading
import sqlite3
from types import SimpleNamespace

import pytest

from companion_memory_context import CompanionMemoryPromptBuilder
from conversation_memory_admin import (
    ConversationMemoryAdminError,
    ConversationMemoryAdminService,
    MEMORY_ADMIN_AUDIT_SCHEMA,
    MemoryAdminMutationStatus,
)
from conversation_memory_port import ConversationMemoryRecord, ConversationMemoryStatus
from runtime.memory.conversation_memory_delivery import CanonicalMemoryDelivery, ConversationMemoryDeliveryCommitter
from conversation_memory_runtime import (
    ensure_conversation_memory_runtime,
    stop_conversation_memory_runtime,
)
from memory_port import LEGACY_LETTERS, MemoryRecord
from memory_prompt import MemoryPromptBuilder


class Archive:
    enabled = True
    conversation_enabled = False

    def status(self):
        return {"status": "available", "enabled": True, "provider": "sqlite"}

    def search(self, query, *, domains=None, limit=8):
        del query, domains, limit
        return [
            MemoryRecord(
                memory_id="archive.1",
                domain=LEGACY_LETTERS,
                text="Archive 原文必须始终可用。",
                source="legacy-import",
                created_at=1,
                provenance={"domain": LEGACY_LETTERS},
            )
        ]


class Mem0:
    enabled = True

    def __init__(self) -> None:
        self.searches = 0
        self.writes: list[str] = []

    def status(self) -> ConversationMemoryStatus:
        return ConversationMemoryStatus(
            "available", True, "mem0", "qdrant-local", memory_count=1
        )

    def search_context(self, query, *, user_id, limit):
        del query, user_id, limit
        self.searches += 1
        return (
            ConversationMemoryRecord(
                memory_id="mem0.1",
                text="这条 Mem0 事实在暂停时绝不能进入 Prompt。",
                user_id="local-user",
                source_id="reply:old:1",
                created_at=datetime.now(timezone.utc),
            ),
        )

    def remember_exchange(self, **kwargs):
        self.writes.append(str(kwargs["source_id"]))
        from conversation_memory_port import MemoryWriteResult, MemoryWriteStatus

        return MemoryWriteResult(
            MemoryWriteStatus.WRITTEN,
            str(kwargs["source_id"]),
            ("mem0.new",),
        )

    def list_memories(self, *, user_id, limit):
        del user_id, limit
        return ()

    def clear_user(self, *, user_id):
        del user_id
        return 0


class BlockingMem0(Mem0):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def remember_exchange(self, **kwargs):
        self.entered.set()
        assert self.release.wait(2.0)
        return super().remember_exchange(**kwargs)


class ConfiguredMem0(Mem0):
    def __init__(self, root: Path) -> None:
        super().__init__()
        self.config = SimpleNamespace(
            user_id="local-user",
            outbox_data_root=root,
            data_root=root / "memory" / "mem0",
        )


class FinalGateInterleaving:
    """Pause/resume only after the worker's initial lifecycle check."""

    def __init__(self, admin: ConversationMemoryAdminService) -> None:
        self.admin = admin
        self.initial_check_complete = threading.Event()
        self.allow_final_gate = threading.Event()

    def blocks_delivery(self, occurred_at: datetime) -> bool:
        del occurred_at
        self.initial_check_complete.set()
        assert self.allow_final_gate.wait(2.0)
        return False

    def run_write(self, operation, occurred_at: datetime | None = None):
        if occurred_at is None:
            return self.admin.run_write(operation)
        return self.admin.run_write(operation, occurred_at=occurred_at)


def _write_state(root: Path, letter_id: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "state.json").write_text(
        json.dumps(
            {
                "letters": [
                    {
                        "letter_id": letter_id,
                        "letter_status": "COMPLETED",
                        "reply_revision": 1,
                        "content": "canonical user message",
                        "reply_text": "canonical assistant reply",
                        "private_world_occurred_at": datetime.now(timezone.utc).isoformat(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _write_prepaused_state(root: Path, letter_id: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "state.json").write_text(
        json.dumps(
            {
                "letters": [
                    {
                        "letter_id": letter_id,
                        "letter_status": "COMPLETED",
                        "reply_revision": 1,
                        "content": "canonical before pause",
                        "reply_text": "reply before pause",
                        "private_world_occurred_at": datetime.now(timezone.utc).isoformat(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _wait_for(predicate) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not reached")


def test_pause_persists_blocks_mem0_only_and_never_backfills_paused_letters(
    tmp_path: Path,
) -> None:
    stop_conversation_memory_runtime()
    root = tmp_path / "data"
    memory = Mem0()
    admin = ConversationMemoryAdminService(memory, root / "memory" / "memory_admin_audit.sqlite3")
    admin.pause(request_id="memory.pause.1", reason="用户暂停长期记忆。")
    with pytest.raises(ConversationMemoryAdminError, match="MEMORY_ADMIN_PAUSED"):
        admin.list_memories()

    prompt = CompanionMemoryPromptBuilder(
        Archive(), memory, memory_lifecycle=admin
    ).build("测试", max_chars=1200)
    assert "Archive 原文必须始终可用。" in prompt.text
    assert "Mem0 事实" not in prompt.text
    assert memory.searches == 0

    _write_state(root, "letter.paused.1")
    paused_runtime = ensure_conversation_memory_runtime(
        Archive(), memory, environ={"OLIVIA_LOCAL_DATA_ROOT": str(root), "OLIVIA_MEMORY_OUTBOX_INTERVAL_SECONDS": "0.25"}, memory_lifecycle=admin
    )
    assert paused_runtime.reason_code == "MEMORY_ADMIN_PAUSED"
    assert paused_runtime.worker_running is True
    time.sleep(0.35)
    assert memory.writes == []

    restarted = ConversationMemoryAdminService(memory, root / "memory" / "memory_admin_audit.sqlite3")
    assert restarted.status().paused is True
    restarted.resume(request_id="memory.resume.1", reason="用户恢复长期记忆。")
    ensure_conversation_memory_runtime(
        Archive(), memory, environ={"OLIVIA_LOCAL_DATA_ROOT": str(root)}, memory_lifecycle=restarted
    )
    time.sleep(0.35)
    assert memory.writes == []

    _write_state(root, "letter.future.1")
    _wait_for(lambda: memory.writes == ["reply:letter.future.1:1"])
    stop_conversation_memory_runtime()


def test_pause_blocks_preexisting_canonical_delivery_until_it_is_recorded_terminal(
    tmp_path: Path,
) -> None:
    stop_conversation_memory_runtime()
    root = tmp_path / "data"
    memory = Mem0()
    _write_prepaused_state(root, "letter.before.pause.1")
    admin = ConversationMemoryAdminService(memory, root / "memory" / "memory_admin_audit.sqlite3")
    admin.pause(request_id="memory.pause.before.1", reason="用户暂停长期记忆。")
    ensure_conversation_memory_runtime(
        Archive(), memory,
        environ={"OLIVIA_LOCAL_DATA_ROOT": str(root), "OLIVIA_MEMORY_OUTBOX_INTERVAL_SECONDS": "0.25"},
        memory_lifecycle=admin,
    )
    time.sleep(0.35)
    assert memory.writes == []
    stop_conversation_memory_runtime()
    admin.resume(request_id="memory.resume.before.1", reason="用户恢复长期记忆。")
    ensure_conversation_memory_runtime(
        Archive(),
        memory,
        environ={"OLIVIA_LOCAL_DATA_ROOT": str(root)},
        memory_lifecycle=admin,
    )
    time.sleep(0.35)
    assert memory.writes == []
    stop_conversation_memory_runtime()


def test_fast_pause_resume_tombstones_an_undelivered_old_canonical_reply(
    tmp_path: Path,
) -> None:
    memory = Mem0()
    admin = ConversationMemoryAdminService(memory, tmp_path / "memory_admin_audit.sqlite3")
    delivery = CanonicalMemoryDelivery(
        "letter.prepause.fast-resume",
        1,
        "synthetic user",
        "synthetic reply",
        datetime.now(timezone.utc),
    )
    admin.pause(request_id="memory.pause.fast.1", reason="用户暂停长期记忆。")
    admin.resume(request_id="memory.resume.fast.1", reason="用户恢复长期记忆。")

    result = asyncio.run(
        ConversationMemoryDeliveryCommitter(memory, memory_lifecycle=admin).commit(delivery)
    )

    assert result.status.value == "skipped"
    assert memory.writes == []


def test_final_gate_rechecks_old_delivery_after_complete_pause_resume(
    tmp_path: Path,
) -> None:
    memory = Mem0()
    admin = ConversationMemoryAdminService(memory, tmp_path / "memory_admin_audit.sqlite3")
    lifecycle = FinalGateInterleaving(admin)
    delivery = CanonicalMemoryDelivery(
        "letter.final-gate.gap",
        1,
        "synthetic user",
        "synthetic reply",
        datetime.now(timezone.utc),
    )
    result: list[object] = []
    writer = threading.Thread(
        target=lambda: result.append(
            asyncio.run(
                ConversationMemoryDeliveryCommitter(
                    memory, memory_lifecycle=lifecycle
                ).commit(delivery)
            )
        )
    )

    writer.start()
    assert lifecycle.initial_check_complete.wait(2.0)
    admin.pause(request_id="memory.pause.final-gate.1", reason="用户暂停长期记忆。")
    admin.resume(request_id="memory.resume.final-gate.1", reason="用户恢复长期记忆。")
    lifecycle.allow_final_gate.set()
    writer.join(2.0)

    assert not writer.is_alive()
    assert result[0].status.value == "skipped"
    assert memory.writes == []


def test_pause_windows_are_isolated_by_normalized_user_id(tmp_path: Path) -> None:
    memory = Mem0()
    audit = tmp_path / "memory_admin_audit.sqlite3"
    user_a = ConversationMemoryAdminService(memory, audit, user_id="User-A")
    same_user_a = ConversationMemoryAdminService(memory, audit, user_id="user-a")
    user_b = ConversationMemoryAdminService(memory, audit, user_id="user-b")
    delivery_time = datetime.now(timezone.utc)

    user_a.pause(request_id="memory.pause.user-a.1", reason="用户 A 暂停长期记忆。")

    assert user_a.is_paused() is True
    assert same_user_a.is_paused() is True
    assert user_b.is_paused() is False
    assert user_b.blocks_delivery(delivery_time) is False


def test_lifecycle_noops_are_persisted_as_terminal_requests(tmp_path: Path) -> None:
    memory = Mem0()
    audit = tmp_path / "memory_admin_audit.sqlite3"
    admin = ConversationMemoryAdminService(memory, audit)
    assert admin.pause(
        request_id="memory.pause.active.1", reason="用户暂停长期记忆。"
    ).status is MemoryAdminMutationStatus.APPLIED
    assert admin.pause(
        request_id="memory.pause.noop.1", reason="用户再次暂停长期记忆。"
    ).status is MemoryAdminMutationStatus.NOOP

    restarted = ConversationMemoryAdminService(memory, audit)
    assert restarted.pause(
        request_id="memory.pause.noop.1", reason="用户再次暂停长期记忆。"
    ).status is MemoryAdminMutationStatus.DUPLICATE
    with pytest.raises(ConversationMemoryAdminError, match="MEMORY_ADMIN_REQUEST_CONFLICT"):
        restarted.resume(
            request_id="memory.pause.noop.1", reason="不能跨 operation 复用请求。"
        )

    assert restarted.resume(
        request_id="memory.resume.active.1", reason="用户恢复长期记忆。"
    ).status is MemoryAdminMutationStatus.APPLIED
    assert restarted.resume(
        request_id="memory.resume.noop.1", reason="用户再次恢复长期记忆。"
    ).status is MemoryAdminMutationStatus.NOOP
    restarted = ConversationMemoryAdminService(memory, audit)
    assert restarted.resume(
        request_id="memory.resume.noop.1", reason="用户再次恢复长期记忆。"
    ).status is MemoryAdminMutationStatus.DUPLICATE


def test_resume_rolls_back_pause_window_when_its_ledger_write_fails(
    tmp_path: Path,
) -> None:
    memory = Mem0()
    audit = tmp_path / "memory_admin_audit.sqlite3"
    admin = ConversationMemoryAdminService(memory, audit)
    admin.pause(request_id="memory.pause.ledger-fault.1", reason="用户暂停长期记忆。")
    with sqlite3.connect(audit) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_resume_ledger
            BEFORE INSERT ON memory_admin_operations
            WHEN NEW.operation = 'resume'
            BEGIN SELECT RAISE(FAIL, 'synthetic ledger failure'); END
            """
        )

    with pytest.raises(ConversationMemoryAdminError, match="MEMORY_ADMIN_AUDIT_UNAVAILABLE"):
        admin.resume(request_id="memory.resume.ledger-fault.1", reason="用户恢复长期记忆。")

    writes: list[str] = []
    assert admin.is_paused() is True
    assert admin.run_write(lambda: writes.append("provider")) is None
    with sqlite3.connect(audit) as connection:
        connection.execute("DROP TRIGGER fail_resume_ledger")
    restarted = ConversationMemoryAdminService(memory, audit)
    assert restarted.resume(
        request_id="memory.resume.ledger-fault.1", reason="重试同一恢复请求。"
    ).status is MemoryAdminMutationStatus.APPLIED
    assert restarted.run_write(lambda: writes.append("provider")) is None
    assert writes == ["provider"]


def test_operation_ledger_migrates_legacy_rows_to_default_user_and_isolates_request_ids(
    tmp_path: Path,
) -> None:
    memory = Mem0()
    audit = tmp_path / "memory_admin_audit.sqlite3"
    with sqlite3.connect(audit) as connection:
        connection.executescript(
            """
            CREATE TABLE memory_admin_operations (
                request_id TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                target_memory_id TEXT,
                replacement_memory_id TEXT,
                replacement_source_id TEXT,
                status TEXT NOT NULL,
                affected_count INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO memory_admin_operations VALUES (
                'memory.pause.shared.1', 'pause', NULL, NULL, NULL,
                'completed', 0, 'legacy local operation',
                '2026-08-26T00:00:00+00:00', '2026-08-26T00:00:00+00:00'
            );
            PRAGMA user_version=4;
            """
        )

    default_user = ConversationMemoryAdminService(memory, audit)
    other_user = ConversationMemoryAdminService(memory, audit, user_id="user-b")

    with pytest.raises(ConversationMemoryAdminError, match="MEMORY_ADMIN_REQUEST_CONFLICT"):
        default_user.pause(
            request_id="memory.pause.shared.1", reason="重试旧本地请求。"
        )
    assert other_user.pause(
        request_id="memory.pause.shared.1", reason="另一位用户暂停长期记忆。"
    ).status is MemoryAdminMutationStatus.APPLIED
    assert other_user.is_paused() is True


@pytest.mark.parametrize("version", (1, 4))
def test_operation_ledger_migration_rolls_back_copy_fault_and_retries_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: int,
) -> None:
    memory = Mem0()
    audit = tmp_path / "memory_admin_audit.sqlite3"
    with sqlite3.connect(audit) as connection:
        connection.executescript(
            """
            CREATE TABLE memory_admin_operations (
                request_id TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                target_memory_id TEXT,
                replacement_memory_id TEXT,
                replacement_source_id TEXT,
                status TEXT NOT NULL,
                affected_count INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO memory_admin_operations VALUES (
                'memory.pause.migration.1', 'pause', NULL, NULL, NULL,
                'completed', 0, 'legacy local operation',
                '2026-08-26T00:00:00+00:00', '2026-08-26T00:00:00+00:00'
            );
            """
        )
        if version == 4:
            connection.execute(
                """
                CREATE TABLE memory_admin_pause_windows (
                    user_id TEXT NOT NULL,
                    pause_request_id TEXT NOT NULL,
                    resume_request_id TEXT,
                    started_at TEXT NOT NULL,
                    resumed_at TEXT,
                    PRIMARY KEY (user_id, pause_request_id),
                    UNIQUE (user_id, resume_request_id)
                )
                """
            )
        connection.execute(f"PRAGMA user_version={version}")

    original_connect = ConversationMemoryAdminService._connect
    faulted = False

    class CopyFaultConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def execute(self, statement: str, parameters=()):
            nonlocal faulted
            if not faulted and statement.lstrip().startswith("INSERT INTO memory_admin_operations"):
                faulted = True
                raise sqlite3.OperationalError("synthetic migration copy fault")
            return self.connection.execute(statement, parameters)

        def __getattr__(self, name: str):
            return getattr(self.connection, name)

    def failing_connect(service: ConversationMemoryAdminService):
        return CopyFaultConnection(original_connect(service))

    monkeypatch.setattr(ConversationMemoryAdminService, "_connect", failing_connect)
    with pytest.raises(ConversationMemoryAdminError, match="MEMORY_ADMIN_INITIALIZATION_FAILED"):
        ConversationMemoryAdminService(memory, audit)
    monkeypatch.setattr(ConversationMemoryAdminService, "_connect", original_connect)

    with sqlite3.connect(audit) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "memory_admin_operations" in names
        assert "memory_admin_operations_legacy" not in names
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version
        assert connection.execute(
            "SELECT request_id FROM memory_admin_operations"
        ).fetchone()[0] == "memory.pause.migration.1"

    restarted = ConversationMemoryAdminService(memory, audit)
    with pytest.raises(ConversationMemoryAdminError, match="MEMORY_ADMIN_REQUEST_CONFLICT"):
        restarted.pause(
            request_id="memory.pause.migration.1", reason="重试旧本地请求。"
        )
    with sqlite3.connect(audit) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == MEMORY_ADMIN_AUDIT_SCHEMA


def test_pause_waits_for_a_provider_write_already_past_the_final_gate(tmp_path: Path) -> None:
    memory = BlockingMem0()
    admin = ConversationMemoryAdminService(memory, tmp_path / "memory_admin_audit.sqlite3")
    committer = ConversationMemoryDeliveryCommitter(memory, memory_lifecycle=admin)
    delivery = CanonicalMemoryDelivery("letter.race.1", 1, "synthetic user", "synthetic reply", datetime.now(timezone.utc))
    writer = threading.Thread(target=lambda: asyncio.run(committer.commit(delivery)))
    writer.start()
    assert memory.entered.wait(2.0)
    pause_done = threading.Event()
    pauser = threading.Thread(target=lambda: (admin.pause(request_id="memory.pause.race.1", reason="用户暂停长期记忆。"), pause_done.set()))
    pauser.start()
    time.sleep(0.05)
    assert not pause_done.is_set()
    memory.release.set()
    writer.join(2.0)
    pauser.join(2.0)
    assert pause_done.is_set()
    assert memory.writes == ["reply:letter.race.1:1"]


def test_unsupported_lifecycle_schema_fails_closed_for_mem0_prompt_retrieval(tmp_path: Path) -> None:
    root = tmp_path / "data"
    audit = root / "memory" / "memory_admin_audit.sqlite3"
    audit.parent.mkdir(parents=True)
    with sqlite3.connect(audit) as connection:
        connection.execute("PRAGMA user_version=99")
    memory = ConfiguredMem0(root)
    prompt = MemoryPromptBuilder(Archive(), conversation_memory=memory).build("synthetic", max_chars=1200)
    assert memory.searches == 0
    assert "Archive 原文必须始终可用。" in prompt.text


def test_unsupported_lifecycle_schema_fails_closed_for_mem0_delivery(tmp_path: Path) -> None:
    root = tmp_path / "data"
    audit = root / "memory" / "memory_admin_audit.sqlite3"
    audit.parent.mkdir(parents=True)
    with sqlite3.connect(audit) as connection:
        connection.execute("PRAGMA user_version=99")
    memory = ConfiguredMem0(root)
    builder = MemoryPromptBuilder(Archive(), conversation_memory=memory)
    delivery = CanonicalMemoryDelivery(
        "letter.schema.1",
        1,
        "synthetic user",
        "synthetic reply",
        datetime.now(timezone.utc),
    )

    result = asyncio.run(
        ConversationMemoryDeliveryCommitter(
            memory,
            memory_lifecycle=builder.memory_lifecycle,
        ).commit(delivery)
    )

    assert result.status.value == "unavailable"
    assert result.error_code == "MEMORY_ADMIN_AUDIT_UNAVAILABLE"
    assert memory.writes == []
