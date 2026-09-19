"""Partitioned prompt context for Mem0 conversation facts and read-only Archive.

The existing SQLite memory port remains the Archive owner.  When a new
ConversationMemoryPort is enabled, its records replace the old SQLite
conversation-memory section, while legacy letters continue to come only from
Archive.  Both domains are rendered by the existing untrusted-data formatter.
"""

from __future__ import annotations

from dataclasses import replace
from collections import Counter
from datetime import datetime
import hashlib
import math
from pathlib import Path
import sqlite3
from typing import Iterable, Mapping, Protocol, Sequence

from .conversation_memory_port import (
    ConversationMemoryPort,
    ConversationMemoryRecord,
)
from runtime.memory.conversation_memory_identity import (
    ConversationMemoryIdentityError,
    normalize_conversation_memory_user_id,
)
from .memory_port import (
    CONVERSATION_MEMORY,
    LEGACY_LETTERS,
    MemoryPort,
    MemoryRecord,
)
from .memory_prompt import MemoryPrompt, MemoryPromptBuilder, estimate_memory_tokens
from .recall import RecallResult, query_topics, source_id
from .recall_sources import archive_source_aliases


class _ConversationMemoryView:
    """Adapt the narrow conversation-memory port to the legacy prompt renderer."""

    enabled = True

    def __init__(
        self,
        memory: ConversationMemoryPort,
        *,
        user_id: str,
        exclude_source_ids=(),
    ) -> None:
        self.memory = memory
        self.user_id = user_id
        self.exclude_source_ids = tuple(exclude_source_ids)

    def status(self) -> Mapping[str, object]:
        return self.memory.status().to_dict()

    def search(
        self,
        query: str,
        *,
        domains: Sequence[str] | None = None,
        limit: int = 8,
    ) -> list[MemoryRecord]:
        return list(self.search_evidence_result(query, domains=domains, limit=limit).records)

    def search_evidence_result(self, query, *, domains=None, limit=8):
        if domains is not None and CONVERSATION_MEMORY not in domains:
            return RecallResult()
        structured = getattr(self.memory, "search_evidence_result", None)
        if callable(structured):
            result = structured(query, user_id=self.user_id, limit=limit,
                                exclude_source_ids=self.exclude_source_ids)
            return replace(result, records=tuple(self._convert(record) for record in result.records
                                                if record.source_id not in self.exclude_source_ids))
        search = getattr(self.memory, "search_evidence_context", None)
        options = {"exclude_source_ids": self.exclude_source_ids} if search is not None else {}
        search = search or self.memory.search_context
        records = search(
            query,
            user_id=self.user_id,
            limit=limit,
            **options,
        )
        return RecallResult(tuple(self._convert(record) for record in records
                                  if record.source_id not in self.exclude_source_ids),
                            source_status=(("semantic", "available"),))

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
            "current_conversation": not record.source_id.startswith("history:"),
        }
        metadata: dict[str, object] = {}
        for key in ("topic_indexes", "retrieval_route", "start", "end", "part_count", "expansion_seed"):
            if key in record.metadata:
                metadata[key] = record.metadata[key]
        if record.metadata.get("verbatim") is True and record.metadata.get("speaker") in {"user", "linli"}:
            provenance["speaker"] = record.metadata["speaker"]
            provenance["verbatim"] = True
            provenance["source"] = "original_text"
            metadata["verbatim"] = True
            metadata["speaker"] = record.metadata["speaker"]
            metadata["complete_original"] = record.metadata.get("complete_original") is True
        history_actor = record.metadata.get("history_actor")
        if (
            record.source_id.startswith("history:")
            and record.metadata.get("canonical") is True
            and history_actor in {"user", "linli"}
        ):
            metadata["canonical"] = True
            metadata["history_actor"] = history_actor
        if record.metadata.get("origin") == "proactive":
            provenance["origin"] = "proactive"
            provenance["speaker"] = "linli"
            metadata["origin"] = "proactive"
            metadata["verbatim"] = record.metadata.get("verbatim") is True
        return MemoryRecord(
            memory_id=record.memory_id,
            domain=CONVERSATION_MEMORY,
            text=record.text,
            source="mem0",
            created_at=created_at,
            occurred_at=occurred_at,
            score=float(record.score or 0.0),
            provenance=provenance,
            metadata=metadata,
        )


