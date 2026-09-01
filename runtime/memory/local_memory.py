"""SQLite adapter for the opt-in local memory profile.

Only the standard library is used.  The adapter has three separate tables:
legacy letter material, opt-in conversation memory, and persona evidence
references.  No table is used for more than one of those domains.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .conversation_memory_port import (
    ConversationMemoryPort,
    NullConversationMemoryPort,
    UnavailableConversationMemoryPort,
)
from .mem0_memory import (
    DeferredConversationMemoryAdapter,
    create_mem0_adapter,
    load_mem0_config,
)
from .memory_port import (
    CONVERSATION_MEMORY,
    LEGACY_LETTERS,
    MEMORY_DOMAINS,
    PERSONA_EVIDENCE,
    LegacyImportResult,
    LegacyLetter,
    MemoryPort,
    MemoryRecord,
    MemoryUnavailable,
    NullMemoryPort,
)


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}|[\u3400-\u9fff]{1,8}")
_MEMORY_USER_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MAX_TEXT_CHARS = 200_000
_MAX_METADATA_BYTES = 1_000_000
_MAX_METADATA_DEPTH = 64
_SCHEMA_VERSION = 1


def _now() -> int:
    return int(time.time())


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(value: Any, *, field: str, allow_empty: bool = False) -> str:
    """Validate text without changing the stored body."""

    if not isinstance(value, str):
        value = "" if value is None else str(value)
    if not allow_empty and not value.strip():
        raise ValueError(f"{field} is required")
    if len(value) > _MAX_TEXT_CHARS:
        raise ValueError(f"{field} is too long")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{field} has invalid unicode") from None
    return value


def _label(value: Any, *, default: str, limit: int = 512) -> str:
    text = _text(value, field="label", allow_empty=True)
    text = text.replace("\\", "/").rstrip("/")
    text = text.rsplit("/", 1)[-1]
    return text[:limit] or default


def _occurred_at(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("occurred_at is invalid")
        return str(value)
    return _text(value, field="occurred_at", allow_empty=True)[:256] or None


def _metadata_value(value: Any, *, depth: int = 0, active: set[int] | None = None) -> Any:
    """Convert metadata to a deterministic JSON-compatible value."""

    if depth > _MAX_METADATA_DEPTH:
        raise ValueError("metadata nesting exceeds limit")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        value.encode("utf-8")
        return unicodedata.normalize("NFC", value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("metadata number is not finite")
        return value

    active = active if active is not None else set()
    container = isinstance(value, (Mapping, list, tuple, set, frozenset))
    marker = id(value) if container else None
    if marker is not None:
        if marker in active:
            raise ValueError("metadata contains a cycle")
        active.add(marker)
    try:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, item in value.items():
                normalized_key = unicodedata.normalize("NFC", str(key))
                if normalized_key in result:
                    raise ValueError("metadata keys collide")
                result[normalized_key] = _metadata_value(
                    item,
                    depth=depth + 1,
                    active=active,
                )
            return result
        if isinstance(value, (list, tuple)):
            return [
                _metadata_value(item, depth=depth + 1, active=active)
                for item in value
            ]
        if isinstance(value, (set, frozenset)):
            result = [
                _metadata_value(item, depth=depth + 1, active=active)
                for item in value
            ]
            return sorted(result, key=lambda item: _metadata_json(item))
        raise ValueError("metadata contains unsupported value")
    finally:
        if marker is not None:
            active.remove(marker)


def _metadata_json(value: Any) -> str:
    normalized = _metadata_value(value)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    encoded.encode("utf-8")
    if len(encoded.encode("utf-8")) > _MAX_METADATA_BYTES:
        raise ValueError("metadata is too large")
    return encoded


def _safe_metadata(value: Mapping[str, Any] | None) -> str:
    """Store a complete object or reject it; never cut serialized JSON."""

    if value is None:
        return "{}"
    if not isinstance(value, Mapping):
        raise ValueError("metadata must be an object")
    try:
        encoded = _metadata_json(value)
        decoded = json.loads(encoded)
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
        raise ValueError("metadata invalid") from None
    if not isinstance(decoded, dict):
        raise ValueError("metadata must be an object")
    return encoded


def _load_metadata(encoded: str | None) -> dict[str, Any]:
    try:
        value = json.loads(encoded or "{}")
    except (TypeError, ValueError, UnicodeError):
        raise MemoryUnavailable("stored metadata unavailable") from None
    if not isinstance(value, dict):
        raise MemoryUnavailable("stored metadata unavailable")
    return value


def _fts_query(query: str) -> str:
    tokens = _TOKEN_RE.findall(unicodedata.normalize("NFC", query))
    return " OR ".join('"' + token.replace('"', "") + '"' for token in tokens[:24])


def _like_value(query: str) -> str:
    escaped = query[:512].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = False
    data_root: Path | None = None
    ttl_seconds: int | None = None
    context_max_chars: int = 2400
    user_id: str = "local-user"
    write_timeout_seconds: float = 30.0
    search_timeout_seconds: float = 8.0
    persona_evidence: tuple[Mapping[str, Any], ...] = ()
    provider: str = "sqlite"
    llm: Mapping[str, Any] = field(default_factory=dict)
    embedder: Mapping[str, Any] = field(default_factory=dict)
    vector_store: Mapping[str, Any] = field(default_factory=dict)
    config_error: str | None = None


class UnavailableMemoryPort(NullMemoryPort):
    """A configured memory profile whose adapter is not usable."""

    def __init__(self, reason: str = "memory unavailable", *, provider: str = "sqlite") -> None:
        self.reason = reason
        self.provider = provider

    def status(self) -> Mapping[str, Any]:
        return {
            "status": "unavailable",
            "enabled": False,
            "provider": self.provider,
            "storage": "none",
            "fts5": False,
            "vector": {"status": "not_configured", "provider": "none"},
            "network_called": False,
            "reason_code": "MEMORY_UNAVAILABLE",
        }

    def search(
        self,
        query: str,
        *,
        domains: Sequence[str] | None = None,
        limit: int = 8,
    ) -> list[MemoryRecord]:
        raise MemoryUnavailable(self.reason)

    def remember_conversation(
        self,
        summary: str,
        *,
        facts: Iterable[str] = (),
        ttl_seconds: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> str | None:
        raise MemoryUnavailable(self.reason)

    def legacy_content_hashes(self) -> set[str]:
        raise MemoryUnavailable(self.reason)

    def legacy_source_hashes(self, source: str) -> Mapping[str, str]:
        del source
        raise MemoryUnavailable(self.reason)

    def import_legacy_records(
        self,
        records: Iterable[LegacyLetter],
        *,
        atomic: bool = True,
        promote_duplicate_metadata: bool = False,
        replace_matching_source_records: bool = False,
    ) -> LegacyImportResult:
        del promote_duplicate_metadata, replace_matching_source_records
        raise MemoryUnavailable(self.reason)


class LocalMemoryAdapter:
    """Transactional local implementation of :class:`MemoryPort`."""

    provider = "sqlite"
    enabled = True

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        ttl_seconds: int | None = None,
        context_max_chars: int = 2400,
        persona_evidence: Sequence[str | Mapping[str, Any]] | None = None,
        conversation_enabled: bool = True,
        read_only: bool = False,
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self.data_root = self.db_path.parent
        self.ttl_seconds = None if ttl_seconds is None else max(1, int(ttl_seconds))
        self.context_max_chars = max(0, min(10000, int(context_max_chars)))
        self.read_only = bool(read_only)
        self.conversation_enabled = bool(conversation_enabled) and not self.read_only
        self._lock = threading.RLock()
        self._closed = False
        self._fts5 = False
        if not self.read_only:
            self._ensure_private_root()
        try:
            if self.read_only:
                target = self.db_path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
                self.connection = sqlite3.connect(
                    target,
                    uri=True,
                    check_same_thread=False,
                    isolation_level=None,
                    timeout=10.0,
                )
            else:
                self.connection = sqlite3.connect(
                    self.db_path,
                    check_same_thread=False,
                    isolation_level=None,
                    timeout=10.0,
                )
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA busy_timeout=10000")
            if self.read_only:
                self.connection.execute("PRAGMA query_only=ON")
                fts_tables = {
                    str(row[0])
                    for row in self.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' "
                        "AND name IN ('legacy_letters_fts', 'conversation_memory_fts')"
                    )
                }
                self._fts5 = fts_tables == {
                    "legacy_letters_fts",
                    "conversation_memory_fts",
                }
            else:
                self.connection.execute("PRAGMA foreign_keys=ON")
                self.connection.execute("PRAGMA synchronous=FULL")
                try:
                    self.connection.execute("PRAGMA journal_mode=WAL")
                except sqlite3.DatabaseError:
                    pass
                self._initialize_schema(persona_evidence or ())
                self._set_private_mode(self.db_path)
        except Exception:
            connection = getattr(self, "connection", None)
            if connection is not None:
                connection.close()
            raise

    def _ensure_private_root(self) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        self._set_private_mode(self.data_root, directory=True)

    @staticmethod
    def _set_private_mode(path: Path, *, directory: bool = False) -> None:
        try:
            os.chmod(path, 0o700 if directory else 0o600)
        except OSError:
            pass

    def _initialize_schema(self, persona_evidence: Sequence[str | Mapping[str, Any]]) -> None:
        with self._lock:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS legacy_letters (
                    memory_id TEXT PRIMARY KEY,
                    source_record_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    occurred_at TEXT,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    imported_at INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    read_only INTEGER NOT NULL DEFAULT 1 CHECK (read_only = 1)
                );
                CREATE INDEX IF NOT EXISTS idx_legacy_letters_order
                    ON legacy_letters(occurred_at, imported_at);
                CREATE TABLE IF NOT EXISTS conversation_memory (
                    memory_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('summary', 'fact')),
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_memory_expiry
                    ON conversation_memory(expires_at, created_at);
                CREATE TABLE IF NOT EXISTS persona_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    reference TEXT NOT NULL UNIQUE,
                    version TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    configured_at INTEGER NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS legacy_letters_no_update
                BEFORE UPDATE ON legacy_letters BEGIN
                    SELECT RAISE(ABORT, 'legacy_letters are read-only');
                END;
                CREATE TRIGGER IF NOT EXISTS legacy_letters_no_delete
                BEFORE DELETE ON legacy_letters BEGIN
                    SELECT RAISE(ABORT, 'legacy_letters require whole-library unload');
                END;
                CREATE TRIGGER IF NOT EXISTS persona_evidence_no_update
                BEFORE UPDATE ON persona_evidence BEGIN
                    SELECT RAISE(ABORT, 'persona_evidence is read-only');
                END;
                CREATE TRIGGER IF NOT EXISTS persona_evidence_no_delete
                BEFORE DELETE ON persona_evidence BEGIN
                    SELECT RAISE(ABORT, 'persona_evidence is read-only');
                END;
                """
            )
            try:
                self.connection.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS legacy_letters_fts "
                    "USING fts5(memory_id UNINDEXED, content, source)"
                )
                self.connection.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS conversation_memory_fts "
                    "USING fts5(memory_id UNINDEXED, content)"
                )
                self._fts5 = True
            except sqlite3.OperationalError:
                self._fts5 = False
            if self._fts5:
                self._rebuild_fts_if_needed()
            for item in persona_evidence:
                if isinstance(item, Mapping):
                    reference = item.get("reference", item.get("path", ""))
                    version = item.get("version", "config-v1")
                else:
                    reference = item
                    version = "config-v1"
                reference_text = _label(reference, default="persona-config")
                version_text = _text(version, field="version", allow_empty=True)[:128] or "config-v1"
                self.connection.execute(
                    "INSERT OR IGNORE INTO persona_evidence "
                    "(evidence_id, reference, version, content_hash, configured_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        uuid.uuid4().hex,
                        reference_text,
                        version_text,
                        _content_hash(reference_text + "\n" + version_text),
                        _now(),
                    ),
                )
            self.connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

    def _rebuild_fts_if_needed(self) -> None:
        legacy_ids = {
            str(row[0])
            for row in self.connection.execute("SELECT memory_id FROM legacy_letters_fts")
        }
        for row in self.connection.execute("SELECT memory_id, content, source FROM legacy_letters"):
            if str(row[0]) not in legacy_ids:
                self.connection.execute(
                    "INSERT INTO legacy_letters_fts(memory_id, content, source) VALUES (?, ?, ?)",
                    (row[0], row[1], row[2]),
                )
        conversation_ids = {
            str(row[0])
            for row in self.connection.execute("SELECT memory_id FROM conversation_memory_fts")
        }
        for row in self.connection.execute("SELECT memory_id, content FROM conversation_memory"):
            if str(row[0]) not in conversation_ids:
                self.connection.execute(
                    "INSERT INTO conversation_memory_fts(memory_id, content) VALUES (?, ?)",
                    (row[0], row[1]),
                )

    @contextmanager
    def _transaction(self):
        with self._lock:
            if self._closed:
                raise MemoryUnavailable("local memory is closed")
            self._require_writable()
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def _require_writable(self) -> None:
        if self.read_only:
            raise MemoryUnavailable("local memory is read-only")

    def _purge_expired(self) -> int:
        now = _now()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT memory_id FROM conversation_memory "
                "WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (now,),
            ).fetchall()
            if self._fts5:
                for row in rows:
                    conn.execute(
                        "DELETE FROM conversation_memory_fts WHERE memory_id = ?",
                        (row[0],),
                    )
            conn.execute(
                "DELETE FROM conversation_memory "
                "WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (now,),
            )
            return len(rows)

    def status(self) -> Mapping[str, Any]:
        with self._lock:
            if self._closed:
                return {
                    "status": "unavailable",
                    "enabled": False,
                    "provider": self.provider,
                    "storage": "sqlite",
                    "fts5": self._fts5,
                    "vector": {"status": "not_configured", "provider": "none"},
                    "network_called": False,
                    "conversation_enabled": False,
                }
            try:
                counts = self.connection.execute(
                    "SELECT (SELECT COUNT(*) FROM legacy_letters), "
                    "(SELECT COUNT(*) FROM conversation_memory), "
                    "(SELECT COUNT(*) FROM persona_evidence)"
                ).fetchone()
            except sqlite3.DatabaseError:
                raise MemoryUnavailable("local memory status unavailable") from None
            conversation_count = 0 if self.read_only else int(counts[1])
            return {
                "status": "available",
                "enabled": True,
                "provider": self.provider,
                "storage": "sqlite",
                "fts5": self._fts5,
                "vector": {"status": "not_configured", "provider": "none"},
                "network_called": False,
                "conversation_enabled": self.conversation_enabled,
                "read_only": self.read_only,
                "counts": {
                    LEGACY_LETTERS: int(counts[0]),
                    CONVERSATION_MEMORY: conversation_count,
                    PERSONA_EVIDENCE: int(counts[2]),
                },
                "ttl_seconds": self.ttl_seconds,
                "context_max_chars": self.context_max_chars,
            }

    def remember_conversation(
        self,
        summary: str,
        *,
        facts: Iterable[str] = (),
        ttl_seconds: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> str | None:
        self._require_writable()
        values = [("summary", _text(summary, field="summary"))]
        values.extend(("fact", _text(fact, field="fact")) for fact in facts)
        metadata_json = _safe_metadata(metadata)
        ttl = self.ttl_seconds if ttl_seconds is None else max(1, int(ttl_seconds))
        expires_at = _now() + ttl if ttl is not None else None
        first_id: str | None = None
        with self._transaction() as conn:
            for kind, content in values:
                digest = _content_hash(kind + "\n" + content)
                memory_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT OR IGNORE INTO conversation_memory "
                    "(memory_id, kind, content, content_hash, created_at, expires_at, metadata_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (memory_id, kind, content, digest, _now(), expires_at, metadata_json),
                )
                row = conn.execute(
                    "SELECT memory_id, content FROM conversation_memory WHERE content_hash = ?",
                    (digest,),
                ).fetchone()
                if row is not None and first_id is None:
                    first_id = str(row[0])
                if self._fts5 and row is not None:
                    fts_row = conn.execute(
                        "SELECT 1 FROM conversation_memory_fts WHERE memory_id = ?",
                        (row[0],),
                    ).fetchone()
                    if fts_row is None:
                        conn.execute(
                            "INSERT INTO conversation_memory_fts(memory_id, content) VALUES (?, ?)",
                            (row[0], row[1]),
                        )
        return first_id

    def clear_conversation(self) -> int:
        with self._transaction() as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM conversation_memory").fetchone()[0])
            if self._fts5:
                conn.execute("DELETE FROM conversation_memory_fts")
            conn.execute("DELETE FROM conversation_memory")
            return count

    def purge_expired(self) -> int:
        return self._purge_expired()

    def _validate_legacy(self, record: LegacyLetter) -> tuple[str, str, str, str | None, str, str]:
        if not isinstance(record, LegacyLetter):
            raise ValueError("legacy record invalid")
        content = _text(record.content, field="content")
        source_record_id = _label(record.source_record_id, default="record")
        source = _label(record.source, default="local-import")
        occurred_at = _occurred_at(record.occurred_at)
        digest = _content_hash(content)
        metadata = _safe_metadata(record.metadata)
        return content, source_record_id, source, occurred_at, digest, metadata

    def legacy_content_hashes(self) -> set[str]:
        with self._lock:
            rows = self.connection.execute("SELECT content_hash FROM legacy_letters").fetchall()
            return {str(row[0]) for row in rows}

    def legacy_source_hashes(self, source: str) -> Mapping[str, str]:
        source_name = _label(source, default="local-import")
        with self._lock:
            rows = self.connection.execute(
                "SELECT source_record_id, content_hash FROM legacy_letters WHERE source = ?",
                (source_name,),
            ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def legacy_source_hash_groups(self, source: str) -> Mapping[str, tuple[str, ...]]:
        """Return every stored digest so repair imports can detect old duplicates."""

        source_name = _label(source, default="local-import")
        with self._lock:
            rows = self.connection.execute(
                "SELECT source_record_id, content_hash FROM legacy_letters "
                "WHERE source = ? ORDER BY imported_at, memory_id",
                (source_name,),
            ).fetchall()
        grouped: dict[str, list[str]] = {}
        for source_record_id, content_hash in rows:
            grouped.setdefault(str(source_record_id), []).append(str(content_hash))
        return {key: tuple(values) for key, values in grouped.items()}

    def _promote_legacy_duplicate(
        self,
        conn: sqlite3.Connection,
        *,
        source_record_id: str,
        source: str,
        occurred_at: str | None,
        digest: str,
        metadata: str,
    ) -> None:
        row = conn.execute(
            "SELECT memory_id FROM legacy_letters WHERE content_hash = ?",
            (digest,),
        ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError("legacy duplicate disappeared")
        memory_id = str(row[0])
        conn.execute("DROP TRIGGER IF EXISTS legacy_letters_no_update")
        try:
            conn.execute(
                """
                UPDATE legacy_letters
                SET source_record_id = ?, source = ?, occurred_at = ?, metadata_json = ?
                WHERE memory_id = ?
                """,
                (source_record_id, source, occurred_at, metadata, memory_id),
            )
            if self._fts5:
                conn.execute(
                    "UPDATE legacy_letters_fts SET source = ? WHERE memory_id = ?",
                    (source, memory_id),
                )
        finally:
            conn.execute(
                "CREATE TRIGGER legacy_letters_no_update BEFORE UPDATE ON legacy_letters "
                "BEGIN SELECT RAISE(ABORT, 'legacy_letters are read-only'); END"
            )

    def _replace_legacy_source_record(
        self,
        conn: sqlite3.Connection,
        *,
        content: str,
        source_record_id: str,
        source: str,
        occurred_at: str | None,
        digest: str,
        metadata: str,
    ) -> tuple[frozenset[str], bool] | None:
        rows = conn.execute(
            "SELECT memory_id, content_hash FROM legacy_letters "
            "WHERE source_record_id = ? AND source = ? "
            "ORDER BY imported_at, memory_id",
            (source_record_id, source),
        ).fetchall()
        if not rows:
            return None
        normalized_rows = tuple((str(row[0]), str(row[1])) for row in rows)
        canonical = next(
            (row for row in normalized_rows if row[1] == digest),
            normalized_rows[0],
        )
        memory_id, canonical_digest = canonical
        removed = tuple(row for row in normalized_rows if row[0] != memory_id)
        collision = conn.execute(
            "SELECT 1 FROM legacy_letters WHERE content_hash = ? "
            "AND memory_id NOT IN ("
            "SELECT memory_id FROM legacy_letters WHERE source_record_id = ? AND source = ?"
            ")",
            (digest, source_record_id, source),
        ).fetchone()
        if collision is not None:
            raise sqlite3.IntegrityError("legacy replacement content already exists")
        conn.execute("DROP TRIGGER IF EXISTS legacy_letters_no_update")
        conn.execute("DROP TRIGGER IF EXISTS legacy_letters_no_delete")
        try:
            if canonical_digest != digest:
                conn.execute(
                    "UPDATE legacy_letters SET content = ?, content_hash = ?, occurred_at = ?, "
                    "metadata_json = ? WHERE memory_id = ?",
                    (content, digest, occurred_at, metadata, memory_id),
                )
                if self._fts5:
                    conn.execute(
                        "UPDATE legacy_letters_fts SET content = ?, source = ? WHERE memory_id = ?",
                        (content, source, memory_id),
                    )
            if removed:
                removed_ids = tuple(row[0] for row in removed)
                placeholders = ",".join("?" for _ in removed_ids)
                if self._fts5:
                    conn.execute(
                        f"DELETE FROM legacy_letters_fts WHERE memory_id IN ({placeholders})",
                        removed_ids,
                    )
                conn.execute(
                    f"DELETE FROM legacy_letters WHERE memory_id IN ({placeholders})",
                    removed_ids,
                )
        finally:
            conn.execute(
                "CREATE TRIGGER legacy_letters_no_update BEFORE UPDATE ON legacy_letters "
                "BEGIN SELECT RAISE(ABORT, 'legacy_letters are read-only'); END"
            )
            conn.execute(
                "CREATE TRIGGER legacy_letters_no_delete BEFORE DELETE ON legacy_letters "
                "BEGIN SELECT RAISE(ABORT, 'legacy_letters require whole-library unload'); END"
            )
        return (
            frozenset(row[1] for row in normalized_rows),
            canonical_digest != digest or bool(removed),
        )

    def import_legacy_records(
        self,
        records: Iterable[LegacyLetter],
        *,
        atomic: bool = True,
        promote_duplicate_metadata: bool = False,
        replace_matching_source_records: bool = False,
    ) -> LegacyImportResult:
        self._require_writable()
        materialized = list(records)
        normalized: list[tuple[str, str, str, str | None, str, str]] = []
        rejected = 0
        for record in materialized:
            try:
                normalized.append(self._validate_legacy(record))
            except (TypeError, ValueError, UnicodeError):
                rejected += 1
        if rejected and atomic:
            return LegacyImportResult(
                seen=len(materialized),
                rejected=rejected,
                rolled_back=True,
            )
        inserted = 0
        updated = 0
        duplicates = 0
        try:
            with self._transaction() as conn:
                existing = {
                    str(row[0])
                    for row in conn.execute("SELECT content_hash FROM legacy_letters")
                }
                batch: set[str] = set()
                for content, source_record_id, source, occurred_at, digest, metadata in normalized:
                    if replace_matching_source_records:
                        replacement = self._replace_legacy_source_record(
                            conn,
                            content=content,
                            source_record_id=source_record_id,
                            source=source,
                            occurred_at=occurred_at,
                            digest=digest,
                            metadata=metadata,
                        )
                        if replacement is not None:
                            old_digests, changed = replacement
                            for old_digest in old_digests:
                                existing.discard(old_digest)
                            existing.add(digest)
                            if changed:
                                updated += 1
                            else:
                                duplicates += 1
                            batch.add(digest)
                            continue
                    if digest in existing or digest in batch:
                        if promote_duplicate_metadata and digest in existing:
                            self._promote_legacy_duplicate(
                                conn,
                                source_record_id=source_record_id,
                                source=source,
                                occurred_at=occurred_at,
                                digest=digest,
                                metadata=metadata,
                            )
                        duplicates += 1
                        batch.add(digest)
                        continue
                    self._insert_legacy_row(
                        conn,
                        content=content,
                        source_record_id=source_record_id,
                        source=source,
                        occurred_at=occurred_at,
                        digest=digest,
                        metadata=metadata,
                    )
                    existing.add(digest)
                    batch.add(digest)
                    inserted += 1
        except Exception:
            return LegacyImportResult(
                seen=len(materialized),
                inserted=0,
                updated=0,
                duplicates=duplicates,
                rejected=rejected,
                rolled_back=True,
            )
        return LegacyImportResult(
            seen=len(materialized),
            inserted=inserted,
            updated=updated,
            duplicates=duplicates,
            rejected=rejected,
        )

    def _insert_legacy_row(
        self,
        conn: sqlite3.Connection,
        *,
        content: str,
        source_record_id: str,
        source: str,
        occurred_at: str | None,
        digest: str,
        metadata: str,
    ) -> None:
        memory_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO legacy_letters "
            "(memory_id, source_record_id, source, occurred_at, content, content_hash, imported_at, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (memory_id, source_record_id, source, occurred_at, content, digest, _now(), metadata),
        )
        if self._fts5:
            conn.execute(
                "INSERT INTO legacy_letters_fts(memory_id, content, source) VALUES (?, ?, ?)",
                (memory_id, content, source),
            )

    def unload_legacy(self) -> int:
        """Delete the complete legacy domain, preserving all other domains."""

        with self._transaction() as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM legacy_letters").fetchone()[0])
            conn.execute("DROP TRIGGER IF EXISTS legacy_letters_no_delete")
            conn.execute("DROP TRIGGER IF EXISTS legacy_letters_no_update")
            if self._fts5:
                conn.execute("DELETE FROM legacy_letters_fts")
            conn.execute("DELETE FROM legacy_letters")
            conn.execute(
                "CREATE TRIGGER legacy_letters_no_update BEFORE UPDATE ON legacy_letters "
                "BEGIN SELECT RAISE(ABORT, 'legacy_letters are read-only'); END"
            )
            conn.execute(
                "CREATE TRIGGER legacy_letters_no_delete BEFORE DELETE ON legacy_letters "
                "BEGIN SELECT RAISE(ABORT, 'legacy_letters require whole-library unload'); END"
            )
            return count

    def _rows_for_domain(self, query: str, domain: str, limit: int) -> list[sqlite3.Row]:
        table = "legacy_letters" if domain == LEGACY_LETTERS else "conversation_memory"
        fts_table = "legacy_letters_fts" if domain == LEGACY_LETTERS else "conversation_memory_fts"
        order_column = "imported_at" if domain == LEGACY_LETTERS else "created_at"
        fts = _fts_query(query)
        with self._lock:
            if self._fts5 and fts:
                try:
                    rows = self.connection.execute(
                        f"SELECT m.* FROM {table} AS m "
                        f"JOIN {fts_table} ON {fts_table}.memory_id = m.memory_id "
                        f"WHERE {fts_table} MATCH ? ORDER BY m.{order_column} DESC LIMIT ?",
                        (fts, limit),
                    ).fetchall()
                    if rows:
                        return rows
                except sqlite3.OperationalError:
                    pass
            if query.strip():
                return self.connection.execute(
                    f"SELECT * FROM {table} WHERE content LIKE ? ESCAPE '\\' "
                    f"ORDER BY {order_column} DESC LIMIT ?",
                    (_like_value(query), limit),
                ).fetchall()
            return self.connection.execute(
                f"SELECT * FROM {table} ORDER BY {order_column} DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def search(
        self,
        query: str,
        *,
        domains: Sequence[str] | None = None,
        limit: int = 8,
    ) -> list[MemoryRecord]:
        if not isinstance(query, str):
            raise ValueError("query must be text")
        requested = tuple(domains or (CONVERSATION_MEMORY, LEGACY_LETTERS))
        if any(domain not in {CONVERSATION_MEMORY, LEGACY_LETTERS} for domain in requested):
            raise ValueError("persona_evidence is not searchable")
        selected = (LEGACY_LETTERS,) if self.read_only else requested
        limit = max(1, min(100, int(limit)))
        if not self.read_only:
            self._purge_expired()
        query_tokens = {item.casefold() for item in _TOKEN_RE.findall(query)}
        result: list[MemoryRecord] = []
        seen: set[str] = set()
        for domain in selected:
            for row in self._rows_for_domain(query, domain, limit):
                memory_id = str(row["memory_id"])
                if memory_id in seen:
                    continue
                seen.add(memory_id)
                if domain == LEGACY_LETTERS:
                    text = str(row["content"])
                    digest = str(row["content_hash"])
                    created_at = int(row["imported_at"])
                    occurred_at = row["occurred_at"]
                    source = str(row["source"])
                    expires_at = None
                    metadata = _load_metadata(row["metadata_json"])
                    provenance = {
                        "domain": domain,
                        "source": source,
                        "source_record_id": str(row["source_record_id"]),
                        "occurred_at": occurred_at,
                        "content_hash": digest,
                        "read_only": True,
                        "current_conversation": False,
                    }
                else:
                    text = str(row["content"])
                    digest = str(row["content_hash"])
                    created_at = int(row["created_at"])
                    occurred_at = None
                    source = "conversation-memory"
                    expires_at = int(row["expires_at"]) if row["expires_at"] is not None else None
                    metadata = _load_metadata(row["metadata_json"])
                    provenance = {
                        "domain": domain,
                        "kind": str(row["kind"]),
                        "content_hash": digest,
                        "expires_at": expires_at,
                        "current_conversation": True,
                    }
                score = float(sum(1 for token in query_tokens if token in text.casefold()))
                result.append(
                    MemoryRecord(
                        memory_id=memory_id,
                        domain=domain,
                        text=text,
                        source=source,
                        created_at=created_at,
                        occurred_at=occurred_at,
                        expires_at=expires_at,
                        content_hash=digest,
                        score=score,
                        provenance=provenance,
                        metadata=metadata,
                    )
                )
        result.sort(key=lambda item: (-item.score, -item.created_at, item.domain, item.memory_id))
        return result[:limit]

    def list_legacy(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT memory_id, source_record_id, source, occurred_at, content, "
                "content_hash, imported_at, metadata_json FROM legacy_letters "
                "ORDER BY imported_at DESC, memory_id"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            metadata = _load_metadata(row["metadata_json"])
            stored_content = str(row["content"])
            user_content = metadata.get("user_content")
            reply_text = metadata.get("reply_text")
            result.append({
                "letter_id": str(row["memory_id"]),
                "source_record_id": str(row["source_record_id"]),
                "source": str(row["source"]),
                "created_at": row["occurred_at"] or int(row["imported_at"]),
                "content": user_content if isinstance(user_content, str) else stored_content,
                "content_hash": str(row["content_hash"]),
                "metadata": metadata,
                "reply_text": reply_text if isinstance(reply_text, str) else "",
                "replied_at": metadata.get("replied_at"),
                "is_read": 0,
                "read_only": True,
            })
        return result

    def persona_evidence(self) -> list[Mapping[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT evidence_id, reference, version, content_hash, configured_at "
                "FROM persona_evidence ORDER BY configured_at, evidence_id"
            ).fetchall()
        return [
            {
                "evidence_id": str(row["evidence_id"]),
                "reference": str(row["reference"]),
                "version": str(row["version"]),
                "content_hash": str(row["content_hash"]),
                "configured_at": int(row["configured_at"]),
                "read_only": True,
                "writes_facts": False,
            }
            for row in rows
        ]

    def export_records(
        self,
        *,
        domains: Sequence[str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        if domains is None:
            raise ValueError("export domains must be explicit")
        selected = tuple(dict.fromkeys(domains))
        if any(domain not in MEMORY_DOMAINS for domain in selected):
            raise ValueError("unknown memory domain")
        if self.read_only:
            selected = tuple(domain for domain in selected if domain != CONVERSATION_MEMORY)
        if CONVERSATION_MEMORY in selected and not self.read_only:
            self._purge_expired()
        result: dict[str, list[dict[str, Any]]] = {}
        with self._lock:
            if LEGACY_LETTERS in selected:
                rows = self.connection.execute(
                    "SELECT memory_id, source_record_id, source, occurred_at, content, "
                    "content_hash, imported_at, metadata_json FROM legacy_letters "
                    "ORDER BY imported_at, memory_id"
                ).fetchall()
                result[LEGACY_LETTERS] = [
                    {
                        "memory_id": str(row["memory_id"]),
                        "source_record_id": str(row["source_record_id"]),
                        "source": str(row["source"]),
                        "occurred_at": row["occurred_at"],
                        "content": str(row["content"]),
                        "content_hash": str(row["content_hash"]),
                        "imported_at": int(row["imported_at"]),
                        "metadata": _load_metadata(row["metadata_json"]),
                        "read_only": True,
                    }
                    for row in rows
                ]
            if CONVERSATION_MEMORY in selected:
                rows = self.connection.execute(
                    "SELECT memory_id, kind, content, content_hash, created_at, "
                    "expires_at, metadata_json FROM conversation_memory "
                    "ORDER BY created_at, memory_id"
                ).fetchall()
                result[CONVERSATION_MEMORY] = [
                    {
                        "memory_id": str(row["memory_id"]),
                        "kind": str(row["kind"]),
                        "content": str(row["content"]),
                        "content_hash": str(row["content_hash"]),
                        "created_at": int(row["created_at"]),
                        "expires_at": int(row["expires_at"]) if row["expires_at"] is not None else None,
                        "metadata": _load_metadata(row["metadata_json"]),
                    }
                    for row in rows
                ]
            if PERSONA_EVIDENCE in selected:
                result[PERSONA_EVIDENCE] = [dict(item) for item in self.persona_evidence()]
        json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
        return result

    def export_json(self, *, domains: Sequence[str] | None = None) -> str:
        payload = self.export_records(domains=domains)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)

    def uninstall(
        self,
        *,
        delete_conversation: bool = False,
        delete_legacy: bool = False,
    ) -> Mapping[str, Any]:
        self._require_writable()
        conversation_count = self.clear_conversation() if delete_conversation else 0
        legacy_count = self.unload_legacy() if delete_legacy else 0
        return {
            "status": "available",
            "conversation_deleted": bool(conversation_count),
            "conversation_deleted_count": conversation_count,
            "legacy_deleted": bool(legacy_count),
            "legacy_deleted_count": legacy_count,
            "legacy_delete_requested": bool(delete_legacy),
            "legacy_delete_scope": "whole_library" if delete_legacy else None,
            "persona_evidence_deleted": False,
        }

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self.connection.close()
                self._closed = True

    def __enter__(self) -> "LocalMemoryAdapter":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _integer(value: Any, *, minimum: int, maximum: int | None = None) -> int | None:
    if value in (None, ""):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    if result < minimum or (maximum is not None and result > maximum):
        return None
    return result


def _duration(value: Any, *, default: float) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not 0.1 <= result <= 300:
        return None
    return result


def load_memory_config(
    path: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> MemoryConfig:
    environ = environ if environ is not None else os.environ
    project_root = root or Path(__file__).resolve().parents[2]
    config_path = Path(path) if path is not None else project_root / "memory_config.json"
    data: dict[str, Any] = {}
    config_error: str | None = None
    if config_path.is_file():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, Mapping):
                config_error = "MEMORY_CONFIG_INVALID"
            else:
                data.update(loaded)
        except (OSError, UnicodeError, json.JSONDecodeError):
            config_error = "MEMORY_CONFIG_INVALID"
    for name, key in (
        ("OLIVIA_MEMORY_ENABLED", "enabled"),
        ("OLIVIA_MEMORY_ROOT", "data_root"),
        ("OLIVIA_MEMORY_TTL_SECONDS", "ttl_seconds"),
        ("OLIVIA_MEMORY_CONTEXT_MAX_CHARS", "context_max_chars"),
        ("OLIVIA_MEMORY_PROVIDER", "provider"),
        ("OLIVIA_MEMORY_USER_ID", "user_id"),
        ("OLIVIA_MEMORY_WRITE_TIMEOUT_SECONDS", "write_timeout_seconds"),
        ("OLIVIA_MEMORY_SEARCH_TIMEOUT_SECONDS", "search_timeout_seconds"),
    ):
        if name in environ:
            data[key] = environ[name]
    enabled = _as_bool(data.get("enabled", False))
    default_provider = environ.get("OLIVIA_MEMORY_DEFAULT_PROVIDER", "sqlite")
    provider = str(data.get("provider", default_provider)).strip().casefold() or "sqlite"
    if provider not in {"sqlite", "mem0", "none"}:
        config_error = "MEMORY_PROVIDER_INVALID"
    configured_root = data.get("data_root")
    if configured_root:
        data_root = Path(str(configured_root)).expanduser()
        if not data_root.is_absolute():
            data_root = project_root / data_root
    else:
        data_root = project_root / ".olivia_data" / "memory"
    ttl_raw = data.get("ttl_seconds")
    ttl = None if ttl_raw in (None, "", 0, "0") else _integer(ttl_raw, minimum=1)
    if ttl_raw not in (None, "", 0, "0") and ttl is None:
        config_error = "MEMORY_TTL_INVALID"
    context_raw = data.get("context_max_chars", 2400)
    context_max = _integer(context_raw, minimum=0, maximum=10000)
    if context_max is None:
        context_max = 2400
        if context_raw not in (None, ""):
            config_error = "MEMORY_CONTEXT_LIMIT_INVALID"
    user_id = str(data.get("user_id", "local-user")).strip()
    if not _MEMORY_USER_ID_RE.fullmatch(user_id):
        user_id = ""
        config_error = "MEMORY_USER_ID_INVALID"
    write_timeout = _duration(data.get("write_timeout_seconds", 30), default=30.0)
    if write_timeout is None:
        write_timeout = 30.0
        config_error = "MEMORY_WRITE_TIMEOUT_INVALID"
    search_timeout = _duration(data.get("search_timeout_seconds", 8), default=8.0)
    if search_timeout is None:
        search_timeout = 8.0
        config_error = "MEMORY_SEARCH_TIMEOUT_INVALID"
    refs: list[Mapping[str, Any]] = []
    raw_refs = data.get("persona_evidence", [])
    if isinstance(raw_refs, (list, tuple)):
        for ref in raw_refs:
            if isinstance(ref, Mapping):
                refs.append(dict(ref))
            elif isinstance(ref, str):
                refs.append({"reference": ref, "version": "config-v1"})
    def section(name: str) -> Mapping[str, Any]:
        nonlocal config_error
        value = data.get(name, {})
        if not isinstance(value, Mapping):
            config_error = "MEMORY_MEM0_CONFIG_INVALID"
            return {}
        return dict(value)

    return MemoryConfig(
        enabled=enabled,
        data_root=data_root,
        ttl_seconds=ttl,
        context_max_chars=context_max,
        user_id=user_id,
        write_timeout_seconds=write_timeout,
        search_timeout_seconds=search_timeout,
        persona_evidence=tuple(refs),
        provider=provider,
        llm=section("llm"),
        embedder=section("embedder"),
        vector_store=section("vector_store"),
        config_error=config_error,
    )


def create_memory_adapter(
    config: MemoryConfig | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    allow_legacy_create: bool = False,
) -> MemoryPort:
    """Create the optional local adapter without affecting core startup."""

    config = config or load_memory_config(environ=environ)
    if config.config_error:
        return UnavailableMemoryPort(config.config_error, provider=config.provider)
    if config.provider == "mem0":
        return UnavailableMemoryPort("mem0 adapter unavailable", provider="mem0")
    if config.provider == "none":
        return NullMemoryPort()
    if config.data_root is None:
        return UnavailableMemoryPort("memory root unavailable")
    archive_root = _archive_data_root(config.data_root)
    db_path = archive_root / "memory.sqlite3"
    if not config.enabled and not allow_legacy_create and not db_path.is_file():
        return NullMemoryPort()
    try:
        return LocalMemoryAdapter(
            db_path,
            ttl_seconds=config.ttl_seconds,
            context_max_chars=config.context_max_chars,
            persona_evidence=config.persona_evidence,
            conversation_enabled=config.enabled,
            read_only=not config.enabled and not allow_legacy_create,
        )
    except (OSError, sqlite3.Error, ValueError):
        return UnavailableMemoryPort("sqlite adapter unavailable", provider="sqlite")


def _archive_data_root(data_root: Path) -> Path:
    """Keep Archive SQLite outside an explicit Mem0 lifecycle root."""

    return data_root.parent if data_root.name.casefold() == "mem0" else data_root


def _conversation_state_root(data_root: Path) -> Path:
    archive_root = _archive_data_root(data_root)
    return (
        archive_root.parent
        if archive_root.name.casefold() == "memory"
        else archive_root
    )


def create_conversation_memory_adapter(
    config: MemoryConfig | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    llm_fallback: Mapping[str, str] | None = None,
    defer_initialization: bool = False,
) -> ConversationMemoryPort:
    """Select the optional conversation-memory provider without crossing Archive.

    ``create_memory_adapter`` remains the SQLite/Archive factory.  Mem0 gets
    its own narrow port and a sibling data root so a configured provider never
    turns legacy Archive records into conversation-memory records.
    """

    active = config or load_memory_config(environ=environ)
    if active.config_error:
        return UnavailableConversationMemoryPort(active.config_error, config=active)
    if not active.enabled or active.provider != "mem0":
        return NullConversationMemoryPort()
    if active.data_root is None:
        return UnavailableConversationMemoryPort(
            "MEM0_DATA_ROOT_NOT_CONFIGURED", config=active
        )

    mem0_environment = dict(environ) if environ is not None else dict(os.environ)
    mem0_environment["OLIVIA_MEMORY_ENABLED"] = "true"
    mem0_root = (
        active.data_root
        if active.data_root.name.casefold() == "mem0"
        else active.data_root / "mem0"
    )
    mem0_environment["OLIVIA_MEMORY_ROOT"] = str(mem0_root)
    mem0_environment["OLIVIA_MEMORY_OUTBOX_DATA_ROOT"] = str(
        _conversation_state_root(active.data_root)
    )
    mem0_environment["OLIVIA_MEMORY_CONTEXT_MAX_CHARS"] = str(
        active.context_max_chars
    )
    mem0_environment["OLIVIA_MEMORY_USER_ID"] = active.user_id
    mem0_environment["OLIVIA_MEMORY_WRITE_TIMEOUT_SECONDS"] = format(
        active.write_timeout_seconds, "g"
    )
    mem0_environment["OLIVIA_MEMORY_SEARCH_TIMEOUT_SECONDS"] = format(
        active.search_timeout_seconds, "g"
    )
    for section, settings in (
        (
            active.llm,
            (
                ("provider", "OLIVIA_MEMORY_LLM_PROVIDER"),
                ("base_url", "OLIVIA_MEMORY_LLM_BASE_URL"),
                ("model", "OLIVIA_MEMORY_LLM_MODEL"),
                ("api_key_env", "OLIVIA_MEMORY_LLM_API_KEY_ENV"),
            ),
        ),
        (
            active.embedder,
            (
                ("provider", "OLIVIA_MEMORY_EMBEDDER_PROVIDER"),
                ("model", "OLIVIA_MEMORY_EMBEDDING_MODEL"),
                ("device", "OLIVIA_MEMORY_EMBEDDING_DEVICE"),
                ("embedding_dims", "OLIVIA_MEMORY_EMBEDDING_DIMS"),
            ),
        ),
        (
            active.vector_store,
            (
                ("provider", "OLIVIA_MEMORY_VECTOR_STORE_PROVIDER"),
                ("collection_name", "OLIVIA_MEMORY_COLLECTION"),
                ("on_disk", "OLIVIA_MEMORY_VECTOR_STORE_ON_DISK"),
            ),
        ),
    ):
        for config_name, environment_name in settings:
            if environment_name in mem0_environment:
                continue
            value = section.get(config_name)
            if isinstance(value, str) and value.strip():
                mem0_environment[environment_name] = value.strip()
            elif isinstance(value, bool):
                mem0_environment[environment_name] = "true" if value else "false"
            elif isinstance(value, int) and not isinstance(value, bool):
                mem0_environment[environment_name] = str(value)
    fallback = llm_fallback or {}
    for memory_name, gateway_name, default_name, field_name in (
        ("OLIVIA_MEMORY_LLM_BASE_URL", "OLIVIA_LLM_BASE_URL", "OLIVIA_MEMORY_LLM_DEFAULT_BASE_URL", "base_url"),
        ("OLIVIA_MEMORY_LLM_MODEL", "OLIVIA_LLM_MODEL", "OLIVIA_MEMORY_LLM_DEFAULT_MODEL", "model"),
        ("OLIVIA_MEMORY_LLM_API_KEY_ENV", "OLIVIA_LLM_API_KEY_ENV", "OLIVIA_MEMORY_LLM_DEFAULT_API_KEY_ENV", "api_key_env"),
    ):
        if mem0_environment.get(memory_name) or mem0_environment.get(gateway_name):
            continue
        value = fallback.get(field_name, "") or mem0_environment.get(default_name, "")
        if isinstance(value, str) and value.strip():
            mem0_environment[memory_name] = value.strip()
    try:
        if defer_initialization:
            mem0_config = load_mem0_config(environ=mem0_environment)
            return DeferredConversationMemoryAdapter(
                mem0_config,
                lambda: create_mem0_adapter(
                    config=mem0_config,
                    environ=mem0_environment,
                ),
            )
        return create_mem0_adapter(environ=mem0_environment)
    except Exception:
        return UnavailableConversationMemoryPort(
            "MEM0_INITIALIZATION_FAILED", config=active
        )


__all__ = [
    "LocalMemoryAdapter",
    "MemoryConfig",
    "UnavailableMemoryPort",
    "create_conversation_memory_adapter",
    "create_memory_adapter",
    "load_memory_config",
]
