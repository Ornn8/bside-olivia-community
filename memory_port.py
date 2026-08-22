"""Framework-neutral contracts for the optional local memory sidecar.

The B03 reply path depends on this small port only.  SQLite is an adapter
behind it; a future local Mem0 adapter may implement the same contract without
being imported, installed, or contacted by the default application.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, Sequence


LEGACY_LETTERS = "legacy_letters"
CONVERSATION_MEMORY = "conversation_memory"
PERSONA_EVIDENCE = "persona_evidence"
MEMORY_DOMAINS = frozenset({LEGACY_LETTERS, CONVERSATION_MEMORY, PERSONA_EVIDENCE})


class MemoryUnavailable(RuntimeError):
    """An optional memory backend cannot serve a request."""

    code = "MEMORY_UNAVAILABLE"


@dataclass(frozen=True)
class LegacyLetter:
    """An imported letter accepted by the read-only import boundary."""

    content: str
    source_record_id: str
    source: str = ""
    occurred_at: str | int | float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryRecord:
    """A retrieved item with citation-safe provenance."""

    memory_id: str
    domain: str
    text: str
    source: str
    created_at: int
    occurred_at: str | int | float | None = None
    expires_at: int | None = None
    content_hash: str = ""
    score: float = 0.0
    provenance: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_legacy(self) -> bool:
        return self.domain == LEGACY_LETTERS

    @property
    def is_current_memory(self) -> bool:
        return self.domain == CONVERSATION_MEMORY


@dataclass(frozen=True)
class LegacyImportResult:
    """Result of inserting an already parsed legacy batch."""

    seen: int = 0
    inserted: int = 0
    duplicates: int = 0
    rejected: int = 0
    rolled_back: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "seen": self.seen,
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "rolled_back": self.rolled_back,
        }


class MemoryPort(Protocol):
    """The only memory surface visible to B03."""

    enabled: bool

    def status(self) -> Mapping[str, Any]: ...

    def search(
        self,
        query: str,
        *,
        domains: Sequence[str] | None = None,
        limit: int = 8,
    ) -> list[MemoryRecord]: ...

    def remember_conversation(
        self,
        summary: str,
        *,
        facts: Iterable[str] = (),
        ttl_seconds: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> str | None: ...

    def clear_conversation(self) -> int: ...

    def import_legacy_records(
        self,
        records: Iterable[LegacyLetter],
        *,
        atomic: bool = True,
    ) -> LegacyImportResult: ...

    def legacy_content_hashes(self) -> set[str]: ...

    def unload_legacy(self) -> int: ...

    def export_records(
        self,
        *,
        domains: Sequence[str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]: ...

    def persona_evidence(self) -> list[Mapping[str, Any]]: ...

    def uninstall(
        self,
        *,
        delete_conversation: bool = False,
        delete_legacy: bool = False,
    ) -> Mapping[str, Any]: ...


class NullMemoryPort:
    """Disabled memory: no storage is opened and no data is retained."""

    enabled = False

    def status(self) -> Mapping[str, Any]:
        return {
            "status": "disabled",
            "enabled": False,
            "provider": "none",
            "storage": "none",
            "fts5": False,
            "vector": {"status": "not_configured", "provider": "none"},
            "network_called": False,
        }

    def search(
        self,
        query: str,
        *,
        domains: Sequence[str] | None = None,
        limit: int = 8,
    ) -> list[MemoryRecord]:
        return []

    def remember_conversation(
        self,
        summary: str,
        *,
        facts: Iterable[str] = (),
        ttl_seconds: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> str | None:
        return None

    def clear_conversation(self) -> int:
        return 0

    def import_legacy_records(
        self,
        records: Iterable[LegacyLetter],
        *,
        atomic: bool = True,
    ) -> LegacyImportResult:
        materialized = list(records)
        return LegacyImportResult(
            seen=len(materialized),
            rejected=len(materialized),
            rolled_back=bool(materialized),
        )

    def legacy_content_hashes(self) -> set[str]:
        return set()

    def unload_legacy(self) -> int:
        return 0

    def export_records(
        self,
        *,
        domains: Sequence[str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        if domains is None:
            raise ValueError("export domains must be explicit")
        selected = tuple(domains)
        if any(domain not in MEMORY_DOMAINS for domain in selected):
            raise ValueError("unknown memory domain")
        return {domain: [] for domain in selected}

    def persona_evidence(self) -> list[Mapping[str, Any]]:
        return []

    def uninstall(
        self,
        *,
        delete_conversation: bool = False,
        delete_legacy: bool = False,
    ) -> Mapping[str, Any]:
        return {
            "status": "disabled",
            "conversation_deleted": False,
            "legacy_deleted": False,
            "legacy_delete_requested": bool(delete_legacy),
            "persona_evidence_deleted": False,
        }


__all__ = [
    "CONVERSATION_MEMORY",
    "LEGACY_LETTERS",
    "MEMORY_DOMAINS",
    "PERSONA_EVIDENCE",
    "LegacyImportResult",
    "LegacyLetter",
    "MemoryPort",
    "MemoryRecord",
    "MemoryUnavailable",
    "NullMemoryPort",
]
