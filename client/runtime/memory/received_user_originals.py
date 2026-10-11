"""Index durable user receipts separately from acknowledged assistant exchanges."""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib


@dataclass(frozen=True)
class ReceivedOriginal:
    source_id: str
    user_message: str
    occurred_at: datetime
    exchange_sources: tuple[str, ...]
    channel: str
    platform_id: str


def _received_at(row):
    value = row.get('life_received_at')
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.astimezone(timezone.utc) if stamp.utcoffset() is not None else None
    except (AttributeError, TypeError, ValueError):
        # Local letter stores historically carry their receive time as epoch.
        value = row.get('created_at')
        if type(value) in (int, float):
            try:
                return datetime.fromtimestamp(value, timezone.utc)
            except (ValueError, OverflowError, OSError):
                pass
        return None


def _undelivered_letter(row):
    """A failed letter was never read by Linli ("林离也还没有读到"); a resend is a new letter."""
    return (row.get('channel') or 'letter') == 'letter' and row.get('letter_status') == 'FAILED'


def undelivered_letter_sources(rows):
    """Receipt identities of failed letters, so an earlier arrival index can be retracted."""
    return tuple(dict.fromkeys(
        'received-user:letter:' + hashlib.sha256(row['letter_id'].encode()).hexdigest()
        for row in rows if isinstance(row, Mapping) and isinstance(row.get('letter_id'), str)
        and row['letter_id'] and _undelivered_letter(row)))


def _origins(row):
    """The (channel, binding, identifier) keys under which received_originals indexes a row."""
    if not isinstance(row, Mapping):
        return ()
    channel = row.get('channel') or 'letter'
    if channel == 'letter':
        letter_id = row.get('letter_id')
        return (('letter', '', letter_id),) if isinstance(letter_id, str) and letter_id else ()
    binding, sources = row.get('binding_id'), row.get('source_messages')
    if channel in {'qq', 'wechat'} and isinstance(binding, str) and binding and isinstance(sources, Mapping):
        return tuple((channel, binding, key) for key in sources if isinstance(key, str) and key)
    return ()


def rows_sharing_originals(rows, anchors):
    """The rows that index any original the anchor rows index, the anchors included.

    received_originals groups by receipt identity, so projecting just these rows
    yields exactly the anchors' receipts, each with all of its rows, however many
    unrelated rows the archive holds.
    """
    wanted = {origin for row in anchors for origin in _origins(row)}
    names = {origin[2] for origin in wanted}
    result = []
    for row in rows if names else ():
        if not isinstance(row, Mapping):
            continue
        sources, letter_id = row.get('source_messages'), row.get('letter_id')
        if ((isinstance(sources, Mapping) and not names.isdisjoint(sources))
                or (isinstance(letter_id, str) and letter_id in names)) and wanted.intersection(_origins(row)):
            result.append(row)
    return result


def received_originals(rows, *, include_undelivered=False):
    """Caller supplies durable rows. Never read reply_text or synthetic media rows.

    Failed letters are left out of the memory index; a caller evaluating the
    letter currently being (re)sent passes include_undelivered=True.
    """
    grouped = {}
    for row in rows:
        if (not isinstance(row, Mapping) or row.get('origin') == 'proactive' or row.get('read_only')
                or not isinstance(row.get('letter_id'), str) or not row['letter_id']
                or not include_undelivered and _undelivered_letter(row)):
            continue
        channel = row.get('channel') or 'letter'
        if channel not in {'qq', 'wechat', 'letter'}:
            continue
        stamp = _received_at(row)
        if stamp is None:
            continue
        if channel in {'qq', 'wechat'}:
            binding = row.get('binding_id')
            sources = row.get('source_messages')
            if not isinstance(binding, str) or not binding or not isinstance(sources, Mapping):
                continue
        else:
            binding = ''
            sources = {row['letter_id']: row.get('content')}
        revision = row.get('reply_revision', 1)
        revision = revision if type(revision) is int and revision >= 1 else 1
        exchange = 'reply:' + row['letter_id'] + ':' + str(revision)
        for identifier, text in sources.items():
            if not isinstance(identifier, str) or not identifier or not isinstance(text, str) or not text.strip():
                continue
            identity = identifier if channel == 'letter' else binding + '\0' + identifier
            source_id = 'received-user:' + channel + ':' + hashlib.sha256(identity.encode()).hexdigest()
            grouped.setdefault(source_id, []).append((text, stamp, len(sources) == 1, exchange, channel, identifier))
    result = []
    for source_id, entries in grouped.items():
        if len({entry[0] for entry in entries}) != 1:
            continue  # Conflicting source IDs must not silently overwrite originals.
        originals = [entry for entry in entries if entry[2]] or entries
        chosen = min(originals, key=lambda entry: entry[1])
        result.append(ReceivedOriginal(source_id, chosen[0], chosen[1],
            tuple(dict.fromkeys(entry[3] for entry in entries)), chosen[4], chosen[5]))
    return tuple(result)


def index_received_rows(adapter, rows, *, user_id, memory_lifecycle=None):
    """Synchronous local writes, suitable for asyncio.to_thread; returns indexed refs."""
    from .conversation_memory_identity import normalize_conversation_memory_user_id
    user_id = normalize_conversation_memory_user_id(user_id)
    write = getattr(adapter, 'index_received_user', None)
    if not getattr(adapter, 'enabled', False) or not callable(write):
        return ()
    if memory_lifecycle is not None and memory_lifecycle.is_paused():
        return ()
    indexed = []
    for record in received_originals(rows):
        def operation():
            return write(user_id=user_id, source_id=record.source_id,
                user_message=record.user_message, occurred_at=record.occurred_at,
                exchange_sources=record.exchange_sources)
        result = (memory_lifecycle.run_write(operation, occurred_at=record.occurred_at)
                  if memory_lifecycle is not None else operation())
        if result:
            indexed.append(record)
    retract = getattr(adapter, 'retract_received_user', None)
    failed = undelivered_letter_sources(rows)
    if failed and callable(retract):
        def retraction():
            return retract(user_id=user_id, source_ids=failed)
        if memory_lifecycle is not None:
            memory_lifecycle.run_write(retraction, occurred_at=datetime.now(timezone.utc))
        else:
            retraction()
    return tuple(indexed)
