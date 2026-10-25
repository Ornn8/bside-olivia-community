"""The rest of a backup besides letters and chats: her diary with his comments, the facts she keeps, and calls.

They ride in the same olivia.letters.v1 file as optional keys, so a file from an earlier version still imports and
an earlier version still reads the letters of a newer file. Calls go in as letter-shaped text records (titled
语音通话, source ids call-…), which every version can restore. Everything is validated before the first write; nothing
calls a model.
"""
from datetime import datetime
import json
import re

from .letter_backup import MAX_BYTES, SCHEMA, _text, export_letters, validate_backup

DAY = re.compile(r'\d{4}-\d{2}(-\d{2})?')
SOURCE = re.compile(r'[A-Za-z0-9._:-]{1,160}')
MEMORY_KEYS = ('category', 'canonical', 'manual', 'actor', 'origin', 'verbatim', 'history_actor',
               'history_extraction_version', 'updated_at')
MAX_ENTRIES = 5000
MAX_COMMENTS = 20000
MAX_MEMORIES = 20000


def _invalid():
    return ValueError('LETTER_BACKUP_INVALID')


def _stamp(value):
    value = _text(value, 64)
    datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value


def call_letters(calls):
    """Each call's stretches as letters: what he said is the letter, what she said aloud is the reply."""
    from runtime.call.store import memory_rows
    rows = []
    for call in calls or ():
        if not isinstance(call, dict):
            continue
        try:
            stretches = list(memory_rows(call))
        except (KeyError, TypeError, ValueError):
            continue
        for row in stretches:
            rows.append({'letter_id': row['letter_id'], 'content': row['content'], 'reply_text': row['reply_text'],
                         'title': '语音通话', 'origin': 'user', 'reply_mode': 'text', 'letter_status': 'COMPLETED',
                         'created_at': row['created_at'], 'replied_at': row['created_at']})
    return rows


def diary_section(store):
    return store.backup() if store is not None else None


def memory_section(memory, user_id):
    """The stored facts, or None when long-term memory cannot be read now (the file then says so)."""
    if memory is None or not getattr(memory, 'enabled', False):
        return []
    try:
        walk = getattr(memory, 'all_memories', None)
        records = walk(user_id=user_id) if callable(walk) else memory.list_memories(user_id=user_id, limit=1000)
    except Exception:  # noqa: BLE001 - an unreadable store must not stop the letters from being saved
        return None
    return [{'text': record.text, 'source_id': record.source_id,
             'occurred_at': record.occurred_at.isoformat() if record.occurred_at else None,
             'metadata': {key: record.metadata[key] for key in MEMORY_KEYS if key in record.metadata}}
            for record in records]


def _entry(value):
    if not isinstance(value, dict) or not isinstance(value.get('day'), str) or not DAY.fullmatch(value['day']):
        raise _invalid()
    facts = value.get('facts')
    if not isinstance(facts, list) or len(facts) > 40:
        raise _invalid()
    kept = []
    for fact in facts:
        if not isinstance(fact, dict) or not isinstance(fact.get('kind'), str):
            raise _invalid()
        date = fact.get('date')
        kept.append({'kind': _text(fact['kind'], 32), 'text': _text(fact.get('text'), 200),
                     'date': None if date is None else _text(date, 10), 'quote': _text(fact.get('quote', ''), 200)})
    body = _text(value.get('body'), 20000)
    if not body.strip() or not all(type(value.get(key)) is bool for key in ('short', 'seen')):
        raise _invalid()
    return {'day': value['day'], 'written_at': _stamp(value.get('written_at')), 'title': _text(value.get('title'), 100),
            'mood': _text(value.get('mood'), 40), 'body': body, 'facts': kept, 'short': value['short'],
            'seen': value['seen'], 'style': _text(value.get('style', 'ordinary'), 32)}


def _comment(value):
    if not isinstance(value, dict) or not isinstance(value.get('day'), str) or not DAY.fullmatch(value['day']):
        raise _invalid()
    text = _text(value.get('text'), 500)
    if not text.strip():
        raise _invalid()
    return {'day': value['day'], 'written_at': _stamp(value.get('written_at')), 'text': text}


def _memory(value):
    if not isinstance(value, dict) or not isinstance(value.get('source_id'), str) \
            or not SOURCE.fullmatch(value['source_id']):
        raise _invalid()
    text = _text(value.get('text'), 2000)
    metadata = value.get('metadata', {})
    if not text.strip() or not isinstance(metadata, dict) or set(metadata) - set(MEMORY_KEYS) or not all(
            item is None or type(item) in (bool, int, float, str) and len(str(item)) <= 200
            for item in metadata.values()):
        raise _invalid()
    occurred = value.get('occurred_at')
    return {'text': text, 'source_id': value['source_id'], 'metadata': dict(metadata),
            'occurred_at': None if occurred is None else _stamp(occurred)}


def export(letters, *, diary, memories):
    """The whole backup: letters and chats as before, plus the diary and the stored facts when there are any."""
    payload = export_letters(letters)
    if diary is not None:
        payload['diary'] = diary
    if memories is None:
        payload['memories_unavailable'] = True
    else:
        payload['memories'] = memories
    validate_backup(payload)
    validate_sections(payload)
    return payload


def read(payload):
    """The diary and facts of a file chosen for import, checked before anything is written. A .soul or
    letter_pairs.json file, or a backup from an earlier version, has none."""
    if isinstance(payload, str):
        if len(payload.encode('utf-8')) > MAX_BYTES:
            raise ValueError('LETTER_BACKUP_TOO_LARGE')
        payload = json.loads(payload.lstrip('﻿'), strict=False)
    if not isinstance(payload, dict) or payload.get('schema_version') != SCHEMA:
        return [], [], []
    return validate_sections(payload)


def validate_sections(payload):
    """(diary entries, diary comments, memories) of a backup; empty for a file that has none."""
    diary = payload.get('diary') if isinstance(payload, dict) else None
    memories = payload.get('memories') if isinstance(payload, dict) else None
    if diary is None:
        entries, comments = [], []
    elif not isinstance(diary, dict) or not isinstance(diary.get('entries'), list) \
            or not isinstance(diary.get('comments'), list) \
            or len(diary['entries']) > MAX_ENTRIES or len(diary['comments']) > MAX_COMMENTS:
        raise _invalid()
    else:
        entries, comments = [_entry(row) for row in diary['entries']], [_comment(row) for row in diary['comments']]
    if memories is None:
        memories = []
    elif not isinstance(memories, list) or len(memories) > MAX_MEMORIES:
        raise _invalid()
    return entries, comments, [_memory(row) for row in memories]