class _LegacyArchiveView:
    """Force the old memory port to expose only read-only legacy letters."""

    def __init__(self, memory: MemoryPort, *, exclude_source_ids=(), forgotten=(), guard_failed=False,
                 include_current=False) -> None:
        self.memory = memory
        self.enabled = bool(getattr(memory, "enabled", False))
        self.excluded = frozenset(exclude_source_ids) | frozenset(forgotten)
        self.guard_failed = guard_failed
        self.domains = (CONVERSATION_MEMORY, LEGACY_LETTERS) if include_current else (LEGACY_LETTERS,)

    def status(self) -> Mapping[str, object]:
        return self.memory.status()

    def search(
        self,
        query: str,
        *,
        domains: Sequence[str] | None = None,
        limit: int = 8,
    ) -> list[MemoryRecord]:
        selected = tuple(domain for domain in self.domains if domains is None or domain in domains)
        if not self.enabled or not selected:
            return []
        if self.guard_failed:
            raise sqlite3.OperationalError("ARCHIVE_FORGET_GUARD_UNAVAILABLE")
        records = self.memory.search(
            query,
            domains=selected,
            limit=limit,
        )
        return [record for record in records if not self._excluded(record)]

    def _excluded(self, record):
        if not self.excluded:
            return False
        source = source_id(record)
        if record.domain == CONVERSATION_MEMORY:
            return source in self.excluded
        mapped = _historical_source_id(source)
        if mapped in self.excluded:
            return True
        # A generic Archive ID lives in a separate namespace. Recognized pairs
        # additionally share the import/export and relationship evidence aliases.
        return bool(archive_source_aliases(source, record.metadata) & self.excluded)

    def search_evidence_result(self, query, *, domains=None, limit=8):
        try:
            records = self.search(query, domains=domains, limit=limit)
            status = str(self.memory.status().get("status", "available"))
            return RecallResult(tuple(records), source_status=(("archive", status),))
        except Exception:
            return RecallResult((), source_status=(("archive", "unavailable"),),
                                stop_reason="partial_source_failure")


class ConversationMemoryLifecycle(Protocol):
    def is_paused(self) -> bool: ...


