from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from conversation_memory_delivery import (
    CanonicalMemoryDelivery,
    CanonicalMemoryDeliveryResult,
    CanonicalMemoryDeliveryStatus,
)
from conversation_memory_outbox import (
    CanonicalMemoryOutbox,
    ConversationMemoryOutboxError,
    OUTBOX_SCHEMA_VERSION,
)


OCCURRED_AT = "2026-08-23T05:30:00+00:00"
SECRET_USER_TEXT = "只允许留在状态文件里的用户正文。"
SECRET_REPLY_TEXT = "只允许留在状态文件里的林离回复。"


class SequencedCommitter:
    def __init__(
        self,
        statuses: list[CanonicalMemoryDeliveryStatus] | None = None,
    ) -> None:
        self.statuses = list(statuses or [CanonicalMemoryDeliveryStatus.WRITTEN])
        self.calls: list[CanonicalMemoryDelivery] = []

    async def commit(
        self,
        delivery: CanonicalMemoryDelivery,
    ) -> CanonicalMemoryDeliveryResult:
        self.calls.append(delivery)
        status = self.statuses.pop(0) if self.statuses else CanonicalMemoryDeliveryStatus.DUPLICATE
        error_code = "MEM0_WRITE_FAILED" if status is CanonicalMemoryDeliveryStatus.UNAVAILABLE else None
        return CanonicalMemoryDeliveryResult(
            status,
            delivery.source_id,
            memory_count=1 if status is CanonicalMemoryDeliveryStatus.WRITTEN else 0,
            error_code=error_code,
        )


