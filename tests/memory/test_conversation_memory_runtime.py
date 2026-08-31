from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from types import SimpleNamespace

from conversation_memory_port import (
    ConversationMemoryStatus,
    MemoryWriteResult,
    MemoryWriteStatus,
)
from conversation_memory_runtime import (
    conversation_memory_runtime_status,
    ensure_conversation_memory_runtime,
    stop_conversation_memory_runtime,
)
from memory_prompt import MemoryPromptBuilder


class ArchiveMemory:
    enabled = True

    def __init__(self) -> None:
        self.conversation_enabled = True

    def status(self):
        return {"status": "available", "enabled": True, "provider": "sqlite"}

    def search(self, query, *, domains=None, limit=8):
        del query, domains, limit
        return []


class ConversationMemory:
    enabled = True

    def __init__(
        self,
        status: str = "available",
        *,
        data_root: Path | None = None,
        outbox_data_root: Path | None = None,
    ) -> None:
        self.provider_status = status
        self.config = SimpleNamespace(
            user_id="local-user",
            data_root=data_root,
            outbox_data_root=outbox_data_root,
        )
        self.calls: list[dict[str, object]] = []

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
        )

    def search_context(self, query, *, user_id, limit):
        del query, user_id, limit
        return ()

    def remember_exchange(self, **kwargs: object) -> MemoryWriteResult:
        self.calls.append(dict(kwargs))
        return MemoryWriteResult(
            MemoryWriteStatus.WRITTEN,
            str(kwargs["source_id"]),
            ("memory-runtime-1",),
        )


