"""Partial recall must not masquerade as an empty or complete memory read."""

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from runtime.memory.companion_memory_context import CompanionMemoryPromptBuilder
from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter
from runtime.memory.memory_port import LEGACY_LETTERS, LegacyLetter, MemoryRecord, NullMemoryPort
from runtime.memory.local_memory import LocalMemoryAdapter
from tests.memory.test_mem0_memory import FakeMem0, _config


NOW = datetime(2024, 1, 2, tzinfo=timezone.utc)


def test_original_index_error_is_not_a_successful_empty_read(tmp_path, monkeypatch):
    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic private database error")

    monkeypatch.setattr(memory._originals, "search", fail)
    prompt = CompanionMemoryPromptBuilder(NullMemoryPort(), memory).build(
        "Do you remember the blue teacup?", max_chars=20000,
    )

    assert prompt.status in {"unavailable", "degraded"}
    assert dict(prompt.recall_result.source_status)["original_index"] == "unavailable"
    assert "synthetic private" not in prompt.text


def test_partial_original_index_does_not_hide_matching_archive(tmp_path):
    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))
    memory.index_original_exchange(
        user_id="local-user", source_id="reply:recent", user_message="piano practice",
        assistant_message="I practiced the prelude", occurred_at=NOW,
    )
    with LocalMemoryAdapter(tmp_path / "archive.sqlite3") as archive:
        archive.import_legacy_records([LegacyLetter(
            content="piano had a blue teacup on top", source_record_id="official:older",
            source="official", occurred_at=NOW.isoformat(),
        )])
        prompt = CompanionMemoryPromptBuilder(archive, memory).build("piano", max_chars=20000)

    assert "blue teacup" in prompt.text
    assert "I practiced the prelude" in prompt.text
    assert LEGACY_LETTERS in prompt.domains


def _archive_letter(source="official:older"):
    return LegacyLetter(content="piano blue teacup", source_record_id=source,
        source="official", occurred_at=NOW.isoformat(),
        metadata={"import_kind": "official_text_reply", "user_content": "piano blue teacup",
                  "reply_text": "Yes, I left the blue teacup there"})


def _memory_row(source="reply:previous"):
    return {"id": "memory.fixture.1", "memory": "the blue teacup", "user_id": "local-user",
            "agent_id": "linli", "metadata": {"domain": "conversation_memory", "canonical": True,
            "source_id": source}, "created_at": NOW.isoformat()}


def test_semantic_failure_preserves_originals_and_reports_degradation(tmp_path):
    backend = FakeMem0()
    backend.fail.add("search")
    memory = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))
    memory.index_original_exchange(user_id="local-user", source_id="reply:previous",
        user_message="blue teacup", assistant_message="left it on the piano", occurred_at=NOW)

    result = memory.search_evidence_result("teacup", user_id="local-user", limit=100)

    assert "left it on the piano" in {r.text for r in result.records}
    assert dict(result.source_status) == {"semantic": "unavailable", "original_index": "available"}
    assert result.status == "degraded"


def test_index_failure_preserves_safe_semantic_summary_and_recovers_without_cache(tmp_path, monkeypatch):
    backend = FakeMem0()
    backend.rows.append(_memory_row())
    memory = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))
    real_search = memory._originals.search

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("private failure details")

    monkeypatch.setattr(memory._originals, "search", fail)
    failed = memory.search_evidence_result("teacup", user_id="local-user", limit=100)
    assert failed.status == "degraded"
    assert [r.text for r in failed.records] == ["the blue teacup"]
    monkeypatch.setattr(memory._originals, "search", real_search)
    recovered = memory.search_evidence_result("teacup", user_id="local-user", limit=100)
    assert recovered.status == "available"
    assert dict(recovered.source_status)["original_index"] == "available"


@pytest.mark.parametrize("max_chars", [2400, 20000])
def test_archive_recall_respects_forgotten_and_excluded_sources(tmp_path, max_chars):
    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))
    source = "official:older"
    mapped = "history:" + hashlib.sha256(source.encode()).hexdigest()
    with LocalMemoryAdapter(tmp_path / "archive.sqlite3") as archive:
        archive.import_legacy_records([_archive_letter(source)])
        builder = CompanionMemoryPromptBuilder(archive, memory)
        excluded = builder.build("teacup", max_chars=max_chars, exclude_source_ids=(mapped,))
        assert not excluded.references
        memory._originals.forget("local-user", mapped)
        forgotten = builder.build("teacup", max_chars=max_chars)
        assert not forgotten.references
        assert "blue teacup" not in forgotten.text