class CompanionMemoryPromptBuilder:
    """Build bounded, visibly untrusted current-memory and Archive sections."""

    def __init__(
        self,
        archive_memory: MemoryPort,
        conversation_memory: ConversationMemoryPort,
        *,
        user_id: str = "local-user",
        max_results: int = 100,
        max_tokens: int = 300000,
        current_share: float = 0.6,
        memory_lifecycle: ConversationMemoryLifecycle | None = None,
    ) -> None:
        try:
            user_id = normalize_conversation_memory_user_id(user_id)
        except ConversationMemoryIdentityError as exc:
            raise ValueError("conversation memory user_id is required") from exc
        if not 0.2 <= float(current_share) <= 0.8:
            raise ValueError("current memory share must be bounded")
        self.archive_memory = archive_memory
        self.max_tokens = max(0, int(max_tokens))
        self.conversation_memory = conversation_memory
        self.user_id = user_id
        self.max_results = max(1, min(100, int(max_results)))
        self.current_share = float(current_share)
        self.memory_lifecycle = memory_lifecycle

    def build(
        self,
        query: str,
        *,
        max_chars: int | None = None,
        exclude_source_ids: Iterable[str] = (),
    ) -> MemoryPrompt:
        budget = max(0, int(max_chars if max_chars is not None else 30000))
        if budget <= 0 or not isinstance(query, str) or not query.strip():
            return MemoryPrompt(status="disabled")

        if self.memory_lifecycle is not None:
            try:
                if self.memory_lifecycle.is_paused():
                    return self._archive_only(
                        query,
                        budget,
                        exclude_source_ids=exclude_source_ids,
                    )
            except Exception:
                return self._archive_only(
                    query,
                    budget,
                    exclude_source_ids=exclude_source_ids,
                )

        if not bool(getattr(self.conversation_memory, "enabled", True)):
            return self.render(self.collect(query, exclude_source_ids=exclude_source_ids), max_chars=budget)

        if budget > 2400:
            return self.render(self.collect(query, exclude_source_ids=exclude_source_ids), max_chars=budget)
        current_budget = max(0, int(budget * self.current_share))
        archive_budget = max(0, budget - current_budget)
        archive = MemoryPromptBuilder(
            self._archive_view(exclude_source_ids),
            max_tokens=int(self.max_tokens * (1 - self.current_share)),
            max_results=self.max_results,
            legacy_budget=archive_budget,
            conversation_budget=0,
            conversation_memory=None,
        ).build(query, max_chars=archive_budget)
        # Reserve space for actual Archive references, not an empty half of the
        # prompt. This keeps recall bounded without dropping facts needlessly.
        current_budget = max(0, budget - len(archive.text) - (1 if archive.text else 0))
        current = MemoryPromptBuilder(
            _ConversationMemoryView(
                self.conversation_memory,
                user_id=self.user_id,
                exclude_source_ids=exclude_source_ids,
            ),
            max_tokens=max(0, self.max_tokens - estimate_memory_tokens(archive.text) - 1),
            max_results=min(8, self.max_results),
            legacy_budget=0,
            conversation_budget=current_budget,
            conversation_memory=None,
        ).build(
            query,
            max_chars=current_budget,
            exclude_source_ids=exclude_source_ids,
        )
        parts = tuple(prompt.text for prompt in (current, archive) if prompt.text)
        references = (*current.references, *archive.references)
        domains = tuple(dict.fromkeys((*current.domains, *archive.domains)))
        return MemoryPrompt(
            text="\n".join(parts),
            references=references,
            status=_combined_status(current, archive, has_text=bool(parts)),
            truncated=current.truncated or archive.truncated,
            domains=domains,
            recall_result=_merge_recall(current.recall_result, archive.recall_result, query=query),
        )

    def _archive_view(self, excluded=(), *, include_current=False):
        """Read deletion markers locally even while the optional provider is paused."""
        if not bool(getattr(self.archive_memory, "enabled", False)):
            return _LegacyArchiveView(self.archive_memory, exclude_source_ids=excluded,
                                      include_current=include_current)
        forgotten, failed = (), False
        originals = self._original_index()
        if originals is not None:
            try:
                forgotten = originals.forgotten_sources(self.user_id)
            except (OSError, sqlite3.Error):
                failed = True
        return _LegacyArchiveView(self.archive_memory, exclude_source_ids=excluded,
                                  forgotten=forgotten, guard_failed=failed, include_current=include_current)

    def _original_index(self):
        originals = getattr(self.conversation_memory, "_originals", None)
        if originals is not None:
            return originals
        data_root = getattr(getattr(self.conversation_memory, "config", None), "data_root", None)
        if isinstance(data_root, Path):
            from .source_retrieval import SourceRetrieval
            return SourceRetrieval(data_root / "original-text-index.sqlite3")
        return None

    def _collect_archive(self, query, excluded=(), *, include_current=False):
        view = self._archive_view(excluded, include_current=include_current)
        topics = query_topics(query)
        ranked, records, indexes, statuses = [], {}, {}, []
        # The old Archive tokenizer cuts Chinese into fixed-width pieces. Until
        # the original-text index has caught up, inspect the same read-only rows
        # once and match the existing bigrams locally, preserving both speakers.
        scanned, scan_status = [], None
        reader = getattr(self.archive_memory, "list_legacy", None)
        if callable(reader) and view.enabled and not view.guard_failed:
            try:
                from .source_retrieval import terms
                for row in reader():
                    if not isinstance(row, Mapping) or not isinstance(row.get("content"), str):
                        continue
                    record = MemoryRecord(memory_id=str(row.get("memory_id", row.get("source_record_id", ""))),
                        domain=LEGACY_LETTERS, text=row["content"], source=str(row.get("source", "archive")),
                        created_at=0, occurred_at=row.get("occurred_at"),
                        provenance={"domain": LEGACY_LETTERS, "source_record_id": str(row.get("source_record_id", "")),
                                    "occurred_at": row.get("occurred_at"), "read_only": True},
                        metadata=row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {})
                    if not view._excluded(record):
                        texts = [record.text, *(str(record.metadata.get(key, "")) for key in ("user_content", "reply_text"))]
                        scanned.append((record, frozenset(terms("\n".join(texts)))))
                scan_status = "available"
            except Exception:
                scan_status = "unavailable"
        frequency = Counter(term for _, tokens in scanned for term in tokens)
        for index, topic in enumerate(topics):
            result = view.search_evidence_result(topic, limit=self.max_results)
            statuses.extend(state for _, state in result.source_status)
            scanned_hits = []
            if scanned:
                tokens = set(terms(topic))
                scored = [(sum(math.log1p((len(scanned) + 1) / (frequency[term] + 1))
                               for term in tokens & words), record) for record, words in scanned]
                scored.sort(key=lambda item: (-item[0], item[1].memory_id))
                scanned_hits = [record for score, record in scored if score > 0][:self.max_results]
            hits = []
            for rank in range(max(len(result.records), len(scanned_hits))):
                if rank < len(result.records):
                    hits.append(result.records[rank])
                if rank < len(scanned_hits):
                    hits.append(scanned_hits[rank])
            ranked.append(tuple(dict.fromkeys(record.memory_id for record in hits)))
            for record in hits:
                records.setdefault(record.memory_id, record)
                indexes.setdefault(record.memory_id, set()).add(index)
        selected, seen = [], set()
        for rank in range(max(map(len, ranked), default=0)):
            for candidates in ranked:
                if rank >= len(candidates) or candidates[rank] in seen:
                    continue
                identity = candidates[rank]
                seen.add(identity)
                record = records[identity]
                record = replace(record, metadata={**record.metadata,
                    "topic_indexes": ",".join(map(str, sorted(indexes[identity])))})
                selected.extend(_archive_originals(record))
        state = ("unavailable" if "unavailable" in statuses else
                 "degraded" if "degraded" in statuses else
                 "disabled" if statuses and all(status == "disabled" for status in statuses) else "available")
        source_states = (("archive", state),) + ((("archive_scan", scan_status),) if scan_status else ())
        return RecallResult(tuple(selected), topics=topics, source_status=source_states,
                            stop_reason="partial_source_failure" if any(value in {"unavailable", "degraded"}
                                                                        for _, value in source_states)
                            else "complete" if selected else "empty")

    def collect(self, query: str, *, exclude_source_ids=()) -> RecallResult:
        """Read both independent stores once; a partial index never hides Archive."""
        excluded = tuple(exclude_source_ids)
        if not isinstance(query, str) or not query.strip():
            return RecallResult(source_status=(("memory", "disabled"),))
        paused = False
        if self.memory_lifecycle is not None:
            try:
                paused = self.memory_lifecycle.is_paused()
            except Exception:
                paused = True
        if paused:
            current = RecallResult(source_status=(("semantic", "paused"),))
        elif not bool(getattr(self.conversation_memory, "enabled", True)):
            # Unavailable ports also declare enabled=False. Only an explicitly
            # disabled port permits the old SQLite conversation fallback.
            try:
                state = self.conversation_memory.status().status
            except Exception:
                state = "unavailable"
            if state == "disabled":
                archive = self._collect_archive(query, excluded, include_current=True)
                return _rank_recall(replace(archive, topics=query_topics(query),
                               source_status=(("semantic", "disabled"), *archive.source_status)))
            current = RecallResult(source_status=(("semantic", "unavailable"),))
        else:
            try:
                current = _ConversationMemoryView(self.conversation_memory, user_id=self.user_id,
                                                  exclude_source_ids=excluded).search_evidence_result(
                                                      query, limit=self.max_results)
            except Exception:
                current = RecallResult(source_status=(("semantic", "unavailable"),),
                                       stop_reason="partial_source_failure")
        archive = self._collect_archive(query, excluded)
        return _merge_recall(current, archive, query=query)

    def render(self, result: RecallResult, *, max_chars: int) -> MemoryPrompt:
        return MemoryPromptBuilder(
            self.archive_memory, max_tokens=self.max_tokens, max_results=self.max_results,
            legacy_budget=max_chars, conversation_budget=max_chars, conversation_memory=None,
        ).render(result, max_chars=max_chars)

    def trace_sources(self, source_ids, *, exclude_source_ids=(), expand=False, limit=12):
        result = self.trace_sources_result(source_ids, exclude_source_ids=exclude_source_ids,
                                           expand=expand, limit=limit)
        if result.status == "unavailable":
            raise RuntimeError("MEMORY_TRACE_UNAVAILABLE")
        return result.records

    def trace_sources_result(self, source_ids, *, exclude_source_ids=(), expand=False, limit=12):
        """Read explicit original pointers without requiring the semantic provider."""
        from .recall_sources import read_archive_sources
        seeds, excluded = tuple(source_ids), tuple(exclude_source_ids)
        active = bool(getattr(self.conversation_memory, "enabled", True))
        if self.memory_lifecycle is not None:
            try:
                active = active and not self.memory_lifecycle.is_paused()
            except Exception:
                active = False
        originals = self._original_index()
        current = RecallResult(source_status=(("original_trace", "disabled"),))
        if active and originals is not None:
            try:
                read = originals.expand_sources if expand else originals.get_sources
                options = {"exclude_source_ids": excluded, **({"limit": limit} if expand else {})}
                records = tuple(_ConversationMemoryView._convert(record)
                                for record in read(self.user_id, seeds, **options))
                current = RecallResult(records, source_status=(("original_trace", "available"),))
            except (OSError, sqlite3.Error):
                current = RecallResult(source_status=(("original_trace", "unavailable"),))
        archive = RecallResult(source_status=(("archive_trace", "disabled"),))
        if not expand and bool(getattr(self.archive_memory, "enabled", True)):
            try:
                view = self._archive_view(excluded)
                if view.guard_failed:
                    raise sqlite3.OperationalError("ARCHIVE_FORGET_GUARD_UNAVAILABLE")
                records = read_archive_sources(self.archive_memory, seeds, excluded_sources=view.excluded)
                archive = RecallResult(tuple(records), source_status=(("archive_trace", "available"),))
            except Exception:
                archive = RecallResult(source_status=(("archive_trace", "unavailable"),))
        return _merge_recall(current, archive, query="")

    def _archive_only(
        self,
        query: str,
        budget: int,
        *,
        exclude_source_ids: Iterable[str],
    ) -> MemoryPrompt:
        if budget > 2400:
            return self.render(self.collect(query, exclude_source_ids=exclude_source_ids), max_chars=budget)
        return MemoryPromptBuilder(
            self._archive_view(exclude_source_ids),
            max_tokens=self.max_tokens,
            max_results=self.max_results,
            legacy_budget=budget,
            conversation_budget=0,
            conversation_memory=None,
        ).build(
            query,
            max_chars=budget,
            exclude_source_ids=exclude_source_ids,
        )


