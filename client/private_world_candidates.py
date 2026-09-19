"""Bounded pending-candidate storage for controlled PrivateWorld review."""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
import hashlib
from pathlib import Path
import re
import sqlite3
from typing import Iterator

from private_world_commands import PrivateWorldActor


PRIVATE_WORLD_CANDIDATE_SCHEMA_VERSION = 1
_CANDIDATE_METADATA_KEY = "candidate_schema_version"
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MAX_SUMMARY_LENGTH = 280
_MAX_REASON_LENGTH = 280


class PrivateWorldCandidateError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CandidateType(StrEnum):
    BOUNDARY_RESPECTED = "boundary_respected"
    CONFLICT = "conflict"
    REPAIR = "repair"


class CandidateStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class CandidateDecisionKind(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class CandidateWriteStatus(StrEnum):
    CREATED = "CREATED"
    DUPLICATE = "DUPLICATE"


class CandidateDecisionWriteStatus(StrEnum):
    RECORDED = "RECORDED"
    DUPLICATE = "DUPLICATE"


def _identifier(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise PrivateWorldCandidateError(code)
    return value


def _text(value: object, *, code: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise PrivateWorldCandidateError(code)
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in normalized
        )
    ):
        raise PrivateWorldCandidateError(code)
    return normalized


def _aware(value: object, *, code: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise PrivateWorldCandidateError(code)
    return value


def _parse_time(value: object, *, code: str) -> datetime:
    if not isinstance(value, str):
        raise PrivateWorldCandidateError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PrivateWorldCandidateError(code) from exc
    return _aware(parsed, code=code)


def candidate_identity(
    source_letter_id: str,
    source_reply_revision: int,
    candidate_type: CandidateType,
) -> str:
    letter_id = _identifier(
        source_letter_id,
        code="PRIVATE_WORLD_CANDIDATE_SOURCE_INVALID",
    )
    if (
        type(source_reply_revision) is not int
        or source_reply_revision < 1
    ):
        raise PrivateWorldCandidateError(
            "PRIVATE_WORLD_CANDIDATE_REVISION_INVALID"
        )
    if not isinstance(candidate_type, CandidateType):
        raise PrivateWorldCandidateError(
            "PRIVATE_WORLD_CANDIDATE_TYPE_INVALID"
        )
    material = (
        f"{letter_id}:{source_reply_revision}:{candidate_type.value}"
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"candidate.{digest}"


@dataclass(frozen=True)
class PrivateWorldCandidate:
    candidate_id: str
    source_letter_id: str
    source_reply_revision: int
    candidate_type: CandidateType
    summary: str
    confidence: float
    status: CandidateStatus
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate_id",
            _identifier(
                self.candidate_id,
                code="PRIVATE_WORLD_CANDIDATE_ID_INVALID",
            ),
        )
        object.__setattr__(
            self,
            "source_letter_id",
            _identifier(
                self.source_letter_id,
                code="PRIVATE_WORLD_CANDIDATE_SOURCE_INVALID",
            ),
        )
        if (
            type(self.source_reply_revision) is not int
            or self.source_reply_revision < 1
        ):
            raise PrivateWorldCandidateError(
                "PRIVATE_WORLD_CANDIDATE_REVISION_INVALID"
            )
        if not isinstance(self.candidate_type, CandidateType):
            raise PrivateWorldCandidateError(
                "PRIVATE_WORLD_CANDIDATE_TYPE_INVALID"
            )
        if not isinstance(self.status, CandidateStatus):
            raise PrivateWorldCandidateError(
                "PRIVATE_WORLD_CANDIDATE_STATUS_INVALID"
            )
        object.__setattr__(
            self,
            "summary",
            _text(
                self.summary,
                code="PRIVATE_WORLD_CANDIDATE_SUMMARY_INVALID",
                maximum=_MAX_SUMMARY_LENGTH,
            ),
        )
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not 0 <= float(self.confidence) <= 1
        ):
            raise PrivateWorldCandidateError(
                "PRIVATE_WORLD_CANDIDATE_CONFIDENCE_INVALID"
            )
        object.__setattr__(self, "confidence", float(self.confidence))
        created = _aware(
            self.created_at,
            code="PRIVATE_WORLD_CANDIDATE_CREATED_AT_INVALID",
        )
        expires = _aware(
            self.expires_at,
            code="PRIVATE_WORLD_CANDIDATE_EXPIRES_AT_INVALID",
        )
        if expires <= created:
            raise PrivateWorldCandidateError(
                "PRIVATE_WORLD_CANDIDATE_EXPIRY_INVALID"
            )

    def with_status(self, status: CandidateStatus) -> PrivateWorldCandidate:
        if not isinstance(status, CandidateStatus):
            raise PrivateWorldCandidateError(
                "PRIVATE_WORLD_CANDIDATE_STATUS_INVALID"
            )
        return replace(self, status=status)

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "source_letter_id": self.source_letter_id,
            "source_reply_revision": self.source_reply_revision,
            "candidate_type": self.candidate_type.value,
            "summary": self.summary,
            "confidence": self.confidence,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True)