def test_paused_memory_keeps_archive_without_provider_status_or_search(tmp_path, monkeypatch):
    from types import SimpleNamespace
    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))

    def unexpected(*args, **kwargs):
        pytest.fail("paused semantic provider was accessed")

    monkeypatch.setattr(memory, "status", unexpected)
    monkeypatch.setattr(memory, "search_evidence_result", unexpected)
    with LocalMemoryAdapter(tmp_path / "archive.sqlite3") as archive:
        archive.import_legacy_records([_archive_letter()])
        builder = CompanionMemoryPromptBuilder(archive, memory,
            memory_lifecycle=SimpleNamespace(is_paused=lambda: True))
        result = builder.build("teacup", max_chars=20000)
        assert "left the blue teacup there" in result.text
        assert dict(result.recall_result.source_status)["semantic"] == "paused"
        assert not builder.trace_sources(("history:unknown",))


def test_archive_guard_failure_is_explicit_and_cannot_resurrect_deleted_history(tmp_path, monkeypatch):
    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("private failure")

    monkeypatch.setattr(memory._originals, "forgotten_sources", fail)
    with LocalMemoryAdapter(tmp_path / "archive.sqlite3") as archive:
        archive.import_legacy_records([_archive_letter()])
        result = CompanionMemoryPromptBuilder(archive, memory).collect("teacup")
        assert dict(result.source_status)["archive"] == "unavailable"
        assert not result.records


def test_concurrent_requests_keep_their_own_read_failure(tmp_path, monkeypatch):
    class SelectiveBackend(FakeMem0):
        def search(self, query, **kwargs):
            if query == "broken":
                raise RuntimeError("synthetic unavailable")
            return super().search(query, **kwargs)

    memory = Mem0ConversationMemoryAdapter(SelectiveBackend(), _config(tmp_path))
    bad_reached_index, good_finished = threading.Event(), threading.Event()

    def originals(query, *args, **kwargs):
        if query == "broken":
            bad_reached_index.set()
            assert good_finished.wait(3)
        return ()

    monkeypatch.setattr(memory._originals, "search", originals)
    with ThreadPoolExecutor(max_workers=2) as executor:
        bad_future = executor.submit(memory.search_evidence_result, "broken", user_id="local-user", limit=100)
        assert bad_reached_index.wait(3)
        try:
            good = memory.search_evidence_result("working", user_id="local-user", limit=100)
        finally:
            good_finished.set()
        bad = bad_future.result(3)
    assert dict(good.source_status)["semantic"] == "available"
    assert dict(bad.source_status)["semantic"] == "unavailable"


def test_archive_pairs_do_not_duplicate_indexed_source(tmp_path):
    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))
    mapped = "history:" + hashlib.sha256(b"official:older").hexdigest()
    memory.index_original_exchange(user_id="local-user", source_id=mapped,
        user_message="piano blue teacup", assistant_message="Yes, I left the blue teacup there", occurred_at=NOW)
    with LocalMemoryAdapter(tmp_path / "archive.sqlite3") as archive:
        archive.import_legacy_records([_archive_letter()])
        result = CompanionMemoryPromptBuilder(archive, memory).collect("teacup")
    assert len(result.records) == 2
    assert {r.metadata["speaker"] for r in result.records} == {"user", "linli"}


def test_archive_unique_topic_gets_capacity_before_many_index_hits(tmp_path):
    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))
    for index in range(16):
        memory.index_original_exchange(user_id="local-user", source_id=f"reply:{index}",
            user_message="piano exercise " + "practice " * 24,
            assistant_message="piano practice " + "melody " * 24, occurred_at=NOW)
    with LocalMemoryAdapter(tmp_path / "archive.sqlite3") as archive:
        archive.import_legacy_records([_archive_letter()])
        prompt = CompanionMemoryPromptBuilder(archive, memory).build(
            "piano. blue teacup", max_chars=7000)
    assert prompt.truncated
    assert "Yes, I left the blue teacup there" in prompt.text


def test_disabled_memory_does_not_resurrect_forgotten_archive(tmp_path, monkeypatch):
    from runtime.memory.conversation_memory_port import NullConversationMemoryPort
    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))
    memory._originals.forget("local-user", "history:" + hashlib.sha256(b"official:older").hexdigest())
    disabled = NullConversationMemoryPort()
    disabled.config = memory.config
    with LocalMemoryAdapter(tmp_path / "archive.sqlite3") as archive:
        archive.import_legacy_records([_archive_letter()])
        prompt = CompanionMemoryPromptBuilder(archive, disabled).build("teacup", max_chars=20000)
    assert not prompt.references
    assert "blue teacup" not in prompt.text
    assert not memory.backend.calls