def _write_state(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "state.json").write_text(
        json.dumps(
            {
                "letters": [
                    {
                        "letter_id": "letter-runtime-1",
                        "letter_status": "COMPLETED",
                        "reply_revision": 1,
                        "content": "用户的 canonical message。",
                        "reply_text": "林离的 canonical reply。",
                        "private_world_occurred_at": datetime(
                            2026,
                            8,
                            23,
                            6,
                            0,
                            tzinfo=timezone.utc,
                        ).isoformat(),
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not reached before timeout")


def setup_function() -> None:
    stop_conversation_memory_runtime()


def teardown_function() -> None:
    stop_conversation_memory_runtime()


def test_available_mem0_starts_one_outbox_and_disables_legacy_writes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    _write_state(root)
    archive = ArchiveMemory()
    memory = ConversationMemory()
    environment = {
        "OLIVIA_LOCAL_DATA_ROOT": str(root.resolve()),
        "OLIVIA_MEMORY_OUTBOX_INTERVAL_SECONDS": "0.25",
    }

    first = ensure_conversation_memory_runtime(
        archive,
        memory,
        environ=environment,
    )
    assert first.enabled is True
    assert archive.conversation_enabled is False
    _wait_for(lambda: len(memory.calls) == 1)
    assert memory.calls[0]["source_id"] == "reply:letter-runtime-1:1"
    assert memory.calls[0]["user_message"] == "用户的 canonical message。"
    assert memory.calls[0]["assistant_message"] == "林离的 canonical reply。"

    second = ensure_conversation_memory_runtime(
        archive,
        memory,
        environ=environment,
    )
    assert second.worker_running is True
    time.sleep(0.35)
    assert len(memory.calls) == 1
    status = conversation_memory_runtime_status()
    assert status.provider == "mem0-outbox"
    assert status.terminal_count == 1
    assert status.pending_count == 0


def test_outbox_journal_prevents_duplicate_after_runtime_restart(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write_state(root)
    environment = {
        "OLIVIA_LOCAL_DATA_ROOT": str(root.resolve()),
        "OLIVIA_MEMORY_OUTBOX_INTERVAL_SECONDS": "0.25",
    }
    first_memory = ConversationMemory()
    ensure_conversation_memory_runtime(
        ArchiveMemory(),
        first_memory,
        environ=environment,
    )
    _wait_for(lambda: len(first_memory.calls) == 1)
    stop_conversation_memory_runtime()

    second_memory = ConversationMemory()
    ensure_conversation_memory_runtime(
        ArchiveMemory(),
        second_memory,
        environ=environment,
    )
    time.sleep(0.4)
    assert second_memory.calls == []
    assert conversation_memory_runtime_status().terminal_count == 1


def test_unavailable_mem0_disables_old_conversation_write_without_starting_worker(
    tmp_path: Path,
) -> None:
    archive = ArchiveMemory()
    status = ensure_conversation_memory_runtime(
        archive,
        ConversationMemory("unavailable"),
        environ={"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path.resolve())},
    )

    assert archive.conversation_enabled is False
    assert status.status == "unavailable"
    assert status.reason_code == "MEM0_IMPORT_FAILED"
    assert status.worker_running is False


def test_disabled_mem0_preserves_existing_sqlite_conversation_behavior(
    tmp_path: Path,
) -> None:
    archive = ArchiveMemory()
    status = ensure_conversation_memory_runtime(
        archive,
        ConversationMemory("disabled"),
        environ={"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path.resolve())},
    )

    assert archive.conversation_enabled is True
    assert status.status == "disabled"
    assert status.worker_running is False


def test_missing_or_relative_data_root_never_starts_worker() -> None:
    archive = ArchiveMemory()
    missing = ensure_conversation_memory_runtime(
        archive,
        ConversationMemory(),
        environ={},
    )
    assert archive.conversation_enabled is False
    assert missing.status == "degraded"
    assert missing.reason_code == "MEMORY_OUTBOX_DATA_ROOT_NOT_CONFIGURED"

    stop_conversation_memory_runtime()
    relative = ensure_conversation_memory_runtime(
        ArchiveMemory(),
        ConversationMemory(),
        environ={"OLIVIA_LOCAL_DATA_ROOT": "relative/data"},
    )
    assert relative.status == "unavailable"
    assert relative.reason_code == "MEMORY_OUTBOX_DATA_ROOT_INVALID"


def test_explicit_mem0_adapter_root_starts_outbox_without_host_environment(
    tmp_path: Path,
) -> None:
    root = tmp_path / "configured-state-root"
    _write_state(root)
    memory = ConversationMemory(
        data_root=root / "memory" / "mem0",
        outbox_data_root=root,
    )

    status = ensure_conversation_memory_runtime(
        ArchiveMemory(),
        memory,
        environ={"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path / "host-root")},
    )

    assert status.status == "available"
    _wait_for(lambda: len(memory.calls) == 1)
    assert memory.calls[0]["source_id"] == "reply:letter-runtime-1:1"


def test_adapter_runtime_configuration_wins_over_host_outbox_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import conversation_memory_runtime as runtime_module

    root = tmp_path / "configured-state-root"
    _write_state(root)
    memory = ConversationMemory(data_root=root / "memory" / "mem0")
    memory.config.outbox_enabled = True
    memory.config.outbox_interval_seconds = 0.25
    memory.config.write_timeout_seconds = 1.25
    captured: dict[str, float] = {}
    original = runtime_module.ConversationMemoryDeliveryCommitter

    class CapturingCommitter(original):
        def __init__(self, *args, timeout_seconds: float, **kwargs) -> None:
            captured["timeout"] = timeout_seconds
            super().__init__(*args, timeout_seconds=timeout_seconds, **kwargs)

    monkeypatch.setattr(runtime_module, "ConversationMemoryDeliveryCommitter", CapturingCommitter)
    status = ensure_conversation_memory_runtime(
        ArchiveMemory(),
        memory,
        environ={
            "OLIVIA_MEMORY_OUTBOX_ENABLED": "0",
            "OLIVIA_MEMORY_WRITE_TIMEOUT_SECONDS": "299",
            "OLIVIA_MEMORY_OUTBOX_INTERVAL_SECONDS": "999",
        },
    )

    assert status.status == "available"
    assert captured["timeout"] == 1.25
    _wait_for(lambda: len(memory.calls) == 1)


def test_available_ledger_without_worker_reports_degraded_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import conversation_memory_runtime as runtime_module

    root = tmp_path / "configured-state-root"
    _write_state(root)

    status = ensure_conversation_memory_runtime(
        ArchiveMemory(),
        ConversationMemory(data_root=root / "memory" / "mem0"),
        environ={},
        start_background=False,
    )

    assert status.status == "degraded"
    assert status.worker_running is False
    assert status.reason_code == "MEMORY_OUTBOX_WORKER_NOT_RUNNING"
    runtime = runtime_module._RUNTIME
    assert runtime is not None

    def unexpected_health_probe():
        raise AssertionError("reply readiness must use the in-process snapshot")

    monkeypatch.setattr(runtime.outbox, "health", unexpected_health_probe)
    assert runtime_module.conversation_memory_reply_readiness_status() == status


def test_memory_prompt_builder_configures_runtime_but_keeps_prompt_available(
    tmp_path: Path,
    monkeypatch,
) -> None:
    archive = ArchiveMemory()
    memory = ConversationMemory()
    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path.resolve()))
    monkeypatch.setenv("OLIVIA_MEMORY_OUTBOX_ENABLED", "0")

    builder = MemoryPromptBuilder(
        archive,
        conversation_memory=memory,
    )

    assert archive.conversation_enabled is False
    assert builder.conversation_runtime_status is not None
    assert builder.conversation_runtime_status["status"] == "disabled"
    assert builder.build("synthetic", max_chars=1000).status == "available"