def _state(
    path: Path,
    *,
    revision: int = 1,
    status: str = "COMPLETED",
    user_text: str = SECRET_USER_TEXT,
    reply_text: str = SECRET_REPLY_TEXT,
) -> None:
    path.write_text(
        json.dumps(
            {
                "letters": [
                    {
                        "letter_id": "letter-1",
                        "letter_status": status,
                        "reply_revision": revision,
                        "content": user_text,
                        "reply_text": reply_text,
                        "private_world_occurred_at": OCCURRED_AT,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _outbox(tmp_path: Path, committer: SequencedCommitter) -> CanonicalMemoryOutbox:
    return CanonicalMemoryOutbox(
        tmp_path / "state.json",
        tmp_path / "memory" / "delivery.sqlite3",
        committer,
    )


def test_written_delivery_is_terminal_across_rescan_and_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        state = tmp_path / "state.json"
        _state(state)
        committer = SequencedCommitter()
        outbox = _outbox(tmp_path, committer)

        first = await outbox.scan_once()
        assert first.status == "available"
        assert first.discovered == 1
        assert first.delivered == 1
        assert first.pending == 0
        assert len(committer.calls) == 1
        assert committer.calls[0].source_id == "reply:letter-1:1"
        assert committer.calls[0].occurred_at == datetime.fromisoformat(OCCURRED_AT)

        second = await outbox.scan_once()
        assert second.delivered == 0
        assert second.duplicates == 1
        assert len(committer.calls) == 1

        restarted_committer = SequencedCommitter()
        restarted = _outbox(tmp_path, restarted_committer)
        recovered = await restarted.scan_once()
        assert recovered.duplicates == 1
        assert restarted_committer.calls == []
        assert restarted.health() == {
            "status": "available",
            "provider": "sqlite-outbox",
            "schema_version": OUTBOX_SCHEMA_VERSION,
            "terminal_count": 1,
            "pending_count": 0,
            "attempt_count": 1,
        }

        journal_bytes = restarted.journal_path.read_bytes()
        assert SECRET_USER_TEXT.encode("utf-8") not in journal_bytes
        assert SECRET_REPLY_TEXT.encode("utf-8") not in journal_bytes

    asyncio.run(scenario())


def test_unavailable_delivery_remains_pending_and_retries_to_success(tmp_path: Path) -> None:
    async def scenario() -> None:
        _state(tmp_path / "state.json")
        committer = SequencedCommitter(
            [
                CanonicalMemoryDeliveryStatus.UNAVAILABLE,
                CanonicalMemoryDeliveryStatus.WRITTEN,
            ]
        )
        outbox = _outbox(tmp_path, committer)

        failed = await outbox.scan_once()
        assert failed.status == "degraded"
        assert failed.pending == 1
        assert outbox.health()["pending_count"] == 1

        recovered = await outbox.scan_once()
        assert recovered.status == "available"
        assert recovered.delivered == 1
        assert len(committer.calls) == 2
        health = outbox.health()
        assert health["terminal_count"] == 1
        assert health["pending_count"] == 0
        assert health["attempt_count"] == 2

    asyncio.run(scenario())


def test_new_canonical_revision_creates_a_new_delivery_identity(tmp_path: Path) -> None:
    async def scenario() -> None:
        state = tmp_path / "state.json"
        _state(state, revision=1)
        committer = SequencedCommitter(
            [
                CanonicalMemoryDeliveryStatus.WRITTEN,
                CanonicalMemoryDeliveryStatus.WRITTEN,
            ]
        )
        outbox = _outbox(tmp_path, committer)

        assert (await outbox.scan_once()).delivered == 1
        _state(
            state,
            revision=2,
            reply_text="第二版 canonical reply。",
        )
        assert (await outbox.scan_once()).delivered == 1
        assert [delivery.source_id for delivery in committer.calls] == [
            "reply:letter-1:1",
            "reply:letter-1:2",
        ]
        assert outbox.health()["terminal_count"] == 2

    asyncio.run(scenario())


def test_concurrent_scans_serialize_and_commit_once(tmp_path: Path) -> None:
    class SlowCommitter(SequencedCommitter):
        async def commit(self, delivery: CanonicalMemoryDelivery):
            await asyncio.sleep(0.02)
            return await super().commit(delivery)

    async def scenario() -> None:
        _state(tmp_path / "state.json")
        committer = SlowCommitter()
        outbox = _outbox(tmp_path, committer)

        first, second = await asyncio.gather(outbox.scan_once(), outbox.scan_once())
        assert len(committer.calls) == 1
        assert sorted((first.delivered, second.delivered)) == [0, 1]
        assert sorted((first.duplicates, second.duplicates)) == [0, 1]

    asyncio.run(scenario())


def test_nonterminal_and_malformed_rows_are_ignored_without_provider_calls(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        state = tmp_path / "state.json"
        state.write_text(
            json.dumps(
                {
                    "letters": [
                        {
                            "letter_id": "pending",
                            "letter_status": "PENDING",
                            "content": "still waiting",
                            "reply_text": "",
                        },
                        {
                            "letter_id": "bad id",
                            "letter_status": "COMPLETED",
                            "reply_revision": 1,
                            "content": "synthetic",
                            "reply_text": "synthetic",
                        },
                        "not-an-object",
                    ]
                }
            ),
            encoding="utf-8",
        )
        committer = SequencedCommitter()
        result = await _outbox(tmp_path, committer).scan_once()

        assert result.status == "available"
        assert result.discovered == 0
        assert result.ignored == 2
        assert committer.calls == []

    asyncio.run(scenario())


def test_missing_state_is_empty_but_corrupt_state_is_honestly_unavailable(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        committer = SequencedCommitter()
        outbox = _outbox(tmp_path, committer)
        missing = await outbox.scan_once()
        assert missing.status == "available"
        assert missing.discovered == 0

        (tmp_path / "state.json").write_text("{broken", encoding="utf-8")
        corrupt = await outbox.scan_once()
        assert corrupt.status == "unavailable"
        assert corrupt.error_code == "MEMORY_OUTBOX_STATE_UNAVAILABLE"
        assert committer.calls == []

    asyncio.run(scenario())


def test_unsupported_journal_schema_fails_closed(tmp_path: Path) -> None:
    journal = tmp_path / "delivery.sqlite3"
    with sqlite3.connect(journal) as connection:
        connection.execute("PRAGMA user_version=99")

    with pytest.raises(ConversationMemoryOutboxError) as raised:
        CanonicalMemoryOutbox(
            tmp_path / "state.json",
            journal,
            SequencedCommitter(),
        )
    assert raised.value.code == "MEMORY_OUTBOX_SCHEMA_UNSUPPORTED"
