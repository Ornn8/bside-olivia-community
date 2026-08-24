from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from conversation_memory_port import (
    ConversationMemoryRecord,
    ConversationMemoryStatus,
)
import installed_memory_runtime
from memory_port import LEGACY_LETTERS, MemoryRecord
from memory_prompt import MemoryPromptBuilder


NOW = datetime(2026, 8, 24, tzinfo=timezone.utc)


class Archive:
    enabled = True

    def status(self):
        return {"status": "available", "enabled": True}

    def search(self, _query, *, domains=None, limit=8):
        del limit
        row = MemoryRecord(
            memory_id="legacy-1",
            domain=LEGACY_LETTERS,
            text="只读旧信。",
            source="archive",
            created_at=1,
            provenance={"domain": LEGACY_LETTERS, "read_only": True},
        )
        return [row] if domains is None or LEGACY_LETTERS in domains else []


class Current:
    enabled = True
    config = SimpleNamespace(user_id="local-user")

    def status(self):
        return ConversationMemoryStatus(
            "available",
            True,
            "mem0",
            "qdrant-local",
            memory_count=1,
        )

    def search_context(self, _query, *, user_id, limit):
        del limit
        return (
            ConversationMemoryRecord(
                memory_id="current-1",
                text="用户现在住在东京。",
                user_id=user_id,
                source_id="reply:fixture:1",
                score=0.9,
                occurred_at=NOW,
                created_at=NOW,
            ),
        )


def test_normal_install_marker_selects_verified_adapter(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OLIVIA_MEMORY_INSTALLED_RUNTIME", "1")
    monkeypatch.setattr(
        installed_memory_runtime,
        "create_installed_mem0_adapter",
        lambda: Current(),
    )
    prompt = MemoryPromptBuilder(Archive()).build("住在哪里", max_chars=2400)
    assert "用户现在住在东京" in prompt.text
    assert "只读旧信" in prompt.text


def test_installed_adapter_failure_never_breaks_prompt_creation(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OLIVIA_MEMORY_INSTALLED_RUNTIME", "1")

    def broken():
        raise RuntimeError("private provider detail")

    monkeypatch.setattr(
        installed_memory_runtime,
        "create_installed_mem0_adapter",
        broken,
    )
    prompt = MemoryPromptBuilder(Archive()).build("住在哪里", max_chars=2400)
    assert "只读旧信" in prompt.text
    assert "private provider detail" not in prompt.text