class CandidateDecision:
    decision_id: str
    candidate_id: str
    decision: CandidateDecisionKind
    actor: PrivateWorldActor
    reason: str
    decided_at: datetime
    command_event_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decision_id",
            _identifier(
                self.decision_id,
                code="PRIVATE_WORLD_CANDIDATE_DECISION_ID_INVALID",
            ),
        )
        object.__setattr__(
            self,
            "candidate_id",
            _identifier(
                self.candidate_id,
                code="PRIVATE_WORLD_CANDIDATE_ID_INVALID",
            ),
        )
        if not isinstance(self.decision, CandidateDecisionKind):
            raise PrivateWorldCandidateError(
                "PRIVATE_WORLD_CANDIDATE_DECISION_INVALID"
            )
        if self.actor is not PrivateWorldActor.LOCAL_USER:
            raise PrivateWorldCandidateError(
                "PRIVATE_WORLD_CANDIDATE_DECISION_ACTOR_INVALID"
            )
        object.__setattr__(
            self,
            "reason",
            _text(
                self.reason,
                code="PRIVATE_WORLD_CANDIDATE_DECISION_REASON_INVALID",
                maximum=_MAX_REASON_LENGTH,
            ),
        )
        _aware(
            self.decided_at,
            code="PRIVATE_WORLD_CANDIDATE_DECIDED_AT_INVALID",
        )
        if self.decision is CandidateDecisionKind.APPROVE:
            object.__setattr__(
                self,
                "command_event_id",
                _identifier(
                    self.command_event_id,
                    code=(
                        "PRIVATE_WORLD_CANDIDATE_COMMAND_EVENT_INVALID"
                    ),
                ),
            )
        elif self.command_event_id is not None:
            raise PrivateWorldCandidateError(
                "PRIVATE_WORLD_CANDIDATE_COMMAND_EVENT_INVALID"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "candidate_id": self.candidate_id,
            "decision": self.decision.value,
            "actor": self.actor.value,
            "reason": self.reason,
            "decided_at": self.decided_at.isoformat(),
            "command_event_id": self.command_event_id,
        }


