"""Restore paired local letters into the read-only archive without providers."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Mapping, Sequence

from runtime.memory.local_memory import LocalMemoryAdapter
from runtime.memory.memory_port import LegacyLetter


_MAX_SOURCE_BYTES = 16 * 1024 * 1024
_MAX_PAIR_CHARS = 200_000
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

OFFLINE_LETTER_PAIR_IMPORT_KIND = "offline_recovered_text_reply"
OFFLINE_LETTER_PAIR_PUBLISH_STATUS_KEY = "offline_mailbox_publish_status"
OFFLINE_LETTER_PAIR_PUBLISH_STATUS = "validated_pair_v1"
OFFLINE_LETTER_PAIR_PROVENANCE_KEY = "offline_letter_pair_provenance"


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
        pair = (content, reply)
        if len(_pair_archive_content(pair)) > _MAX_PAIR_CHARS:
            raise ValueError("OFFLINE_LETTER_PAIR_TOO_LARGE")
        pairs.append(pair)
    return raw, tuple(pairs)


def _read_existing_hashes(database_path: Path) -> set[str]:
    if not database_path.is_file():
        return set()
    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    try:
        try:
            rows = connection.execute("SELECT content_hash FROM legacy_letters").fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            return set()
        return {str(row[0]) for row in rows}
    finally:
        connection.close()


def _recovery_plan(
    raw: bytes,
    pairs: tuple[tuple[str, str], ...],
    database_path: Path,
) -> OfflineLetterPairRecoveryReport:
    existing = _read_existing_hashes(database_path)
    batch: set[str] = set()
    duplicates = 0
    for pair in pairs:
        digest = hashlib.sha256(_pair_archive_content(pair).encode()).hexdigest()
        if digest in existing or digest in batch:
            duplicates += 1
        batch.add(digest)
    return OfflineLetterPairRecoveryReport(
        status="dry_run",
        source_sha256=hashlib.sha256(raw).hexdigest(),
        seen=len(pairs),
        accepted=len(pairs),
        would_insert=len(pairs) - duplicates,
        duplicates=duplicates,
    )


def plan_offline_letter_pair_recovery(
    source_path: str | Path,
    *,
    database_path: str | Path,
) -> OfflineLetterPairRecoveryReport:
    raw, pairs = _parse_source(Path(source_path))
    return _recovery_plan(raw, pairs, Path(database_path))


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
        result = archive.import_legacy_records(
            _build_records(raw, pairs),
            atomic=True,
        )
    finally:
        archive.close()
    return replace(
        plan,
        status="rolled_back" if result.rolled_back else "committed",
        inserted=result.inserted,
        duplicates=result.duplicates,
        rejected=result.rejected,
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