def _historical_source_id(source: str) -> str:
    prefix = "history:offline:" if source.startswith("offline-letter-pairs:") else "history:"
    return prefix + hashlib.sha256(source.encode("utf-8")).hexdigest()


def _archive_originals(record: MemoryRecord):
    """Keep imported speakers separate and use the index's stable source identity."""
    metadata = record.metadata
    if (metadata.get("import_kind") not in {
            "official_text_reply", "offline_recovered_text_reply", "local_letter_backup_v1"}
            or not all(isinstance(metadata.get(key), str) for key in ("user_content", "reply_text"))):
        return (record,)
    source = _historical_source_id(source_id(record))
    occurred_at = None if metadata.get("timestamp_known") is False else record.occurred_at
    records = []
    for actor, key in (("user", "user_content"), ("linli", "reply_text")):
        text = metadata[key]
        if not text.strip():
            continue
        records.append(replace(record, memory_id=f"{record.memory_id}:{actor}", text=text, occurred_at=occurred_at,
            provenance={**record.provenance, "source_record_id": source, "speaker": actor,
                        "verbatim": True, "source": "archive_original_text", "occurred_at": occurred_at},
            metadata={"canonical": True, "verbatim": True, "complete_original": True,
                      "history_actor": actor, "speaker": actor, "retrieval_route": "archive",
                      "topic_indexes": metadata.get("topic_indexes", ""),
                      "start": 0, "end": len(text), "part_count": 1}))
    return tuple(records)


