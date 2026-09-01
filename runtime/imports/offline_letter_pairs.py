"""Restore paired local letters into the read-only archive without providers."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Mapping, Sequence

from runtime.memory.local_memory import LocalMemoryAdapter
from runtime.memory.memory_port import LegacyLetter, MemoryPort
from runtime.imports.historical_memory import HistoricalExchange


_MAX_SOURCE_BYTES = 16 * 1024 * 1024
_MAX_PAIR_CHARS = 200_000
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

OFFLINE_LETTER_PAIR_IMPORT_KIND = "offline_recovered_text_reply"
OFFLINE_LETTER_PAIR_PUBLISH_STATUS_KEY = "offline_mailbox_publish_status"
OFFLINE_LETTER_PAIR_PUBLISH_STATUS = "validated_pair_v1"
OFFLINE_LETTER_PAIR_PROVENANCE_KEY = "offline_letter_pair_provenance"
_OBSOLETE_OFFLINE_EXPORT_SOURCE = "official-olivia-offline-export"
_OBSOLETE_OFFLINE_EXPORT_KIND = "official_text_reply_offline_export"


@dataclass(frozen=True)
class OfflineLetterPairProvenance:
    source_sha256: str
    source_index: int
    schema_version: int = 1
    timestamp_status: str = "unknown"

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.source_sha256):
            raise ValueError("offline source digest is invalid")
        if type(self.source_index) is not int or self.source_index < 1:
            raise ValueError("offline source index is invalid")
        if self.schema_version != 1 or self.timestamp_status != "unknown":
            raise ValueError("offline provenance version is invalid")

    @classmethod
    def from_metadata(cls, value: object) -> "OfflineLetterPairProvenance":
        try:
            if not isinstance(value, Mapping):
                raise TypeError
            return cls(**dict(value))  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValueError("offline provenance metadata is invalid") from exc


@dataclass(frozen=True)
class OfflineLetterPairRecoveryReport:
    status: str
    source_sha256: str
    seen: int
    accepted: int
    would_insert: int
    inserted: int = 0
    duplicates: int = 0
    rejected: int = 0
    read_only: bool = True
    history_audit: str = "not_written"
    provider_calls: int = 0
    would_update: int = 0
    updated: int = 0
    would_remove: int = 0
    removed: int = 0


def is_published_offline_letter_pair(metadata: object) -> bool:
    if not isinstance(metadata, Mapping):
        return False
    if (
        metadata.get("import_kind") != OFFLINE_LETTER_PAIR_IMPORT_KIND
        or metadata.get(OFFLINE_LETTER_PAIR_PUBLISH_STATUS_KEY)
        != OFFLINE_LETTER_PAIR_PUBLISH_STATUS
    ):
        return False
    try:
        OfflineLetterPairProvenance.from_metadata(
            metadata.get(OFFLINE_LETTER_PAIR_PROVENANCE_KEY)
        )
    except (TypeError, ValueError):
        return False
    return True


def _pair_archive_content(pair: tuple[str, str]) -> str:
    return json.dumps(
        {"content": pair[0], "reply": pair[1]},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _repair_reversible_utf8_latin1_mojibake(value: str) -> str:
    """Undo the legacy backup's lossless UTF-8-as-Latin-1 decode mistake."""

    if any(ord(character) > 0xFF for character in value):
        return value
    try:
        repaired = value.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return value
    original_c1 = sum(0x80 <= ord(character) <= 0x9F for character in value)
    repaired_c1 = sum(0x80 <= ord(character) <= 0x9F for character in repaired)
    original_cjk = sum("\u3400" <= character <= "\u9fff" for character in value)
    repaired_cjk = sum("\u3400" <= character <= "\u9fff" for character in repaired)
    if repaired_cjk > original_cjk and (
        not original_c1 or repaired_c1 < original_c1
    ):
        return repaired
    return value


