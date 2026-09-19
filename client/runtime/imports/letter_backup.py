"""Portable text-only mailbox backups; no provider, credentials or media paths."""
from datetime import datetime, timezone
import hashlib
import json
import math
import re

from runtime.memory.memory_port import LegacyLetter

SCHEMA = 'olivia.letters.v1'
KIND = 'local_letter_backup_v1'
MAX_BYTES = 16 * 1024 * 1024
MAX_LETTERS = 10000


def is_backup(metadata):
    return isinstance(metadata, dict) and metadata.get('import_kind') == KIND


def _text(value, maximum=50000):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError('LETTER_BACKUP_INVALID')
    if any(ord(c) < 32 and c not in '\n\r\t' for c in value):
        raise ValueError('LETTER_BACKUP_INVALID')
    value.encode('utf-8')
    return value


def _time(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError('LETTER_BACKUP_INVALID')
    if isinstance(value, str) and re.fullmatch(r'-?\d+(\.\d+)?', value):
        value = float(value)
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError('LETTER_BACKUP_INVALID')
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    parsed = datetime.fromisoformat(_text(value, 64).replace('Z', '+00:00'))
    # Older archive timestamps may have no timezone. Preserve that uncertainty.
    return parsed.isoformat()


def _record(value):
    if not isinstance(value, dict):
        raise ValueError('LETTER_BACKUP_INVALID')
    result = {key: _text(value.get(key, ''), maximum) for key, maximum in
              (('content', 50000), ('reply_text', 50000), ('title', 1000),
               ('origin', 32), ('reply_mode', 64), ('letter_status', 32))}
    if not (result['content'].strip() or result['reply_text'].strip()):
        raise ValueError('LETTER_BACKUP_INVALID')
    result['created_at'] = _time(value.get('created_at'))
    result['replied_at'] = _time(value.get('replied_at'))
    # Whitelisted source identity is data, never an instruction or filesystem path.
    result['source_id'] = _text(value.get('source_id', ''), 512)
    return result


def identity(record):
    return hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def export_letters(letters):
    rows, seen = [], set()
    for letter in letters:
        metadata = letter.get('metadata') or {}
        if is_backup(metadata):
            record = _record(metadata['backup_record'])
        else:
            record = _record({
                'content': letter.get('content') or '', 'reply_text': letter.get('reply_text') or '',
                'title': letter.get('title') or '', 'origin': letter.get('origin') or 'user',
                'reply_mode': letter.get('reply_mode') or 'text',
                'letter_status': str(letter.get('letter_status', 'COMPLETED')),
                # Archive created_at can be the import date; explicit unknown
                # Preserve the original occurred_at across export.
                'created_at': letter.get('occurred_at', letter.get('created_at')),
                'replied_at': letter.get('replied_at'),
                'source_id': letter.get('source_record_id') or letter.get('letter_id') or '',
            })
        digest = identity(record)
        if digest not in seen:
            rows.append(record)
            seen.add(digest)
    payload = {'schema_version': SCHEMA, 'exported_at': datetime.now(timezone.utc).isoformat(),
               'media_included': False, 'letters': rows}
    validate_backup(payload)
    return payload


def validate_backup(payload):
    if not isinstance(payload, dict) or payload.get('schema_version') != SCHEMA:
        raise ValueError('LETTER_BACKUP_INVALID')
    rows = payload.get('letters')
    if not isinstance(rows, list) or len(rows) > MAX_LETTERS:
        raise ValueError('LETTER_BACKUP_INVALID')
    if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > MAX_BYTES:
        raise ValueError('LETTER_BACKUP_TOO_LARGE')
    return tuple(_record(row) for row in rows)


def import_letters(payload, *, adapter, existing=()):
    # Validate the entire document before the first write. Re-exported imports
    # retain their original record identity; repeated backups do not duplicate.
    raw = None
    if isinstance(payload, str):
        raw = payload.encode('utf-8')
        if len(raw) > MAX_BYTES:
            raise ValueError('LETTER_BACKUP_TOO_LARGE')
        payload = json.loads(payload.lstrip('\ufeff'), strict=False)
    if isinstance(payload, list):
        from .offline_letter_pairs import parse_offline_letter_pair_bytes, _build_records
        raw, pairs = parse_offline_letter_pair_bytes(raw or json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        result = adapter.import_legacy_records(_build_records(raw, pairs), atomic=True)
        if result.rolled_back or result.rejected:
            raise ValueError('LETTER_BACKUP_WRITE_FAILED')
        return {'status': 'APPLIED', 'seen': len(pairs), 'inserted': result.inserted,
                'duplicates': result.duplicates, 'memory_mode': 'originals', 'provider_calls': 0}
    rows = validate_backup(payload)
    seen = {identity(row) for row in export_letters(existing)['letters']} if existing else set()
    records, duplicates = [], 0
    for position,row in enumerate(rows):
        digest = identity(row)
        if digest in seen:
            duplicates += 1
            continue
        seen.add(digest)
        offline = bool(re.fullmatch(r'offline-letter-pairs:[0-9a-f]{64}:\d{6}', row['source_id']))
        if offline:
            from .offline_letter_pairs import _pair_archive_content
        records.append(LegacyLetter(
            content=(_pair_archive_content((row['content'], row['reply_text'])) if offline
                     else json.dumps(row, ensure_ascii=False, sort_keys=True)),
            source_record_id=row['source_id'] if offline else 'letter-backup:' + digest, source='letter-backup',
            occurred_at=row['created_at'],
            metadata={'import_kind': KIND, 'backup_record': row, 'import_position': position,
                      'user_content': row['content'], 'reply_text': row['reply_text'],
                      'replied_at': row['replied_at']},
        ))
    result = adapter.import_legacy_records(records, atomic=True)
    if result.rolled_back or result.rejected:
        raise ValueError('LETTER_BACKUP_WRITE_FAILED')
    return {'status': 'APPLIED', 'seen': len(rows), 'inserted': result.inserted,
            'duplicates': duplicates + result.duplicates, 'memory_mode': 'originals',
            'provider_calls': 0}