def _merge_recall(current, archive, *, query):
    current = current or RecallResult()
    archive = archive or RecallResult()
    records = list(current.records)
    # An indexed original may be split into multiple complete pieces. Compare
    # the reconstructed actor text before dropping its duplicate Archive copy.
    complete = {}
    for record in current.records:
        if record.metadata.get("complete_original"):
            key = (source_id(record), record.metadata.get("speaker"))
            complete.setdefault(key, []).append(record)
    actor_text = {key: "".join(r.text for r in sorted(group,
                    key=lambda item: int(item.metadata.get("start", 0))))
                  for key, group in complete.items()}
    seen = {(source_id(r), r.metadata.get("speaker"), r.text) for r in records}
    for record in archive.records:
        key = (source_id(record), record.metadata.get("speaker"))
        identity = (*key, record.text)
        indexed_text = "".join(record.text[start:start + 2000].strip()
                               for start in range(0, len(record.text), 2000))
        if identity in seen or (record.metadata.get("complete_original")
                                and actor_text.get(key) == indexed_text):
            continue
        seen.add(identity)
        records.append(record)
    # Alternate complete source groups so a large index result cannot consume
    # all final capacity before an Archive-only exchange receives a chance.
    current_sources = {source_id(record) for record in current.records}
    current_groups, archive_groups = {}, {}
    for record in records:
        source = source_id(record)
        target = current_groups if source in current_sources else archive_groups
        target.setdefault(source, []).append(record)
    current_groups, archive_groups = list(current_groups.values()), list(archive_groups.values())
    records = []
    for index in range(max(len(current_groups), len(archive_groups))):
        if index < len(current_groups):
            records.extend(current_groups[index])
        if index < len(archive_groups):
            records.extend(archive_groups[index])
    states = tuple(dict.fromkeys((*current.source_status, *archive.source_status)))
    failed = any(state in {"unavailable", "degraded", "incomplete"} for _, state in states)
    return _rank_recall(RecallResult(tuple(records), topics=query_topics(query), source_status=states,
                        rounds=max(current.rounds, archive.rounds),
                        stop_reason="partial_source_failure" if failed else "complete" if records else "empty"))