def _matching_obsolete_export_ids(
    rows: Sequence[Mapping[str, object]],
    pairs: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    canonical = {_pair_archive_content(pair) for pair in pairs}
    matches: list[str] = []
    for row in rows:
        if row.get("source") != _OBSOLETE_OFFLINE_EXPORT_SOURCE:
            continue
        metadata = row.get("metadata")
        if not isinstance(metadata, Mapping) or metadata.get("import_kind") != _OBSOLETE_OFFLINE_EXPORT_KIND:
            continue
        content = metadata.get("user_content")
        reply = metadata.get("reply_text")
        memory_id = row.get("letter_id") or row.get("memory_id")
        if not all(isinstance(value, str) for value in (content, reply, memory_id)):
            continue
        repaired = (
            _repair_reversible_utf8_latin1_mojibake(content),
            _repair_reversible_utf8_latin1_mojibake(reply),
        )
        if _pair_archive_content(repaired) in canonical:
            matches.append(memory_id)
    return tuple(matches)


def _adapter_obsolete_export_ids(
    adapter: MemoryPort,
    pairs: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    list_legacy = getattr(adapter, "list_legacy", None)
    if not callable(list_legacy):
        return ()
    return _matching_obsolete_export_ids(list_legacy(), pairs)


def _build_records(
    raw: bytes,
    pairs: tuple[tuple[str, str], ...],
) -> tuple[LegacyLetter, ...]:
    source_sha256 = hashlib.sha256(raw).hexdigest()
    records: list[LegacyLetter] = []
    for index, pair in enumerate(pairs, start=1):
        records.append(
            LegacyLetter(
                content=_pair_archive_content(pair),
                source_record_id=f"offline-letter-pairs:{source_sha256}:{index:06d}",
                source="offline-letter-pairs",
                occurred_at=None,
                metadata={
                    "user_content": pair[0],
                    "reply_text": pair[1],
                    "import_kind": OFFLINE_LETTER_PAIR_IMPORT_KIND,
                    OFFLINE_LETTER_PAIR_PUBLISH_STATUS_KEY: OFFLINE_LETTER_PAIR_PUBLISH_STATUS,
                    OFFLINE_LETTER_PAIR_PROVENANCE_KEY: asdict(
                        OfflineLetterPairProvenance(source_sha256, index)
                    ),
                },
            )
        )
    return tuple(records)


def offline_letter_pair_exchanges(
    source_path: str | Path,
) -> tuple[HistoricalExchange, ...]:
    """Project an ordered local backup into the canonical memory migration."""

    raw, pairs = _parse_source(Path(source_path))
    source_sha256 = hashlib.sha256(raw).hexdigest()
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return tuple(
        HistoricalExchange(
            source_record_id=(
                f"offline-letter-pairs:{source_sha256}:{index:06d}"
            ),
            occurred_at=epoch + timedelta(seconds=index - 1),
            user_message=pair[0],
            assistant_message=pair[1],
        )
        for index, pair in enumerate(pairs, start=1)
    )


def _parse_source(path: Path) -> tuple[bytes, tuple[tuple[str, str], ...]]:
    raw = path.read_bytes()
    if len(raw) > _MAX_SOURCE_BYTES:
        raise ValueError("OFFLINE_LETTER_SOURCE_TOO_LARGE")
    try:
        loaded = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("OFFLINE_LETTER_SOURCE_INVALID") from exc
    if not isinstance(loaded, list) or not loaded:
        raise ValueError("OFFLINE_LETTER_SOURCE_INVALID")
    pairs: list[tuple[str, str]] = []
    for value in loaded:
        if not isinstance(value, dict) or set(value) != {"content", "reply"}:
            raise ValueError("OFFLINE_LETTER_PAIR_INVALID")
        content = value.get("content")
        reply = value.get("reply")
        if not all(isinstance(item, str) and item.strip() for item in (content, reply)):
            raise ValueError("OFFLINE_LETTER_PAIR_INVALID")
        pair = (
            _repair_reversible_utf8_latin1_mojibake(content),
            _repair_reversible_utf8_latin1_mojibake(reply),
        )
        if len(_pair_archive_content(pair)) > _MAX_PAIR_CHARS:
            raise ValueError("OFFLINE_LETTER_PAIR_TOO_LARGE")
        pairs.append(pair)
    return raw, tuple(pairs)


def _read_existing_state(
    database_path: Path,
) -> tuple[set[str], dict[str, tuple[str, ...]]]:
    if not database_path.is_file():
        return set(), {}
    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    try:
        try:
            rows = connection.execute(
                "SELECT content_hash, source_record_id, source FROM legacy_letters"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            return set(), {}
        grouped: dict[str, list[str]] = {}
        for content_hash, source_record_id, source in rows:
            if str(source) == "offline-letter-pairs":
                grouped.setdefault(str(source_record_id), []).append(str(content_hash))
        return (
            {str(row[0]) for row in rows},
            {key: tuple(values) for key, values in grouped.items()},
        )
    finally:
        connection.close()


def _recovery_plan_from_hashes(
    raw: bytes,
    pairs: tuple[tuple[str, str], ...],
    existing: set[str],
    existing_by_source: Mapping[str, str | Sequence[str]] | None = None,
) -> OfflineLetterPairRecoveryReport:
    batch: set[str] = set()
    duplicates = 0
    would_insert = 0
    would_update = 0
    source_sha256 = hashlib.sha256(raw).hexdigest()
    source_hashes = existing_by_source or {}
    for index, pair in enumerate(pairs, start=1):
        digest = hashlib.sha256(_pair_archive_content(pair).encode()).hexdigest()
        source_record_id = f"offline-letter-pairs:{source_sha256}:{index:06d}"
        stored = source_hashes.get(source_record_id)
        if stored is not None:
            old_digests = (stored,) if isinstance(stored, str) else tuple(stored)
            if len(old_digests) == 1 and old_digests[0] == digest:
                duplicates += 1
            else:
                would_update += 1
        elif digest in existing or digest in batch:
            duplicates += 1
        else:
            would_insert += 1
        batch.add(digest)
    return OfflineLetterPairRecoveryReport(
        status="dry_run",
        source_sha256=source_sha256,
        seen=len(pairs),
        accepted=len(pairs),
        would_insert=would_insert,
        would_update=would_update,
        duplicates=duplicates,
    )


def _recovery_plan(
    raw: bytes,
    pairs: tuple[tuple[str, str], ...],
    database_path: Path,
) -> OfflineLetterPairRecoveryReport:
    existing, existing_by_source = _read_existing_state(database_path)
    return _recovery_plan_from_hashes(
        raw,
        pairs,
        existing,
        existing_by_source,
    )


def plan_offline_letter_pair_recovery(
    source_path: str | Path,
    *,
    database_path: str | Path,
) -> OfflineLetterPairRecoveryReport:
    raw, pairs = _parse_source(Path(source_path))
    return _recovery_plan(raw, pairs, Path(database_path))


def plan_offline_letter_pair_recovery_with_adapter(
    source_path: str | Path,
    *,
    adapter: MemoryPort,
) -> OfflineLetterPairRecoveryReport:
    """Inspect a paired backup against the active Archive adapter."""

    raw, pairs = _parse_source(Path(source_path))
    grouped = getattr(adapter, "legacy_source_hash_groups", None)
    existing_by_source = (
        grouped("offline-letter-pairs")
        if callable(grouped)
        else adapter.legacy_source_hashes("offline-letter-pairs")
    )
    return replace(_recovery_plan_from_hashes(
        raw,
        pairs,
        adapter.legacy_content_hashes(),
        existing_by_source,
    ), would_remove=len(_adapter_obsolete_export_ids(adapter, pairs)))


def apply_offline_letter_pair_recovery(
    source_path: str | Path,
    *,
    database_path: str | Path,
) -> OfflineLetterPairRecoveryReport:
    database = Path(database_path)
    raw, pairs = _parse_source(Path(source_path))
    plan = _recovery_plan(raw, pairs, database)
    archive = LocalMemoryAdapter(database)
    try:
        obsolete_ids = _adapter_obsolete_export_ids(archive, pairs)
        result = archive.import_legacy_records(
            _build_records(raw, pairs),
            atomic=True,
            replace_matching_source_records=True,
        )
        removed = (
            archive.remove_legacy_source_records(_OBSOLETE_OFFLINE_EXPORT_SOURCE, obsolete_ids)
            if not result.rolled_back
            else 0
        )
    finally:
        archive.close()
    return replace(
        plan,
        status="rolled_back" if result.rolled_back else "committed",
        inserted=result.inserted,
        updated=result.updated,
        duplicates=result.duplicates,
        rejected=result.rejected,
        would_remove=len(obsolete_ids),
        removed=removed,
    )


def apply_offline_letter_pair_recovery_to_adapter(
    source_path: str | Path,
    *,
    adapter: MemoryPort,
) -> OfflineLetterPairRecoveryReport:
    """Import a local paired backup through the active Archive adapter."""

    raw, pairs = _parse_source(Path(source_path))
    grouped = getattr(adapter, "legacy_source_hash_groups", None)
    existing_by_source = (
        grouped("offline-letter-pairs")
        if callable(grouped)
        else adapter.legacy_source_hashes("offline-letter-pairs")
    )
    plan = _recovery_plan_from_hashes(
        raw,
        pairs,
        adapter.legacy_content_hashes(),
        existing_by_source,
    )
    obsolete_ids = _adapter_obsolete_export_ids(adapter, pairs)
    plan = replace(plan, would_remove=len(obsolete_ids))
    result = adapter.import_legacy_records(
        _build_records(raw, pairs),
        atomic=True,
        replace_matching_source_records=True,
    )
    remove_records = getattr(adapter, "remove_legacy_source_records", None)
    removed = (
        remove_records(_OBSOLETE_OFFLINE_EXPORT_SOURCE, obsolete_ids)
        if not result.rolled_back and callable(remove_records)
        else 0
    )
    return replace(
        plan,
        status="rolled_back" if result.rolled_back else "committed",
        inserted=result.inserted,
        updated=result.updated,
        duplicates=result.duplicates,
        rejected=result.rejected,
        removed=removed,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--memory-root", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    database_path = Path(args.memory_root) / "memory.sqlite3"
    try:
        report = (
            apply_offline_letter_pair_recovery(args.source, database_path=database_path)
            if args.apply
            else plan_offline_letter_pair_recovery(args.source, database_path=database_path)
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        error_code = str(exc)
        if not re.fullmatch(r"OFFLINE_LETTER_[A-Z0-9_]+", error_code):
            error_code = "OFFLINE_LETTER_SOURCE_UNAVAILABLE"
        failure = dict(error_code=error_code, history_audit="not_written",
                       provider_calls=0, read_only=True, status="rejected")
        print(json.dumps(failure, ensure_ascii=True, sort_keys=True))
        return 2
    print(json.dumps(asdict(report), ensure_ascii=True, sort_keys=True))
    return 0 if report.status in {"dry_run", "committed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
