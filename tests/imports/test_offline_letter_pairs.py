from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from runtime.imports.offline_letter_pairs import (
    OFFLINE_LETTER_PAIR_IMPORT_KIND,
    OFFLINE_LETTER_PAIR_PROVENANCE_KEY,
    OFFLINE_LETTER_PAIR_PUBLISH_STATUS,
    OFFLINE_LETTER_PAIR_PUBLISH_STATUS_KEY,
    main,
)
from runtime.memory.local_memory import LocalMemoryAdapter
from runtime.memory.memory_port import LegacyImportResult


def _write_pairs(path: Path) -> bytes:
    raw = json.dumps(
        [{"content": f"synthetic old letter {n}", "reply": f"synthetic old reply {n}"}
         for n in ("one", "two")], ensure_ascii=False,
    ).encode("utf-8")
    path.write_bytes(raw)
    return raw


def _run_cli(source: Path, memory_root: Path, capsys, *, apply: bool = False):
    args = ["--source", str(source), "--memory-root", str(memory_root)]
    exit_code = main([*args, "--apply"] if apply else args)
    output = capsys.readouterr().out
    return exit_code, output, json.loads(output)


def _offline_metadata(digest: str) -> dict[str, object]:
    return {
        "import_kind": OFFLINE_LETTER_PAIR_IMPORT_KIND,
        OFFLINE_LETTER_PAIR_PUBLISH_STATUS_KEY: OFFLINE_LETTER_PAIR_PUBLISH_STATUS,
        OFFLINE_LETTER_PAIR_PROVENANCE_KEY: dict(schema_version=1, source_index=1,
            source_sha256=digest, timestamp_status="unknown"),
    }