def test_archive_search_covers_later_topics_without_repeating_duplicate_topics(tmp_path):
    class LeadingTopicArchive:
        enabled = True
        calls = []

        def status(self):
            return {"status": "available"}

        def search(self, query, **kwargs):
            self.calls.append(query)
            # Simulate the existing archive engine only admitting leading terms.
            topic = "breakfast" if "breakfast" in query else "ring" if "ring" in query else "name"
            return [MemoryRecord(memory_id=topic, domain=LEGACY_LETTERS, text=topic,
                source="official", created_at=0, provenance={"source_record_id": f"official:{topic}"},
                metadata={"import_kind": "official_text_reply", "user_content": f"I remember {topic}",
                          "reply_text": f"Yes, {topic} was in the old letter"})]

    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))
    archive = LeadingTopicArchive()
    result = CompanionMemoryPromptBuilder(archive, memory).build(
        "breakfast?ring?name?breakfast?", max_chars=20000)

    assert all(f"Yes, {topic}" in result.text for topic in ("breakfast", "ring", "name"))
    assert archive.calls == ["breakfast", "ring", "name"]
    assert {r.metadata.get("topic_indexes") for r in result.references} == {"0", "1", "2"}


def test_fresh_chinese_archive_matches_detail_inside_a_long_question(tmp_path):
    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))
    with LocalMemoryAdapter(tmp_path / "archive.sqlite3") as archive:
        archive.import_legacy_records([LegacyLetter(
            content="这对戒指还戴着吗？\n戒指确实在手上。", source_record_id="official:ring",
            source="official", metadata={"import_kind": "official_text_reply",
                "user_content": "这对戒指还戴着吗？", "reply_text": "戒指确实在手上。"})])
        result = CompanionMemoryPromptBuilder(archive, memory).build(
            "你还记得我把银色戒指戴在你手上的那个晚上吗？", max_chars=20000)
    assert "戒指确实在手上" in result.text


def test_paused_source_trace_reads_archive_without_touching_original_index(tmp_path, monkeypatch):
    from types import SimpleNamespace
    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))

    def unexpected(*args, **kwargs):
        pytest.fail("paused original provider was accessed")

    monkeypatch.setattr(memory._originals, "get_sources", unexpected)
    monkeypatch.setattr(memory._originals, "expand_sources", unexpected)
    with LocalMemoryAdapter(tmp_path / "archive.sqlite3") as archive:
        archive.import_legacy_records([_archive_letter()])
        builder = CompanionMemoryPromptBuilder(archive, memory,
            memory_lifecycle=SimpleNamespace(is_paused=lambda: True))
        result = builder.trace_sources_result(("official:older",))
    assert len(result.records) == 2
    assert result.status == "available"
    assert dict(result.source_status)["original_trace"] == "disabled"
    assert not memory.backend.calls


def test_failed_original_trace_preserves_archive_evidence_and_failure_state(tmp_path, monkeypatch):
    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("private index read failure")

    monkeypatch.setattr(memory._originals, "get_sources", fail)
    with LocalMemoryAdapter(tmp_path / "archive.sqlite3") as archive:
        archive.import_legacy_records([_archive_letter()])
        result = CompanionMemoryPromptBuilder(archive, memory).trace_sources_result(("official:older",))
    assert len(result.records) == 2
    assert result.status == "degraded"
    assert dict(result.source_status)["original_trace"] == "unavailable"
    assert not memory.backend.calls


def test_relevant_short_reply_is_not_buried_by_many_long_topic_mentions(tmp_path):
    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))
    filler = " ".join(f"irrelevant{index}" for index in range(70))

    class RankedArchive:
        enabled = True
        def status(self):
            return {"status": "available"}
        def search(self, query, **kwargs):
            records = [MemoryRecord(memory_id=f"a{index}", domain=LEGACY_LETTERS,
                text=f"garden compass {filler}", source="official", created_at=0,
                provenance={"source_record_id":f"old:{index}"},
                metadata={"import_kind":"official_text_reply", "user_content":f"garden compass {filler}",
                          "reply_text":"We can discuss it later"}) for index in range(25)]
            records.append(MemoryRecord(memory_id="z_target", domain=LEGACY_LETTERS,
                text="garden compass", source="official", created_at=0,
                provenance={"source_record_id":"old:target"},
                metadata={"import_kind":"official_text_reply", "user_content":"Where was it?",
                          "reply_text":"The garden compass was beside the door"}))
            return records
    prompt = CompanionMemoryPromptBuilder(RankedArchive(), memory).build("garden compass", max_chars=4000)
    assert "The garden compass was beside the door" in prompt.text
    assert "Where was it?" in prompt.text


