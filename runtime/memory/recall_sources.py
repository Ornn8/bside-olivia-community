"""Resolve explicit evidence pointers to the existing read-only archive."""
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json

from .memory_port import LEGACY_LETTERS, MemoryRecord, MemoryUnavailable


def _history_id(source):
    prefix = "history:offline:" if source.startswith("offline-letter-pairs:") else "history:"
    return prefix + hashlib.sha256(source.encode("utf-8")).hexdigest()


def _original_time(value):
    # SQLite legacy rows preserve epoch timestamps as strings.
    try:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, str) and value.strip().replace('.', '', 1).lstrip('-').isdigit():
            value = float(value)
        stamp = (datetime.fromtimestamp(value, timezone.utc) if isinstance(value, (int, float))
                 else datetime.fromisoformat(value.replace("Z", "+00:00")))
        return stamp if stamp.utcoffset() is not None else None
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None


def archive_source_aliases(source, metadata) -> frozenset[str]:
    """The same content-checked identity aliases for search and explicit reads."""
    if (not isinstance(source, str) or not source or not isinstance(metadata, Mapping)
            or metadata.get("import_kind") not in {
                "official_text_reply", "offline_recovered_text_reply", "local_letter_backup_v1",
            }):
        return frozenset()
    user, reply = metadata.get("user_content"), metadata.get("reply_text")
    if not all(isinstance(text, str) and text.strip() for text in (user, reply)):
        return frozenset()
    pair_hash = hashlib.sha256(json.dumps([user, reply], ensure_ascii=False).encode("utf-8")).hexdigest()
    relation = "relationship-letter:" + pair_hash
    aliases = {source, _history_id(source), relation, _history_id(relation)}
    backup = metadata.get("backup_record")
    if (isinstance(backup, Mapping) and isinstance(backup.get("source_id"), str)
            and backup["source_id"] and backup.get("content", user) == user
            and backup.get("reply_text", reply) == reply):
        aliases.update((backup["source_id"], _history_id(backup["source_id"])))
    return frozenset(aliases)


def read_archive_sources(archive, source_ids, *, excluded_sources=()) -> tuple[MemoryRecord, ...]:
    """Read explicit pointers within this caller-selected user's archive only.

    The caller supplies both request exclusions and forgotten aliases. No index,
    model, or writable store is opened; read errors propagate as unavailable.
    """
    requested = tuple(dict.fromkeys(source for source in source_ids if isinstance(source, str) and source))
    if not requested or not bool(getattr(archive, "enabled", True)):
        return ()
    excluded = frozenset(excluded_sources)
    reader = getattr(archive, "list_legacy", None)
    if not callable(reader):
        raise MemoryUnavailable("ARCHIVE_SOURCE_READ_UNAVAILABLE")
    groups = {}
    # Existing list_legacy owns user isolation and the read transaction. Exact
    # pair identity matches relationship import; similarity is insufficient.
    for row in reader():
        if not isinstance(row, Mapping):
            continue
        metadata = row.get("metadata")
        if not isinstance(metadata, Mapping) or metadata.get("import_kind") not in {
            "official_text_reply", "offline_recovered_text_reply", "local_letter_backup_v1",
        }:
            continue
        source = row.get("source_record_id")
        user, reply = metadata.get("user_content"), metadata.get("reply_text")
        if (not isinstance(source, str) or not source
                or not all(isinstance(text, str) and text.strip() for text in (user, reply))):
            continue
        pair_hash = hashlib.sha256(json.dumps([user, reply], ensure_ascii=False).encode("utf-8")).hexdigest()
        aliases = archive_source_aliases(source, metadata)
        # list_legacy.created_at may fall back to imported_at; it is never evidence.
        stamp = None if metadata.get("timestamp_known") is False else _original_time(row.get("occurred_at"))
        key = (pair_hash, stamp.astimezone(timezone.utc).isoformat() if stamp else None)
        groups.setdefault(key, []).append((source, user, reply, stamp, aliases))

    result = []
    for entries in groups.values():
        aliases = set().union(*(entry[4] for entry in entries))
        if aliases & excluded:
            continue
        matched = next((source for source in requested if source in aliases), None)
        if matched is None:
            continue
        source, user, reply, stamp, _ = next(entry for entry in entries if matched in entry[4])
        original_time = stamp.isoformat() if stamp else None
        indexed_source = _history_id(source)
        for actor, text in (("user", user), ("linli", reply)):
            result.append(MemoryRecord(
                memory_id=f"archive-original:{hashlib.sha256(source.encode('utf-8')).hexdigest()}:{actor}",
                domain=LEGACY_LETTERS, text=text, source="archive_original_text",
                created_at=int(stamp.timestamp()) if stamp else 0, occurred_at=original_time,
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                provenance={"domain": LEGACY_LETTERS, "source": "archive_original_text",
                            "source_record_id": indexed_source, "archive_source_id": source,
                            "speaker": actor, "verbatim": True, "occurred_at": original_time},
                metadata={"canonical": True, "verbatim": True, "complete_original": True,
                          "speaker": actor, "history_actor": actor, "start": 0, "end": len(text),
                          "part_count": 1, "retrieval_route": "archive_source",
                          "requested_source_id": matched,
                          "source_aliases": json.dumps(sorted(aliases), ensure_ascii=False)},
            ))
    return tuple(result)
