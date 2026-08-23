from __future__ import annotations

from datetime import datetime, timezone

import pytest

from conversation_memory_port import (
    ConversationMemoryError,
    ConversationMemoryRecord,
    ConversationMemoryStatus,
    MemoryWriteResult,
    MemoryWriteStatus,
    NullConversationMemoryPort,
    UnavailableConversationMemoryPort,
)


NOW = datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)


def test_record_exposes_only_bounded_prompt_fields() -> None:
    record = ConversationMemoryRecord(
        memory_id="memory.fixture.1",
        text="用户目前在东京工作。",
        user_id="local-user",
        source_id="letter:fixture:1",
        score=0.87654321,
        occurred_at=NOW,
        created_at=NOW,
        metadata={"category": "personal_info", "canonical": True},
    )

    assert record.to_prompt_dict() == {
        "memory_id": "memory.fixture.1",
        "text": "用户目前在东京工作。",
        "source_id": "letter:fixture:1",
        "domain": "conversation_memory",
        "score": 0.876543,
        "occurred_at": NOW.isoformat(),
        "created_at": NOW.isoformat(),
    }
    assert "user_id" not in record.to_prompt_dict()
    assert "metadata" not in record.to_prompt_dict()


@pytest.mark.parametrize(
    "metadata",
    [
        {"private_world": "hidden"},
        {"hidden_scores": 100},
        {"system_prompt": "secret"},
        {"credential": "secret"},
        {"Bad-Key": "value"},
        {"nested": {"not": "scalar"}},
    ],
)
def test_record_rejects_reserved_or_unbounded_metadata(metadata) -> None:
    with pytest.raises(ConversationMemoryError):
        ConversationMemoryRecord(
            "memory.fixture.1",
            "用户喜欢黑咖啡。",
            "local-user",
            "letter:fixture:1",
            metadata=metadata,
        )


def test_record_rejects_naive_time_and_invalid_score() -> None:
    with pytest.raises(ConversationMemoryError):
        ConversationMemoryRecord(
            "memory.fixture.1",
            "用户喜欢黑咖啡。",
            "local-user",
            "letter:fixture:1",
            occurred_at=datetime(2026, 8, 23),
        )
    with pytest.raises(ConversationMemoryError):
        ConversationMemoryRecord(
            "memory.fixture.1",
            "用户喜欢黑咖啡。",
            "local-user",
            "letter:fixture:1",
            score=1.1,
        )


def test_write_result_requires_stable_unavailable_error() -> None:
    written = MemoryWriteResult(
        MemoryWriteStatus.WRITTEN,
        "letter:fixture:1",
        ("memory.fixture.1",),
    )
    assert written.status is MemoryWriteStatus.WRITTEN

    unavailable = MemoryWriteResult(
        MemoryWriteStatus.UNAVAILABLE,
        "letter:fixture:1",
        error_code="MEM0_UNAVAILABLE",
    )
    assert unavailable.error_code == "MEM0_UNAVAILABLE"

    with pytest.raises(ConversationMemoryError):
        MemoryWriteResult(
            MemoryWriteStatus.UNAVAILABLE,
            "letter:fixture:1",
        )
    with pytest.raises(ConversationMemoryError):
        MemoryWriteResult(
            MemoryWriteStatus.WRITTEN,
            "letter:fixture:1",
            error_code="MEM0_UNAVAILABLE",
        )


def test_null_port_is_non_blocking_and_contains_no_records() -> None:
    port = NullConversationMemoryPort()

    assert port.search_context("东京", user_id="local-user", limit=3) == ()
    assert port.list_memories(user_id="local-user") == ()
    assert port.delete_memory("memory.fixture.1", user_id="local-user") is False
    assert port.clear_user(user_id="local-user") == 0
    assert port.remember_exchange(
        user_message="我在东京工作。",
        assistant_message="记住了。",
        occurred_at=NOW,
        source_id="letter:fixture:1",
        user_id="local-user",
    ) == MemoryWriteResult(MemoryWriteStatus.SKIPPED, "letter:fixture:1")
    assert port.status() == ConversationMemoryStatus(
        "disabled",
        False,
        "none",
        "none",
    )
    assert port.export_user(user_id="local-user")["records"] == []

    with pytest.raises(RuntimeError, match="CONVERSATION_MEMORY_DISABLED"):
        port.add_manual_memory(
            "用户在东京工作。",
            user_id="local-user",
            source_id="manual:fixture:1",
        )


def test_unavailable_port_degrades_without_echoing_messages() -> None:
    port = UnavailableConversationMemoryPort("MEM0_IMPORT_FAILED")
    result = port.remember_exchange(
        user_message="private user text",
        assistant_message="private assistant text",
        occurred_at=NOW,
        source_id="letter:fixture:2",
        user_id="local-user",
    )

    assert result == MemoryWriteResult(
        MemoryWriteStatus.UNAVAILABLE,
        "letter:fixture:2",
        error_code="MEM0_IMPORT_FAILED",
    )
    assert port.status().to_dict() == {
        "status": "unavailable",
        "enabled": False,
        "provider": "none",
        "storage": "none",
        "reason_code": "MEM0_IMPORT_FAILED",
    }
    assert "private user text" not in repr(result)
    assert "private assistant text" not in repr(result)