def test_lexical_reranking_preserves_a_semantic_only_source(tmp_path):
    backend = FakeMem0()
    backend.rows.append(_memory_row())
    memory = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))
    with LocalMemoryAdapter(tmp_path / "archive.sqlite3") as archive:
        archive.import_legacy_records([LegacyLetter(
            content=f"piano practice {index}", source_record_id=f"official:{index}", source="official",
            metadata={"import_kind":"official_text_reply", "user_content":f"piano practice {index}",
                      "reply_text":"I practiced again"}) for index in range(20)])
        prompt = CompanionMemoryPromptBuilder(archive, memory).build("piano", max_chars=4000)
    assert "the blue teacup" in prompt.text
    assert any(record.metadata.get("retrieval_route") == "semantic" for record in prompt.references)


@pytest.mark.parametrize("query", ["piano?blue teacup?", "blue teacup?piano?"])
def test_distinct_detail_is_kept_when_topic_order_changes(tmp_path, query):
    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))
    with LocalMemoryAdapter(tmp_path / "archive.sqlite3") as archive:
        records = [LegacyLetter(content=f"piano practice {index}", source_record_id=f"official:{index}",
            source="official", metadata={"import_kind":"official_text_reply",
                "user_content":f"piano practice {index}", "reply_text":"I played another tune"}) for index in range(25)]
        records.append(_archive_letter())
        archive.import_legacy_records(records)
        prompt = CompanionMemoryPromptBuilder(archive, memory).build(query, max_chars=4000)
    assert "Yes, I left the blue teacup there" in prompt.text


def test_disabled_archive_never_reads_hidden_rows(tmp_path):
    class DisabledArchive:
        enabled = False
        calls = []
        def status(self):
            return {"status":"disabled"}
        def search(self, *args, **kwargs):
            self.calls.append("search")
            return []
        def list_legacy(self):
            self.calls.append("list_legacy")
            return [{"memory_id":"hidden", "source_record_id":"official:hidden", "content":"hidden blue teacup",
                     "metadata":{"import_kind":"official_text_reply", "user_content":"hidden blue teacup",
                                 "reply_text":"hidden reply"}}]
    archive = DisabledArchive()
    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))
    result = CompanionMemoryPromptBuilder(archive, memory).collect("blue teacup")
    assert not result.records
    assert archive.calls == []
    assert dict(result.source_status)["archive"] == "disabled"


@pytest.mark.parametrize("alias_kind", ["backup", "backup_history", "relationship", "relationship_history"])
@pytest.mark.parametrize("max_chars", [2400, 20000])
def test_normal_archive_recall_honors_every_import_alias(tmp_path, alias_kind, max_chars):
    from dataclasses import replace
    pair = _archive_letter("letter-backup:copy")
    metadata = dict(pair.metadata, backup_record={"source_id":"official:original",
        "content":pair.metadata["user_content"], "reply_text":pair.metadata["reply_text"]})
    relationship = "relationship-letter:" + hashlib.sha256(json.dumps(
        [metadata["user_content"],metadata["reply_text"]], ensure_ascii=False).encode()).hexdigest()
    alias = {"backup":"official:original", "relationship":relationship}.get(alias_kind)
    if alias_kind.endswith("_history"):
        raw = "official:original" if alias_kind == "backup_history" else relationship
        alias = "history:" + hashlib.sha256(raw.encode()).hexdigest()
    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))
    memory._originals.forget("local-user", alias)
    with LocalMemoryAdapter(tmp_path / "archive.sqlite3") as archive:
        archive.import_legacy_records([replace(pair, metadata=metadata)])
        prompt = CompanionMemoryPromptBuilder(archive, memory).build("teacup", max_chars=max_chars)
    assert not prompt.references
    assert "blue teacup" not in prompt.text


def test_forgetting_one_dated_source_keeps_later_same_words(tmp_path):
    from dataclasses import replace
    first = _archive_letter("letter-backup:first")
    second = _archive_letter("letter-backup:second")
    first = replace(first, content=first.content + " first date", occurred_at="2024-01-02T00:00:00+00:00",
        metadata=dict(first.metadata, backup_record={"source_id":"official:first"}))
    second = replace(second, content=second.content + " second date", occurred_at="2024-01-03T00:00:00+00:00",
        metadata=dict(second.metadata, backup_record={"source_id":"official:second"}))
    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))
    memory._originals.forget("local-user", "history:" + hashlib.sha256(b"official:first").hexdigest())
    with LocalMemoryAdapter(tmp_path / "archive.sqlite3") as archive:
        archive.import_legacy_records([first, second])
        result = CompanionMemoryPromptBuilder(archive, memory).collect("teacup")
    assert len(result.records) == 2
    assert {r.occurred_at for r in result.records} == {"2024-01-03T00:00:00+00:00"}
