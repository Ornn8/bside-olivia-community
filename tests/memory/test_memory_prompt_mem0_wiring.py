from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import mem0_memory
from conversation_memory_port import (
    ConversationMemoryRecord,
    ConversationMemoryStatus,
)
from memory_port import CONVERSATION_MEMORY, LEGACY_LETTERS, MemoryRecord
from memory_prompt import MemoryPromptBuilder


NOW = datetime(2026, 8, 23, 4, 30, tzinfo=timezone.utc)


class ArchiveMemory:
    enabled = True

    def __init__(self, *, source_id: str = "legacy-1") -> None:
        self.source_id = source_id

    def status(self):
        return {"status": "available", "enabled": True, "provider": "sqlite"}

    def search(self, query, *, domains=None, limit=8):
        del query, limit
        rows = [
            MemoryRecord(
                memory_id="sqlite-current",
                domain=CONVERSATION_MEMORY,
                text="旧 SQLite 当前事实。",
                source="sqlite",
                created_at=1,
                provenance={"domain": CONVERSATION_MEMORY},
            ),
            MemoryRecord(
                memory_id="archive-reference",
                domain=LEGACY_LETTERS,
                text="旧信只读证据。",
                source="archive",
                created_at=1,
                provenance={
                    "domain": LEGACY_LETTERS,
                    "source": "archive",
                    "source_record_id": self.source_id,
                    "read_only": True,
                },
            ),
        ]
        if domains is None:
            return rows
        selected = set(domains)
        return [row for row in rows if row.domain in selected]


class CurrentMemory:
    enabled = True

    def __init__(self) -> None:
        self.config = SimpleNamespace(user_id="mem0-scope")
        self.calls: list[tuple[str, str, int]] = []

    def status(self):
        return ConversationMemoryStatus(
            "available",
            True,
            "mem0",
            "qdrant-local",
            memory_count=1,
        )

    def search_context(self, query, *, user_id, limit):
        self.calls.append((query, user_id, limit))
        return (
            ConversationMemoryRecord(
                memory_id="mem0-current",
                text="Mem0 当前事实。",
                user_id=user_id,
                source_id="reply:letter-1:1",
                score=0.9,
                occurred_at=NOW,
                created_at=NOW,
            ),
        )


class UnavailableCurrentMemory:
    enabled = False

    def status(self):
        return ConversationMemoryStatus(
            "unavailable",
            False,
            "none",
            "none",
            reason_code="MEM0_IMPORT_FAILED",
        )

    def search_context(self, query, *, user_id, limit):
        del query, user_id, limit
        return ()


def test_default_memory_prompt_builder_uses_configured_mem0_adapter(monkeypatch) -> None:
    current = CurrentMemory()
    monkeypatch.setattr(mem0_memory, "create_mem0_adapter", lambda: current)

    prompt = MemoryPromptBuilder(ArchiveMemory()).build("东京", max_chars=2400)

    assert "Mem0 当前事实" in prompt.text
    assert "旧信只读证据" in prompt.text
    assert "旧 SQLite 当前事实" not in prompt.text
    assert current.calls == [("东京", "mem0-scope", 8)]
    assert prompt.domains == (CONVERSATION_MEMORY, LEGACY_LETTERS)


def test_explicit_none_keeps_internal_builder_from_recursively_loading_mem0(monkeypatch) -> None:
    def forbidden():
        raise AssertionError("Mem0 factory must not be called")

    monkeypatch.setattr(mem0_memory, "create_mem0_adapter", forbidden)

    prompt = MemoryPromptBuilder(
        ArchiveMemory(),
        conversation_memory=None,
    ).build("东京", max_chars=2400)

    assert "旧 SQLite 当前事实" in prompt.text
    assert "旧信只读证据" in prompt.text


def test_archive_source_id_collision_is_not_filtered_by_current_selector() -> None:
    prompt = MemoryPromptBuilder(
        ArchiveMemory(source_id="reply:letter-1:1"),
        conversation_memory=None,
    ).build(
        "东京",
        max_chars=2400,
        exclude_source_ids=("reply:letter-1:1",),
    )

    assert "旧信只读证据" in prompt.text
    assert any(record.domain == LEGACY_LETTERS for record in prompt.references)


def test_configured_but_unavailable_mem0_does_not_restore_stale_sqlite_current_facts() -> None:
    prompt = MemoryPromptBuilder(
        ArchiveMemory(),
        conversation_memory=UnavailableCurrentMemory(),
    ).build("东京", max_chars=2400)

    assert "旧信只读证据" in prompt.text
    assert "旧 SQLite 当前事实" not in prompt.text
    assert prompt.status == "degraded"
    assert prompt.domains == (LEGACY_LETTERS,)
