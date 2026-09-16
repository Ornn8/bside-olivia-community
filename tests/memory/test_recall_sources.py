"""An evidence pointer must resolve exactly without inventing dates or restoring deletion."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from runtime.memory.recall_sources import read_archive_sources


def history(source):
    prefix = "history:offline:" if source.startswith("offline-letter-pairs:") else "history:"
    return prefix + hashlib.sha256(source.encode("utf-8")).hexdigest()


def row(source="official:1", *, user="那只茶杯还有吗？", reply="蓝色茶杯还在窗边。", stamp=None):
    return {"source_record_id": source, "occurred_at": stamp,
            "created_at": "2026-09-16T12:00:00+08:00", "imported_at": 1789530000,
            "metadata": {"import_kind": "official_text_reply", "user_content": user, "reply_text": reply}}


def relationship(record):
    metadata = record["metadata"]
    return "relationship-letter:" + hashlib.sha256(json.dumps(
        [metadata["user_content"], metadata["reply_text"]], ensure_ascii=False).encode("utf-8")).hexdigest()


def archive(*rows):
    def no_search(*args, **kwargs):
        raise AssertionError("explicit source reads must not invoke semantic or full text search")
    return SimpleNamespace(list_legacy=lambda: list(rows), search=no_search)


@pytest.mark.parametrize("alias", ["raw", "indexed", "relationship", "relationship_indexed"])
def test_all_historical_pointer_forms_read_both_original_speakers(alias):
    original = row("offline-letter-pairs:archive-hash:000001")
    raw = original["source_record_id"]
    aliases = {"raw": raw, "indexed": history(raw), "relationship": relationship(original),
               "relationship_indexed": history(relationship(original))}
    records = read_archive_sources(archive(original, row("different:2")), [aliases[alias]])
    assert [record.text for record in records] == ["那只茶杯还有吗？", "蓝色茶杯还在窗边。"]
    assert [record.metadata["speaker"] for record in records] == ["user", "linli"]
    assert all(record.provenance["source_record_id"] == history(raw) for record in records)
    assert all(record.provenance["archive_source_id"] == raw for record in records)
    assert all(record.metadata["complete_original"] and record.metadata["verbatim"] for record in records)
    assert all(record.metadata["requested_source_id"] == aliases[alias] for record in records)


def test_export_import_alias_reads_once_and_excluding_any_alias_blocks_the_pair():
    original = row("official:stable", stamp="2024-03-01T10:00:00+08:00")
    copied = row("letter-backup:copy", stamp=original["occurred_at"])
    copied["metadata"]["import_kind"] = "local_letter_backup_v1"
    copied["metadata"]["backup_record"] = {"source_id": "official:stable",
        "content": original["metadata"]["user_content"], "reply_text": original["metadata"]["reply_text"]}
    sources = archive(original, copied)
    aliases = {"official:stable", "letter-backup:copy", history("official:stable"),
               history("letter-backup:copy"), relationship(original), history(relationship(original))}
    records = read_archive_sources(sources, [history("letter-backup:copy"), "official:stable"])
    assert len(records) == 2
    assert aliases <= set(json.loads(records[0].metadata["source_aliases"]))
    for excluded in aliases:
        assert not read_archive_sources(sources, [relationship(original)], excluded_sources=[excluded])


@pytest.mark.parametrize("stamp", [None, "", "not-a-time", "2024-03-01T10:00:00"])
def test_unknown_original_time_never_uses_created_or_imported_time(stamp):
    records = read_archive_sources(archive(row(stamp=stamp)), ["official:1"])
    assert len(records) == 2
    assert all(record.occurred_at is None and record.created_at == 0 for record in records)
    assert all(record.provenance["occurred_at"] is None for record in records)


def test_known_original_timestamp_is_preserved():
    stamp = "2024-03-01T10:00:00+08:00"
    records = read_archive_sources(archive(row(stamp=stamp)), ["official:1"])
    assert len(records) == 2
    assert all(record.occurred_at == stamp for record in records)
    assert all(record.created_at < 1789530000 for record in records)


def test_repeated_same_words_at_different_times_are_distinct_events():
    first = row("official:first", stamp="2024-03-01T10:00:00+08:00")
    second = row("official:second", stamp="2024-03-02T10:00:00+08:00")
    records = read_archive_sources(archive(first, second), ["official:first", "official:second"])
    assert len(records) == 4
    assert len({record.provenance["source_record_id"] for record in records}) == 2


def test_unknown_pointer_does_not_search_or_return_a_similar_source():
    sources = archive(row("official:1"), row("official:10", reply="蓝色茶杯在楼上。"))
    assert not read_archive_sources(sources, ["official:missing"])
    found = read_archive_sources(sources, ["official:10"])
    assert [record.text for record in found] == ["那只茶杯还有吗？", "蓝色茶杯在楼上。"]
    assert not read_archive_sources(sources, ["official:10"], excluded_sources=[history("official:10")])


def test_source_errors_propagate_instead_of_becoming_no_match():
    def unavailable():
        raise OSError("archive unavailable")
    with pytest.raises(OSError, match="archive unavailable"):
        read_archive_sources(SimpleNamespace(list_legacy=unavailable), ["official:1"])


def test_real_import_relationship_pointer_survives_export_to_a_new_archive(tmp_path):
    from runtime.imports.letter_backup import export_letters, import_letters
    from runtime.imports.relationship_batches import archive_exchanges
    from runtime.memory.local_memory import LocalMemoryAdapter

    pair = {"content": "院子里的小风铃响了吗？", "reply": "蓝色风铃响过了，明天再去给它换绳子。"}
    with LocalMemoryAdapter(tmp_path / "first.sqlite3") as first:
        import_letters([pair], adapter=first)
        original = first.list_legacy()[0]
        exchange = archive_exchanges(first.list_legacy())[0]
        exported = export_letters(first.list_legacy())
    with LocalMemoryAdapter(tmp_path / "second.sqlite3") as second:
        import_letters(exported, adapter=second)
        for pointer in (original["source_record_id"], exchange.source_record_id, exchange.memory_source_id):
            records = read_archive_sources(second, [pointer])
            assert [record.text for record in records] == [pair["content"], pair["reply"]]
            assert all(record.occurred_at is None and record.created_at == 0 for record in records)
        assert not read_archive_sources(second, [exchange.memory_source_id],
                                        excluded_sources=[history(original["source_record_id"])])


def test_backup_alias_with_different_raw_content_cannot_redirect_a_pointer():
    unrelated = row("letter-backup:unrelated", reply="楼上的白杯子碎了。")
    unrelated["metadata"]["backup_record"] = {"source_id": "official:target",
        "content": "原来的来信", "reply_text": "原来的回信"}
    assert not read_archive_sources(archive(unrelated), ["official:target"])


def test_caller_selected_archive_is_the_only_user_scope_read():
    alice = archive(row("official:same-id", reply="甲的蓝色杯子。"))
    bob = archive(row("official:same-id", reply="乙的白色杯子。"))
    assert [record.text for record in read_archive_sources(alice, ["official:same-id"])][-1] == "甲的蓝色杯子。"
    assert [record.text for record in read_archive_sources(bob, ["official:same-id"])][-1] == "乙的白色杯子。"