def _rank_recall(result):
    """Rank whole exchanges by relevant details on either side, not their length.

    BM25 length normalization keeps a short original answer competitive with a
    long letter mentioning the same topic. Semantic candidates retain their own
    RRF route; lexical reranking must not erase a useful paraphrase match.
    These scores rank evidence only; they do not confirm claims.
    """
    if not result.topics or len(result.groups()) < 2:
        return result
    from .source_retrieval import terms
    groups = list(result.groups().values())
    documents = []
    for group in groups:
        actors = {}
        for record in group:
            actors.setdefault(record.metadata.get("speaker"), []).append(record.text)
        documents.append([set(terms("\n".join(texts))) for texts in actors.values()])
    frequency = Counter(term for sides in documents for term in set().union(*sides))
    average_length = sum(len(words) for sides in documents for words in sides) / sum(map(len, documents))
    inverse_frequency = {term: math.log1p((len(groups) - count + .5) / (count + .5))
                         for term, count in frequency.items()}
    topics = [set(terms(topic)) for topic in result.topics]
    scores = []
    for sides in documents:
        # terms() is binary within each side; with term frequency 1 the usual
        # k1=1.2, b=.75 BM25 factor reduces to this bounded length adjustment.
        scores.append(sum(max((sum(inverse_frequency[term] for term in words & topic)
            * 2.2 / (1 + 1.2 * (.25 + .75 * len(words) / max(1, average_length)))
            for words in sides), default=0) for topic in topics))
    lexical = sorted(range(len(groups)), key=lambda index: (-scores[index], index))
    semantic = [index for index, group in enumerate(groups)
                if any(record.metadata.get("retrieval_route") in {"semantic", "hybrid"} for record in group)]
    if semantic:
        ranks = {index: 1 / (60 + rank + 1) for rank, index in enumerate(lexical)}
        for rank, index in enumerate(semantic):
            ranks[index] += 1 / (60 + rank + 1)
        lexical.sort(key=lambda index: (-ranks[index], index))
    return replace(result, records=tuple(record for index in lexical for record in groups[index]))


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
