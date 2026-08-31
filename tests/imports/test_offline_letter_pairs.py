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
        [
            {"content": "synthetic old letter one", "reply": "synthetic old reply one"},
            {"content": "synthetic old letter two", "reply": "synthetic old reply two"},
        ],
        ensure_ascii=False,
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
        OFFLINE_LETTER_PAIR_PROVENANCE_KEY: dict(
            schema_version=1, source_index=1, source_sha256=digest,
            timestamp_status="unknown",
        ),
    }


def test_offline_letter_pair_cli_dry_run_is_content_free_and_side_effect_free(
    tmp_path: Path,
    capsys,
) -> None:
    source = tmp_path / "letter-pairs.json"
    raw = _write_pairs(source)
    memory_root = tmp_path / "memory"

    exit_code, output, report = _run_cli(source, memory_root, capsys)
    assert exit_code == 0
    assert report["status"] == "dry_run"
    assert report["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert (report["seen"], report["accepted"], report["would_insert"]) == (2, 2, 2)
    assert report["history_audit"] == "not_written" and report["provider_calls"] == 0
    assert "synthetic old letter" not in output
    assert "synthetic old reply" not in output
    assert not memory_root.exists()


def test_offline_letter_pair_cli_apply_is_atomic_typed_and_idempotent(
    tmp_path: Path,
    capsys,
) -> None:
    source = tmp_path / "letter-pairs.json"
    raw = _write_pairs(source)
    memory_root = tmp_path / "memory"

    first_exit, first_output, first = _run_cli(source, memory_root, capsys, apply=True)
    second_exit, second_output, second = _run_cli(source, memory_root, capsys, apply=True)
    assert first_exit == second_exit == 0
    assert (first["status"], first["inserted"], first["duplicates"]) == (
        "committed", 2, 0
    )
    assert first["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert (second["status"], second["inserted"], second["duplicates"]) == (
        "committed", 0, 2
    )
    assert second["would_insert"] == 0
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
        assert (
            metadata[OFFLINE_LETTER_PAIR_PUBLISH_STATUS_KEY]
            == OFFLINE_LETTER_PAIR_PUBLISH_STATUS
        )
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


def test_offline_letter_pair_cli_rejects_whole_invalid_batch_without_echo(
    tmp_path: Path,
    capsys,
) -> None:
    source = tmp_path / "invalid.json"
    source.write_text(
        json.dumps(
            [
                {"content": "private-looking synthetic text", "reply": "valid reply"},
                {"content": "second private-looking synthetic text"},
            ]
        ),
        encoding="utf-8",
    )
    memory_root = tmp_path / "memory"

    exit_code, output, report = _run_cli(source, memory_root, capsys, apply=True)
    assert exit_code == 2
    assert report["error_code"] == "OFFLINE_LETTER_PAIR_INVALID"
    assert report["status"] == "rejected"
    assert report["history_audit"] == "not_written" and report["provider_calls"] == 0
    assert "private-looking" not in output
    assert str(source) not in output
    assert not memory_root.exists()


def test_validated_offline_pairs_are_read_only_in_the_default_mailbox(
    monkeypatch,
) -> None:
    import local_server

    imported = {
        "letter_id": "offline-pair-1",
        "created_at": 1710000000,
        "content": "synthetic recovered letter",
        "reply_text": "synthetic recovered reply",
        "is_read": 0,
        "read_only": True,
        "metadata": _offline_metadata("a" * 64),
    }
    monkeypatch.setattr(local_server.store, "letters", [])
    monkeypatch.setattr(local_server, "_legacy_letter_collection", lambda *, strict=False: [imported])

    listed = asyncio.run(local_server.route("GET", "/toy/letter/list", {}, {}))
    unread = asyncio.run(local_server.route("GET", "/toy/letter/unread_count", {}, {}))
    detail = asyncio.run(
        local_server.route(
            "GET",
            "/toy/letter/detail",
            {},
            {"letter_id": "offline-pair-1"},
        )
    )

    assert listed["data"]["total"] == 1
    assert listed["data"]["list"][0]["summary"] == "synthetic recovered letter"
    assert unread["data"]["unread_count"] == 0
    assert detail["data"]["reply_text"] == "synthetic recovered reply"
    assert detail["data"]["scope"] == "legacy"
    assert detail["data"]["read_only"] is True
    assert local_server._letter_collection("current")[0]["is_read"] == 1


def test_generic_legacy_http_import_cannot_forge_offline_mailbox_publication(
    monkeypatch,
) -> None:
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
    payload = {
        "mode": "read_only",
        "letters": [
            {
                "source_record_id": "untrusted-1",
                "content": "synthetic combined pair",
                "metadata": {
                    **_offline_metadata("b" * 64),
                    "reply_text": "synthetic reply",
                },
            }
        ],
    }

    imported = asyncio.run(local_server.route("POST", "/toy/letter/legacy/import", payload, {}))

    assert imported["code"] == 0
    assert len(archive.records) == 1
    metadata = archive.records[0].metadata
    assert OFFLINE_LETTER_PAIR_PUBLISH_STATUS_KEY not in metadata
    assert OFFLINE_LETTER_PAIR_PROVENANCE_KEY not in metadata
    assert metadata["reply_text"] == "synthetic reply"