def test_offline_letter_pair_cli_dry_run_is_content_free_and_side_effect_free(tmp_path, capsys):
    source = tmp_path / "letter-pairs.json"
    raw = _write_pairs(source)
    memory_root = tmp_path / "memory"

    exit_code, output, report = _run_cli(source, memory_root, capsys)
    assert exit_code == 0
    assert report["status"] == "dry_run"
    assert report["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert (report["seen"], report["accepted"], report["would_insert"]) == (2, 2, 2)
    assert (report["history_audit"], report["provider_calls"]) == ("not_written", 0)
    assert "synthetic old letter" not in output and "synthetic old reply" not in output
    assert not memory_root.exists()


def test_offline_letter_pair_cli_apply_is_atomic_typed_and_idempotent(tmp_path, capsys):
    source = tmp_path / "letter-pairs.json"
    raw = _write_pairs(source)
    memory_root = tmp_path / "memory"

    first_exit, first_output, first = _run_cli(source, memory_root, capsys, apply=True)
    _dry_exit, _dry_output, dry = _run_cli(source, memory_root, capsys)
    second_exit, second_output, second = _run_cli(source, memory_root, capsys, apply=True)
    assert first_exit == second_exit == 0
    assert (first["status"], first["inserted"], first["duplicates"]) == ("committed", 2, 0)
    assert first["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert (second["status"], second["inserted"], second["duplicates"]) == ("committed", 0, 2)
    assert (second["would_insert"], second["duplicates"]) == (
        dry["would_insert"], dry["duplicates"]
    ) == (0, 2)
    assert "synthetic old letter" not in first_output + second_output
    assert "synthetic old reply" not in first_output + second_output

    archive = LocalMemoryAdapter(memory_root / "memory.sqlite3")
    try:
        letters = archive.list_legacy()
    finally:
        archive.close()
    assert len(letters) == 2
    observed_indexes = set()
    for letter in letters:
        metadata = letter["metadata"]
        assert metadata["import_kind"] == OFFLINE_LETTER_PAIR_IMPORT_KIND
        assert metadata[OFFLINE_LETTER_PAIR_PUBLISH_STATUS_KEY] == OFFLINE_LETTER_PAIR_PUBLISH_STATUS
        provenance = metadata[OFFLINE_LETTER_PAIR_PROVENANCE_KEY]
        observed_indexes.add(provenance["source_index"])
        assert provenance == {
            "schema_version": 1,
            "source_index": provenance["source_index"],
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "timestamp_status": "unknown",
        }
        assert "official_history_publish_status" not in metadata
        assert "official_history_memory_semantics" not in metadata
    assert observed_indexes == {1, 2}

    different_source = tmp_path / "same-pairs-different-source.json"
    different_source.write_text(
        json.dumps(json.loads(raw), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    before = letters
    _exit, _output, dry_duplicate = _run_cli(different_source, memory_root, capsys)
    _exit, _output, applied_duplicate = _run_cli(different_source, memory_root, capsys, apply=True)
    archive = LocalMemoryAdapter(memory_root / "memory.sqlite3")
    try:
        after = archive.list_legacy()
    finally:
        archive.close()
    assert (dry_duplicate["would_insert"], dry_duplicate["duplicates"]) == (0, 2)
    assert (applied_duplicate["inserted"], applied_duplicate["duplicates"]) == (0, 2)
    assert after == before


def test_offline_letter_pair_cli_rejects_whole_invalid_batch_without_echo(tmp_path, capsys):
    source = tmp_path / "invalid.json"
    source.write_text(
        json.dumps([{"content": "private-looking synthetic text", "reply": "valid reply"},
                    {"content": "second private-looking synthetic text"}]), encoding="utf-8",
    )
    memory_root = tmp_path / "memory"

    exit_code, output, report = _run_cli(source, memory_root, capsys, apply=True)
    assert exit_code == 2
    assert report["error_code"] == "OFFLINE_LETTER_PAIR_INVALID"
    assert report["status"] == "rejected"
    assert (report["history_audit"], report["provider_calls"]) == ("not_written", 0)
    assert "private-looking" not in output
    assert str(source) not in output
    assert not memory_root.exists()


def test_pair_identity_does_not_collide_on_display_delimiters(tmp_path, capsys):
    source = tmp_path / "delimiter-pairs.json"
    source.write_text(
        json.dumps([{"content": "alpha\nLinli reply:\nbeta", "reply": "gamma"},
                    {"content": "alpha", "reply": "beta\nLinli reply:\ngamma"}]),
        encoding="utf-8",
    )
    memory_root = tmp_path / "memory"

    _exit, _output, dry = _run_cli(source, memory_root, capsys)
    _exit, _output, applied = _run_cli(source, memory_root, capsys, apply=True)

    assert (dry["would_insert"], dry["duplicates"]) == (2, 0)
    assert (applied["inserted"], applied["duplicates"]) == (2, 0)


def test_cli_recovery_keeps_unknown_time_in_the_default_mailbox(tmp_path, capsys, monkeypatch):
    import local_server

    source = tmp_path / "letter-pairs.json"
    _write_pairs(source)
    memory_root = tmp_path / "memory"
    exit_code, _output, report = _run_cli(source, memory_root, capsys, apply=True)
    assert exit_code == 0 and report["inserted"] == 2

    archive = LocalMemoryAdapter(memory_root / "memory.sqlite3")
    monkeypatch.setattr(local_server.store, "letters", [])
    monkeypatch.setattr(local_server, "memory_adapter", archive)
    try:
        stored_times = archive.connection.execute("SELECT occurred_at FROM legacy_letters").fetchall()
        assert {row[0] for row in stored_times} == {None}
        listed = asyncio.run(local_server.route("GET", "/toy/letter/list", {}, {}))
        unread = asyncio.run(local_server.route("GET", "/toy/letter/unread_count", {}, {}))
        item = listed["data"]["list"][0]
        detail = asyncio.run(local_server.route(
            "GET", "/toy/letter/detail", {}, {"letter_id": item["letter_id"]}))
    finally:
        archive.close()

    assert listed["data"]["total"] == 2
    assert item["summary"].startswith("synthetic old letter")
    assert item["created_at"] is None
    assert unread["data"]["unread_count"] == 0
    assert detail["data"]["reply_text"].startswith("synthetic old reply")
    assert detail["data"]["created_at"] is None
    assert detail["data"]["replied_at"] is None
    assert detail["data"]["scope"] == "legacy"
    assert detail["data"]["read_only"] is True


def test_generic_legacy_http_import_cannot_forge_offline_mailbox_publication(monkeypatch):
    import local_server

    class Archive:
        enabled = True

        def __init__(self) -> None:
            self.records = ()

        def import_legacy_records(self, records, **_kwargs):
            self.records = tuple(records)
            return LegacyImportResult(seen=len(self.records), inserted=len(self.records))

    archive = Archive()
    monkeypatch.setattr(local_server, "_legacy_import_adapter", lambda: archive)
    payload = {"mode": "read_only", "letters": [{
        "source_record_id": "untrusted-1", "content": "synthetic combined pair",
        "metadata": {**_offline_metadata("b" * 64), "reply_text": "synthetic reply"},
    }]}

    imported = asyncio.run(local_server.route("POST", "/toy/letter/legacy/import", payload, {}))

    assert imported["code"] == 0
    assert len(archive.records) == 1
    metadata = archive.records[0].metadata
    assert OFFLINE_LETTER_PAIR_PUBLISH_STATUS_KEY not in metadata
    assert OFFLINE_LETTER_PAIR_PROVENANCE_KEY not in metadata
    assert metadata["reply_text"] == "synthetic reply"


def test_local_history_route_discovers_installed_official_backup_and_imports(
    tmp_path, monkeypatch
):
    import local_server

    install_root = tmp_path / "installed"
    data_root = install_root / "data"
    official_root = tmp_path / "official-client"
    data_root.mkdir(parents=True)
    official_root.mkdir()
    source = official_root / "letter_pairs.json"
    _write_pairs(source)
    (install_root / ".olivia-full-patch.json").write_text(
        json.dumps({"official_source": str(official_root)}),
        encoding="utf-8",
    )
    archive = LocalMemoryAdapter(tmp_path / "memory.sqlite3")
    monkeypatch.setattr(local_server, "_local_data_root", lambda: data_root)
    monkeypatch.setattr(local_server, "_legacy_import_adapter", lambda: archive)
    try:
        ready = asyncio.run(local_server.route(
            "GET", "/toy/letter/legacy/local-import", {}, {}
        ))
        unconfirmed = asyncio.run(local_server.route(
            "POST", "/toy/letter/legacy/local-import", {}, {}
        ))
        applied = asyncio.run(local_server.route(
            "POST", "/toy/letter/legacy/local-import", {}, {},
            companion_confirmed=True,
        ))
        repeated = asyncio.run(local_server.route(
            "POST", "/toy/letter/legacy/local-import", {}, {},
            companion_confirmed=True,
        ))
    finally:
        archive.close()

    assert ready["data"]["source"] == "local_backup"
    assert (ready["data"]["status"], ready["data"]["seen"]) == ("READY", 2)
    assert (ready["data"]["would_insert"], ready["data"]["duplicates"]) == (2, 0)
    assert unconfirmed["code"] == 403
    assert (applied["data"]["status"], applied["data"]["inserted"]) == ("APPLIED", 2)
    assert (repeated["data"]["inserted"], repeated["data"]["duplicates"]) == (0, 2)


def test_local_history_route_prompts_when_backup_is_absent(tmp_path, monkeypatch):
    import local_server

    install_root = tmp_path / "installed"
    data_root = install_root / "data"
    official_root = tmp_path / "official-client"
    data_root.mkdir(parents=True)
    official_root.mkdir()
    (install_root / ".olivia-full-patch.json").write_text(
        json.dumps({"official_source": str(official_root)}),
        encoding="utf-8",
    )
    monkeypatch.setattr(local_server, "_local_data_root", lambda: data_root)

    result = asyncio.run(local_server.route(
        "GET", "/toy/letter/legacy/local-import", {}, {}
    ))

    assert result["code"] == 404
    assert result["data"] == {
        "status": "UNAVAILABLE",
        "error_code": "OFFLINE_LETTER_BACKUP_REQUIRED",
        "retryable": True,
        "source": "local_backup",
    }
