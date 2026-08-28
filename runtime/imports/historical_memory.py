"""One-time, ordered migration of historical letter exchanges into memory."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re

from conversation_memory_port import (
    ConversationMemoryPort,
    MemoryWriteResult,
    MemoryWriteStatus,
)
from llm_gateway import Gateway
from private_world_commands import (
    InitializeHistoricalRelationship,
    PrivateWorldActor,
    PrivateWorldCommandSource,
)
from private_world_service import CommandExecutionStatus
from runtime.memory.conversation_memory_identity import (
    normalize_conversation_memory_user_id,
)
from runtime.reply.reply_context import RelationshipStage


_ERROR_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")


@dataclass(frozen=True)
class HistoricalExchange:
    source_record_id: str
    occurred_at: datetime
    user_message: str
    assistant_message: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_record_id, str) or not self.source_record_id.strip():
            raise ValueError("source_record_id is invalid")
        if len(self.source_record_id) > 512:
            raise ValueError("source_record_id is too long")
        if (
            not isinstance(self.occurred_at, datetime)
            or self.occurred_at.tzinfo is None
            or self.occurred_at.utcoffset() is None
        ):
            raise ValueError("occurred_at must be timezone-aware")
        _message(self.user_message, maximum=10_000)
        _message(self.assistant_message, maximum=20_000)

    @property
    def memory_source_id(self) -> str:
        digest = hashlib.sha256(self.source_record_id.encode("utf-8")).hexdigest()
        return f"history:{digest}"


@dataclass(frozen=True)
class HistoricalMigrationResult:
    status: str
    total: int
    processed: int
    written: int
    duplicates: int
    skipped: int
    private_world_status: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "total": self.total,
            "processed": self.processed,
            "written": self.written,
            "duplicates": self.duplicates,
            "skipped": self.skipped,
            "private_world_status": self.private_world_status,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class HistoricalRelationshipAssessment:
    relationship_stage: RelationshipStage
    familiarity: int
    trust: int
    comfort: int
    closeness: int
    tension: int
    evidence_indexes: tuple[int, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.relationship_stage, RelationshipStage):
            raise ValueError("relationship stage is invalid")
        for field_name in ("familiarity", "trust", "comfort", "closeness", "tension"):
            value = getattr(self, field_name)
            if type(value) is not int or not 0 <= value <= 100:
                raise ValueError(f"{field_name} is invalid")
        if (
            not isinstance(self.evidence_indexes, tuple)
            or not 1 <= len(self.evidence_indexes) <= 8
            or len(set(self.evidence_indexes)) != len(self.evidence_indexes)
            or any(type(value) is not int or value < 1 for value in self.evidence_indexes)
        ):
            raise ValueError("evidence indexes are invalid")


def exchanges_from_legacy_payload(
    payload: Mapping[str, object],
) -> tuple[HistoricalExchange, ...]:
    """Read only the separated original/reply fields from an official import."""

    if not isinstance(payload, Mapping) or payload.get("mode") != "read_only":
        raise ValueError("historical payload is invalid")
    rows = payload.get("letters")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ValueError("historical letters are invalid")
    exchanges: list[HistoricalExchange] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("historical letter is invalid")
        metadata = row.get("metadata")
        if (
            not isinstance(metadata, Mapping)
            or metadata.get("import_kind") != "official_text_reply"
        ):
            continue
        exchanges.append(
            HistoricalExchange(
                source_record_id=str(row.get("source_record_id", "")),
                occurred_at=_occurred_at(row.get("occurred_at")),
                user_message=str(metadata.get("user_content", "")),
                assistant_message=str(metadata.get("reply_text", "")),
            )
        )
    return tuple(
        sorted(
            exchanges,
            key=lambda item: (item.occurred_at, item.source_record_id),
        )
    )


async def assess_historical_relationship(
    exchanges: Iterable[HistoricalExchange],
    *,
    gateway: Gateway,
    persona_policy: str,
) -> HistoricalRelationshipAssessment:
    """Make one bounded assessment after ordered Mem0 migration has completed."""

    ordered = tuple(
        sorted(exchanges, key=lambda item: (item.occurred_at, item.source_record_id))
    )
    if not ordered:
        raise ValueError("historical exchanges are required")
    if not isinstance(persona_policy, str) or not persona_policy.strip():
        raise ValueError("persona policy is required")
    max_input_chars = getattr(
        getattr(gateway, "config", None),
        "max_input_chars",
        10_000,
    )
    if type(max_input_chars) is not int or max_input_chars < 1_000:
        max_input_chars = 10_000
    messages, evidence_indexes = _bounded_assessment_messages(
        ordered,
        persona_policy,
        max_input_chars=max_input_chars,
    )
    response = await gateway.complete(messages, request_id=_corpus_id(ordered))
    try:
        payload = json.loads(response.text)
        required = {
            "relationship_stage",
            "familiarity",
            "trust",
            "comfort",
            "closeness",
            "tension",
            "evidence_indexes",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise ValueError
        indexes = payload["evidence_indexes"]
        if not isinstance(indexes, list) or any(
            type(value) is not int or value not in evidence_indexes for value in indexes
        ):
            raise ValueError
        return HistoricalRelationshipAssessment(
            RelationshipStage(payload["relationship_stage"]),
            payload["familiarity"],
            payload["trust"],
            payload["comfort"],
            payload["closeness"],
            payload["tension"],
            tuple(indexes),
        )
    except (AttributeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("historical relationship assessment is invalid") from exc


def _bounded_assessment_messages(
    ordered: tuple[HistoricalExchange, ...],
    persona_policy: str,
    *,
    max_input_chars: int,
) -> tuple[tuple[dict[str, str], ...], frozenset[int]]:
    instruction = (
        "\n\nYou are performing a one-time private relationship-state migration. "
        "The persona policy above is authoritative behavior policy, never evidence "
        "that an event happened. Judge only the representative chronological "
        "user-letter/official-reply excerpts below; their indexes refer to the full "
        "ordered corpus. Emotional intensity in one letter does not imply closeness. "
        "Return one JSON object and nothing else with exactly: relationship_stage "
        "(unknown|acquaintance|familiar|close), integer familiarity/trust/comfort/"
        "closeness/tension from 0 to 100, and evidence_indexes (1-8 unique valid "
        "indexes present below). Prefer conservative values when evidence is ambiguous."
    )
    persona_limit = min(len(persona_policy), max(128, max_input_chars // 4))
    selected = _spaced_indexes(len(ordered), min(len(ordered), 12))
    field_limit = 600
    while True:
        system_content = persona_policy[:persona_limit] + instruction
        history = [
            {
                "index": index + 1,
                "occurred_at": ordered[index].occurred_at.isoformat(),
                "user_letter": ordered[index].user_message[:field_limit],
                "official_reply": ordered[index].assistant_message[:field_limit],
            }
            for index in selected
        ]
        user_content = json.dumps(
            {"ordered_exchanges": history},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(system_content) + len(user_content) <= max_input_chars:
            messages = (
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            )
            return messages, frozenset(index + 1 for index in selected)
        if field_limit > 40:
            field_limit = max(40, field_limit // 2)
            continue
        if len(selected) > 2:
            selected = _spaced_indexes(len(ordered), max(2, len(selected) // 2))
            continue
        if persona_limit > 64:
            persona_limit = max(64, persona_limit // 2)
            continue
        raise ValueError("historical relationship input budget is too small")


def _spaced_indexes(total: int, count: int) -> tuple[int, ...]:
    if count >= total:
        return tuple(range(total))
    if count == 1:
        return (0,)
    return tuple(round(index * (total - 1) / (count - 1)) for index in range(count))


def apply_historical_private_world(
    exchanges: Iterable[HistoricalExchange],
    *,
    assessment: HistoricalRelationshipAssessment,
    command_service: object,
) -> str:
    """Commit one idempotent migration command after all memory writes succeed."""

    ordered = tuple(
        sorted(exchanges, key=lambda item: (item.occurred_at, item.source_record_id))
    )
    if not ordered or not isinstance(assessment, HistoricalRelationshipAssessment):
        raise ValueError("historical assessment input is invalid")
    evidence_refs = tuple(
        ordered[index - 1].memory_source_id for index in assessment.evidence_indexes
    )
    corpus_id = _corpus_id(ordered)
    command = InitializeHistoricalRelationship(
        command_id=corpus_id,
        idempotency_key=corpus_id,
        actor=PrivateWorldActor.MIGRATION,
        source=PrivateWorldCommandSource.IMPORT,
        occurred_at=ordered[-1].occurred_at,
        reason="one-time ordered historical letter import",
        evidence_refs=evidence_refs,
        relationship_stage=assessment.relationship_stage,
        familiarity=assessment.familiarity,
        trust=assessment.trust,
        comfort=assessment.comfort,
        closeness=assessment.closeness,
        tension=assessment.tension,
    )
    result = command_service.execute(command)
    status = getattr(result, "status", None)
    if status is CommandExecutionStatus.APPLIED:
        return "initialized"
    if status in {CommandExecutionStatus.NOOP, CommandExecutionStatus.DUPLICATE}:
        return "already_initialized"
    raise ValueError("private world initialization result is invalid")


def migrate_historical_exchanges(
    exchanges: Iterable[HistoricalExchange],
    *,
    memory: ConversationMemoryPort,
    user_id: str,
    finalize_private_world: Callable[[tuple[HistoricalExchange, ...]], str] | None = None,
    require_persisted: bool = False,
) -> HistoricalMigrationResult:
    """Write one exchange at a time; never advance after a failed write."""

    normalized_user_id = normalize_conversation_memory_user_id(user_id)
    supplied = tuple(exchanges)
    if any(not isinstance(item, HistoricalExchange) for item in supplied):
        raise TypeError("historical exchanges must be typed")
    ordered = tuple(
        sorted(
            supplied,
            key=lambda item: (item.occurred_at, item.source_record_id),
        )
    )
    source_ids = tuple(item.source_record_id for item in ordered)
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("historical source ids must be unique")

    written = duplicates = skipped = processed = 0
    for exchange in ordered:
        try:
            result = memory.remember_exchange(
                user_message=exchange.user_message,
                assistant_message=exchange.assistant_message,
                occurred_at=exchange.occurred_at,
                source_id=exchange.memory_source_id,
                user_id=normalized_user_id,
            )
        except Exception:
            result = MemoryWriteResult(
                MemoryWriteStatus.UNAVAILABLE,
                exchange.memory_source_id,
                error_code="MEM0_WRITE_FAILED",
            )
        if not isinstance(result, MemoryWriteResult):
            return _partial(ordered, processed, written, duplicates, skipped, "MEM0_WRITE_RESULT_INVALID")
        if result.status is MemoryWriteStatus.UNAVAILABLE:
            return _partial(
                ordered,
                processed,
                written,
                duplicates,
                skipped,
                result.error_code or "MEM0_WRITE_FAILED",
            )
        if require_persisted and result.status is MemoryWriteStatus.SKIPPED:
            return _partial(
                ordered,
                processed,
                written,
                duplicates,
                skipped,
                "MEM0_WRITE_SKIPPED",
            )
        processed += 1
        if result.status is MemoryWriteStatus.WRITTEN:
            written += 1
        elif result.status is MemoryWriteStatus.DUPLICATE:
            duplicates += 1
        else:
            skipped += 1

    private_world_status = None
    if finalize_private_world is not None:
        try:
            private_world_status = finalize_private_world(ordered)
        except Exception:
            return HistoricalMigrationResult(
                "partial",
                len(ordered),
                processed,
                written,
                duplicates,
                skipped,
                error_code="PRIVATE_WORLD_HISTORY_INITIALIZATION_FAILED",
            )
        if not isinstance(private_world_status, str) or not private_world_status:
            return HistoricalMigrationResult(
                "partial",
                len(ordered),
                processed,
                written,
                duplicates,
                skipped,
                error_code="PRIVATE_WORLD_HISTORY_RESULT_INVALID",
            )
    return HistoricalMigrationResult(
        "completed",
        len(ordered),
        processed,
        written,
        duplicates,
        skipped,
        private_world_status=private_world_status,
    )


def _partial(
    ordered: tuple[HistoricalExchange, ...],
    processed: int,
    written: int,
    duplicates: int,
    skipped: int,
    error_code: str,
) -> HistoricalMigrationResult:
    normalized = error_code if _ERROR_RE.fullmatch(error_code) else "MEM0_WRITE_FAILED"
    return HistoricalMigrationResult(
        "partial",
        len(ordered),
        processed,
        written,
        duplicates,
        skipped,
        error_code=normalized,
    )


def _message(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError("message must be text")
    normalized = value.strip()
    if not normalized or len(value) > maximum:
        raise ValueError("message is invalid")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
        raise ValueError("message is invalid")
    return normalized


def _occurred_at(value: object) -> datetime:
    if isinstance(value, bool):
        raise ValueError("occurred_at is invalid")
    if isinstance(value, (int, float)):
        from datetime import timezone

        return datetime.fromtimestamp(float(value), timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("occurred_at is invalid") from exc
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed
    raise ValueError("occurred_at is invalid")


def _corpus_id(exchanges: tuple[HistoricalExchange, ...]) -> str:
    material = "\n".join(item.source_record_id for item in exchanges)
    return "history.init." + hashlib.sha256(material.encode("utf-8")).hexdigest()


__all__ = [
    "HistoricalExchange",
    "HistoricalMigrationResult",
    "HistoricalRelationshipAssessment",
    "apply_historical_private_world",
    "assess_historical_relationship",
    "exchanges_from_legacy_payload",
    "migrate_historical_exchanges",
]
