"""Partitioned prompt context for Mem0 conversation facts and read-only Archive.

The existing SQLite memory port remains the Archive owner.  When a new
ConversationMemoryPort is enabled, its records replace the old SQLite
conversation-memory section, while legacy letters continue to come only from
Archive.  Both domains are rendered by the existing untrusted-data formatter.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping, Sequence

from conversation_memory_port import (
    ConversationMemoryPort,
    ConversationMemoryRecord,
)
from memory_port import (
    CONVERSATION_MEMORY,
    LEGACY_LETTERS,
    MemoryPort,
    MemoryRecord,
)
from memory_prompt import MemoryPrompt, MemoryPromptBuilder


class _ConversationMemoryView:
    """Adapt the narrow conversation-memory port to the legacy prompt renderer."""

    enabled = True

    def __init__(
        self,
        memory: ConversationMemoryPort,
        *,
        user_id: str,
    ) -> None:
        self.memory = memory
        self.user_id = user_id

    def status(self) -> Mapping[str, object]:
        return self.memory.status().to_dict()

    def search(
        self,
        query: str,
        *,
        domains: Sequence[str] | None = None,
        limit: int = 8,
    ) -> list[MemoryRecord]:
        if domains is not None and CONVERSATION_MEMORY not in domains:
            return []
        records = self.memory.search_context(
            query,
            user_id=self.user_id,
            limit=limit,
        )
        return [self._convert(record) for record in records]

    @staticmethod
    def _convert(record: ConversationMemoryRecord) -> MemoryRecord:
        created_at = _epoch(record.created_at or record.occurred_at)
        occurred_at = (
            record.occurred_at.isoformat()
            if record.occurred_at is not None
            else None
        )
        provenance = {
            "domain": CONVERSATION_MEMORY,
            "source": "mem0",
            "source_record_id": record.source_id,
            "occurred_at": occurred_at or "",
            "current_conversation": True,
        }
        return MemoryRecord(
            memory_id=record.memory_id,
            domain=CONVERSATION_MEMORY,
            text=record.text,
            source="mem0",
            created_at=created_at,
            occurred_at=occurred_at,
            score=float(record.score or 0.0),
            provenance=provenance,
            metadata={},
        )


class _LegacyArchiveView:
    """Force the old memory port to expose only read-only legacy letters."""

    def __init__(self, memory: MemoryPort) -> None:
        self.memory = memory
        self.enabled = bool(getattr(memory, "enabled", False))

    def status(self) -> Mapping[str, object]:
        return self.memory.status()

    def search(
        self,
        query: str,
        *,
        domains: Sequence[str] | None = None,
        limit: int = 8,
    ) -> list[MemoryRecord]:
        if domains is not None and LEGACY_LETTERS not in domains:
            return []
        return self.memory.search(
            query,
            domains=(LEGACY_LETTERS,),
            limit=limit,
        )


class CompanionMemoryPromptBuilder:
    """Build bounded, visibly untrusted current-memory and Archive sections."""

    def __init__(
        self,
        archive_memory: MemoryPort,
        conversation_memory: ConversationMemoryPort,
        *,
        user_id: str = "local-user",
        max_results: int = 8,
        current_share: float = 0.6,
    ) -> None:
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("conversation memory user_id is required")
        if not 0.2 <= float(current_share) <= 0.8:
            raise ValueError("current memory share must be bounded")
        self.archive_memory = archive_memory
        self.conversation_memory = conversation_memory
        self.user_id = user_id.strip()
        self.max_results = max(1, min(32, int(max_results)))
        self.current_share = float(current_share)
        self._fallback = MemoryPromptBuilder(
            archive_memory,
            max_results=self.max_results,
        )

    def build(self, query: str, *, max_chars: int | None = None) -> MemoryPrompt:
        budget = max(0, int(max_chars if max_chars is not None else 2400))
        if budget <= 0 or not isinstance(query, str) or not query.strip():
            return MemoryPrompt(status="disabled")

        if not bool(getattr(self.conversation_memory, "enabled", False)):
            return self._fallback.build(query, max_chars=budget)

        current_budget = max(0, int(budget * self.current_share))
        archive_budget = max(0, budget - current_budget)
        current = MemoryPromptBuilder(
            _ConversationMemoryView(
                self.conversation_memory,
                user_id=self.user_id,
            ),
            max_results=self.max_results,
            legacy_budget=0,
            conversation_budget=current_budget,
        ).build(query, max_chars=current_budget)
        archive = MemoryPromptBuilder(
            _LegacyArchiveView(self.archive_memory),
            max_results=self.max_results,
            legacy_budget=archive_budget,
            conversation_budget=0,
        ).build(query, max_chars=archive_budget)

        parts = tuple(prompt.text for prompt in (current, archive) if prompt.text)
        references = (*current.references, *archive.references)
        domains = tuple(dict.fromkeys((*current.domains, *archive.domains)))
        return MemoryPrompt(
            text="\n".join(parts),
            references=references,
            status=_combined_status(current, archive, has_text=bool(parts)),
            truncated=current.truncated or archive.truncated,
            domains=domains,
        )


def _epoch(value: datetime | None) -> int:
    if value is None:
        return 0
    try:
        return max(0, int(value.timestamp()))
    except (OSError, OverflowError, ValueError):
        return 0


def _combined_status(
    current: MemoryPrompt,
    archive: MemoryPrompt,
    *,
    has_text: bool,
) -> str:
    statuses = {current.status, archive.status}
    if has_text:
        return "degraded" if statuses & {"degraded", "unavailable"} else "available"
    if "unavailable" in statuses:
        return "unavailable"
    if "degraded" in statuses:
        return "degraded"
    if statuses == {"disabled"}:
        return "disabled"
    return "available"


__all__ = ["CompanionMemoryPromptBuilder"]