class SQLitePrivateWorldCandidateStore:
    """Store bounded candidates beside the PrivateWorld ledger."""

    def __init__(self, database_path: Path) -> None:
        path = Path(database_path)
        if (
            str(path) in {"", "."}
            or not path.is_file()
            or path.is_dir()
        ):
            raise PrivateWorldCandidateError(
                "PRIVATE_WORLD_CANDIDATE_LEDGER_REQUIRED"
            )
        self._database_path = path
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self._database_path,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _initialize(self) -> None:
        try:
            with self._write_connection() as connection:
                metadata = connection.execute(
                    """SELECT value FROM private_world_metadata
                       WHERE key = 'schema_version'"""
                ).fetchone()
                if metadata is None:
                    raise PrivateWorldCandidateError(
                        "PRIVATE_WORLD_CANDIDATE_LEDGER_REQUIRED"
                    )
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS private_world_candidates (
                        candidate_id TEXT PRIMARY KEY,
                        source_letter_id TEXT NOT NULL,
                        source_reply_revision INTEGER NOT NULL,
                        candidate_type TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        confidence REAL NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        UNIQUE(
                            source_letter_id,
                            source_reply_revision,
                            candidate_type
                        )
                    );
                    CREATE INDEX IF NOT EXISTS
                        private_world_candidates_status_created
                    ON private_world_candidates(status, created_at);
                    CREATE TABLE IF NOT EXISTS
                        private_world_candidate_decisions (
                            decision_id TEXT PRIMARY KEY,
                            candidate_id TEXT NOT NULL UNIQUE,
                            decision TEXT NOT NULL,
                            actor TEXT NOT NULL,
                            reason TEXT NOT NULL,
                            decided_at TEXT NOT NULL,
                            command_event_id TEXT,
                            FOREIGN KEY(candidate_id)
                                REFERENCES private_world_candidates(candidate_id)
                        );
                    """
                )
                connection.execute(
                    """INSERT INTO private_world_metadata (key, value)
                       VALUES (?, ?)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                    (
                        _CANDIDATE_METADATA_KEY,
                        str(PRIVATE_WORLD_CANDIDATE_SCHEMA_VERSION),
                    ),
                )
        except PrivateWorldCandidateError:
            raise
        except sqlite3.Error as exc:
            raise PrivateWorldCandidateError(
                "PRIVATE_WORLD_CANDIDATE_STORAGE_UNAVAILABLE"
            ) from exc

    @staticmethod
    def _candidate_from_row(row: sqlite3.Row) -> PrivateWorldCandidate:
        return PrivateWorldCandidate(
            candidate_id=row["candidate_id"],
            source_letter_id=row["source_letter_id"],
            source_reply_revision=row["source_reply_revision"],
            candidate_type=CandidateType(row["candidate_type"]),
            summary=row["summary"],
            confidence=row["confidence"],
            status=CandidateStatus(row["status"]),
            created_at=_parse_time(
                row["created_at"],
                code="PRIVATE_WORLD_CANDIDATE_CREATED_AT_INVALID",
            ),
            expires_at=_parse_time(
                row["expires_at"],
                code="PRIVATE_WORLD_CANDIDATE_EXPIRES_AT_INVALID",
            ),
        )

    @staticmethod
    def _decision_from_row(row: sqlite3.Row) -> CandidateDecision:
        return CandidateDecision(
            decision_id=row["decision_id"],
            candidate_id=row["candidate_id"],
            decision=CandidateDecisionKind(row["decision"]),
            actor=PrivateWorldActor(row["actor"]),
            reason=row["reason"],
            decided_at=_parse_time(
                row["decided_at"],
                code="PRIVATE_WORLD_CANDIDATE_DECIDED_AT_INVALID",
            ),
            command_event_id=row["command_event_id"],
        )

    @staticmethod
    def _same_candidate(
        left: PrivateWorldCandidate,
        right: PrivateWorldCandidate,
    ) -> bool:
        return left == right

    @staticmethod
    def _same_decision(
        left: CandidateDecision,
        right: CandidateDecision,
    ) -> bool:
        return left == right

    @staticmethod
    def _expire_in_transaction(
        connection: sqlite3.Connection,
        now: datetime,
    ) -> int:
        _aware(now, code="PRIVATE_WORLD_CANDIDATE_NOW_INVALID")
        cursor = connection.execute(
            """UPDATE private_world_candidates
               SET status = ?
               WHERE status = ? AND expires_at <= ?""",
            (
                CandidateStatus.EXPIRED.value,
                CandidateStatus.PENDING.value,
                now.isoformat(),
            ),
        )
        return int(cursor.rowcount)

    def add(
        self,
        candidate: PrivateWorldCandidate,
    ) -> CandidateWriteStatus:
        if not isinstance(candidate, PrivateWorldCandidate):
            raise TypeError("a typed PrivateWorld candidate is required")
        if candidate.status is not CandidateStatus.PENDING:
            raise PrivateWorldCandidateError(
                "PRIVATE_WORLD_CANDIDATE_NOT_PENDING"
            )
        try:
            with self._write_connection() as connection:
                row = connection.execute(
                    """SELECT * FROM private_world_candidates
                       WHERE candidate_id = ? OR (
                           source_letter_id = ?
                           AND source_reply_revision = ?
                           AND candidate_type = ?
                       ) LIMIT 1""",
                    (
                        candidate.candidate_id,
                        candidate.source_letter_id,
                        candidate.source_reply_revision,
                        candidate.candidate_type.value,
                    ),
                ).fetchone()
                if row is not None:
                    existing = self._candidate_from_row(row)
                    if self._same_candidate(existing, candidate):
                        return CandidateWriteStatus.DUPLICATE
                    raise PrivateWorldCandidateError(
                        "PRIVATE_WORLD_CANDIDATE_IDENTITY_CONFLICT"
                    )
                connection.execute(
                    """INSERT INTO private_world_candidates (
                           candidate_id,
                           source_letter_id,
                           source_reply_revision,
                           candidate_type,
                           summary,
                           confidence,
                           status,
                           created_at,
                           expires_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        candidate.candidate_id,
                        candidate.source_letter_id,
                        candidate.source_reply_revision,
                        candidate.candidate_type.value,
                        candidate.summary,
                        candidate.confidence,
                        candidate.status.value,
                        candidate.created_at.isoformat(),
                        candidate.expires_at.isoformat(),
                    ),
                )
            return CandidateWriteStatus.CREATED
        except PrivateWorldCandidateError:
            raise
        except sqlite3.Error as exc:
            raise PrivateWorldCandidateError(
                "PRIVATE_WORLD_CANDIDATE_STORAGE_UNAVAILABLE"
            ) from exc

    def expire(self, now: datetime) -> int:
        try:
            with self._write_connection() as connection:
                return self._expire_in_transaction(connection, now)
        except PrivateWorldCandidateError:
            raise
        except sqlite3.Error as exc:
            raise PrivateWorldCandidateError(
                "PRIVATE_WORLD_CANDIDATE_STORAGE_UNAVAILABLE"
            ) from exc

    def get(
        self,
        candidate_id: str,
        *,
        now: datetime | None = None,
    ) -> PrivateWorldCandidate | None:
        identifier = _identifier(
            candidate_id,
            code="PRIVATE_WORLD_CANDIDATE_ID_INVALID",
        )
        if now is not None:
            self.expire(now)
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """SELECT * FROM private_world_candidates
                       WHERE candidate_id = ?""",
                    (identifier,),
                ).fetchone()
            return None if row is None else self._candidate_from_row(row)
        except PrivateWorldCandidateError:
            raise
        except (sqlite3.Error, ValueError) as exc:
            raise PrivateWorldCandidateError(
                "PRIVATE_WORLD_CANDIDATE_STORAGE_UNAVAILABLE"
            ) from exc

    def list_candidates(
        self,
        *,
        status: CandidateStatus | None = None,
        now: datetime | None = None,
    ) -> tuple[PrivateWorldCandidate, ...]:
        if status is not None and not isinstance(status, CandidateStatus):
            raise PrivateWorldCandidateError(
                "PRIVATE_WORLD_CANDIDATE_STATUS_INVALID"
            )
        if now is not None:
            self.expire(now)
        try:
            with self._connection() as connection:
                if status is None:
                    rows = connection.execute(
                        """SELECT * FROM private_world_candidates
                           ORDER BY created_at DESC, candidate_id"""
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """SELECT * FROM private_world_candidates
                           WHERE status = ?
                           ORDER BY created_at DESC, candidate_id""",
                        (status.value,),
                    ).fetchall()
            return tuple(self._candidate_from_row(row) for row in rows)
        except PrivateWorldCandidateError:
            raise
        except (sqlite3.Error, ValueError) as exc:
            raise PrivateWorldCandidateError(
                "PRIVATE_WORLD_CANDIDATE_STORAGE_UNAVAILABLE"
            ) from exc

    def decision(
        self,
        candidate_id: str,
    ) -> CandidateDecision | None:
        identifier = _identifier(
            candidate_id,
            code="PRIVATE_WORLD_CANDIDATE_ID_INVALID",
        )
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """SELECT * FROM private_world_candidate_decisions
                       WHERE candidate_id = ?""",
                    (identifier,),
                ).fetchone()
            return None if row is None else self._decision_from_row(row)
        except PrivateWorldCandidateError:
            raise
        except (sqlite3.Error, ValueError) as exc:
            raise PrivateWorldCandidateError(
                "PRIVATE_WORLD_CANDIDATE_STORAGE_UNAVAILABLE"
            ) from exc

    def record_decision(
        self,
        decision: CandidateDecision,
    ) -> CandidateDecisionWriteStatus:
        if not isinstance(decision, CandidateDecision):
            raise TypeError("a typed candidate decision is required")
        try:
            with self._write_connection() as connection:
                self._expire_in_transaction(connection, decision.decided_at)
                candidate_row = connection.execute(
                    """SELECT * FROM private_world_candidates
                       WHERE candidate_id = ?""",
                    (decision.candidate_id,),
                ).fetchone()
                if candidate_row is None:
                    raise PrivateWorldCandidateError(
                        "PRIVATE_WORLD_CANDIDATE_NOT_FOUND"
                    )
                existing_row = connection.execute(
                    """SELECT * FROM private_world_candidate_decisions
                       WHERE candidate_id = ? OR decision_id = ?
                       LIMIT 1""",
                    (decision.candidate_id, decision.decision_id),
                ).fetchone()
                if existing_row is not None:
                    existing = self._decision_from_row(existing_row)
                    if self._same_decision(existing, decision):
                        return CandidateDecisionWriteStatus.DUPLICATE
                    raise PrivateWorldCandidateError(
                        "PRIVATE_WORLD_CANDIDATE_DECISION_CONFLICT"
                    )
                candidate = self._candidate_from_row(candidate_row)
                if candidate.status is not CandidateStatus.PENDING:
                    raise PrivateWorldCandidateError(
                        "PRIVATE_WORLD_CANDIDATE_NOT_PENDING"
                    )
                next_status = (
                    CandidateStatus.APPROVED
                    if decision.decision is CandidateDecisionKind.APPROVE
                    else CandidateStatus.REJECTED
                )
                connection.execute(
                    """INSERT INTO private_world_candidate_decisions (
                           decision_id,
                           candidate_id,
                           decision,
                           actor,
                           reason,
                           decided_at,
                           command_event_id
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        decision.decision_id,
                        decision.candidate_id,
                        decision.decision.value,
                        decision.actor.value,
                        decision.reason,
                        decision.decided_at.isoformat(),
                        decision.command_event_id,
                    ),
                )
                connection.execute(
                    """UPDATE private_world_candidates
                       SET status = ? WHERE candidate_id = ?""",
                    (next_status.value, decision.candidate_id),
                )
            return CandidateDecisionWriteStatus.RECORDED
        except PrivateWorldCandidateError:
            raise
        except sqlite3.Error as exc:
            raise PrivateWorldCandidateError(
                "PRIVATE_WORLD_CANDIDATE_STORAGE_UNAVAILABLE"
            ) from exc

    def health(self) -> dict[str, int | str]:
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """SELECT status, COUNT(*) AS count
                       FROM private_world_candidates
                       GROUP BY status"""
                ).fetchall()
                decisions = connection.execute(
                    """SELECT COUNT(*) AS count
                       FROM private_world_candidate_decisions"""
                ).fetchone()["count"]
            counts = {status.value: 0 for status in CandidateStatus}
            counts.update({str(row["status"]): int(row["count"]) for row in rows})
            return {
                "status": "READY",
                "schema_version": PRIVATE_WORLD_CANDIDATE_SCHEMA_VERSION,
                "pending": counts[CandidateStatus.PENDING.value],
                "approved": counts[CandidateStatus.APPROVED.value],
                "rejected": counts[CandidateStatus.REJECTED.value],
                "expired": counts[CandidateStatus.EXPIRED.value],
                "decisions": int(decisions),
            }
        except sqlite3.Error as exc:
            raise PrivateWorldCandidateError(
                "PRIVATE_WORLD_CANDIDATE_STORAGE_UNAVAILABLE"
            ) from exc


__all__ = [
    "CandidateDecision",
    "CandidateDecisionKind",
    "CandidateDecisionWriteStatus",
    "CandidateStatus",
    "CandidateType",
    "CandidateWriteStatus",
    "PRIVATE_WORLD_CANDIDATE_SCHEMA_VERSION",
    "PrivateWorldCandidate",
    "PrivateWorldCandidateError",
    "SQLitePrivateWorldCandidateStore",
    "candidate_identity",
]
