"""Synthetic route coverage: mailbox presence is not a memory import receipt."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from conversation_memory_port import MemoryWriteResult, MemoryWriteStatus
from runtime.imports.offline_letter_pairs import (
    apply_offline_letter_pair_recovery_to_adapter,
    offline_letter_pair_exchanges,
    plan_offline_letter_pair_recovery_with_adapter,
)
from runtime.memory.local_memory import LocalMemoryAdapter


class SourceDeduplicatingMemory:
    enabled = True

    def __init__(self, persisted):
        self.persisted = set(persisted)
        self.provider_writes = []
        self.fail_source = None

    def status(self):
        return SimpleNamespace(status="available")

    def remember_exchange(self, **kwargs):
        source = kwargs["source_id"]
        if source in self.persisted:
            return MemoryWriteResult(MemoryWriteStatus.DUPLICATE, source)
        self.provider_writes.append(source)
        if source == self.fail_source:
            return MemoryWriteResult(
                MemoryWriteStatus.UNAVAILABLE, source, error_code="MEM0_WRITE_FAILED"
            )
        self.persisted.add(source)
        return MemoryWriteResult(MemoryWriteStatus.WRITTEN, source, (source,))

    def delete_memory(self, memory_id, *, user_id):
        self.persisted.discard(memory_id)
        return True


@pytest.mark.parametrize("count,completed", [(1, 0), (5, 0), (5, 3)])
@pytest.mark.parametrize("fail_first", [False, True])
def test_reimport_repairs_memory_when_all_mailbox_letters_already_exist(
    tmp_path, monkeypatch, count, completed, fail_first
):
    import local_server

    source = tmp_path / "letter_pairs.json"
    source.write_text(json.dumps([
        {"content": f"Synthetic question {i}", "reply": f"Synthetic answer {i}"}
        for i in range(count)
    ]), encoding="utf-8")
    archive = LocalMemoryAdapter(tmp_path / "archive.sqlite3")
    initial = apply_offline_letter_pair_recovery_to_adapter(source, adapter=archive)
    assert initial.inserted == count
    ids = [exchange.memory_source_id for exchange in offline_letter_pair_exchanges(source)]
    memory = SourceDeduplicatingMemory(ids[:completed])
    monkeypatch.setattr(local_server, "_default_offline_letter_pair_source", lambda: source)
    monkeypatch.setattr(local_server, "_legacy_import_adapter", lambda: archive)
    monkeypatch.setattr(local_server, "_official_history_preflight_error", lambda: None)
    monkeypatch.setattr(local_server, "conversation_memory_adapter", memory)
    # A prior relationship audit must not short-circuit the independent memory pass.
    monkeypatch.setattr(local_server, "private_world_command_service", SimpleNamespace(
        lookup_command=lambda command_id: object(),
    ))
    monkeypatch.setattr(local_server, "_update_official_import_progress", lambda **kwargs: None)

    def post():
        return asyncio.run(local_server.route(
            "POST", "/toy/letter/legacy/local-import", {}, {}, companion_confirmed=True,
        ))

    try:
        if fail_first:
            memory.fail_source = ids[completed]
            failed = post()
            assert failed["code"] == 503
            assert failed["data"]["memory_migration"]["status"] == "partial"
            assert memory.persisted == set(ids[:completed])
            assert plan_offline_letter_pair_recovery_with_adapter(
                source, adapter=archive
            ).duplicates == count
            memory.fail_source = None
            memory.provider_writes.clear()

        repaired = post()
        assert repaired["code"] == 0
        assert repaired["data"]["inserted"] == 0
        assert repaired["data"]["duplicates"] == count
        migration = repaired["data"]["memory_migration"]
        assert migration["status"] == "completed"
        assert migration["written"] == count - completed
        assert migration["duplicates"] == completed
        assert memory.provider_writes == ids[completed:]
        assert memory.persisted == set(ids)

        memory.provider_writes.clear()
        repeated = post()
        assert repeated["data"]["inserted"] == 0
        assert repeated["data"]["duplicates"] == count
        assert repeated["data"]["memory_migration"]["written"] == 0
        assert repeated["data"]["memory_migration"]["duplicates"] == count
        assert memory.provider_writes == []
    finally:
        archive.close()
