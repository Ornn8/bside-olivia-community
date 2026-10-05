"""Portable letter/chat text backups; no provider, credentials or media paths."""
from datetime import datetime, timezone, timedelta
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
    # Legacy PR #508 wall-clock strings are Beijing time. Canonicalize the
    # instant before hashing so epoch, UTC and +08:00 imports share an identity.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed.astimezone(timezone.utc).isoformat()


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
    if 'channel' in value:
        channel = value['channel']
        delivery = value.get('delivery_status')
        if channel not in ('qq', 'wechat') or delivery not in ('DELIVERED', 'RECEIVED_ONLY'):
            raise ValueError('LETTER_BACKUP_INVALID')
        if delivery == 'RECEIVED_ONLY' and (result['reply_text'] or result['replied_at'] is not None):
            raise ValueError('LETTER_BACKUP_INVALID')
        result.update(channel=channel, delivery_status=delivery)
    elif 'delivery_status' in value:
        raise ValueError('LETTER_BACKUP_INVALID')
    return result


def identity(record):
    return hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def personal_chat_letters(chats):
    """Snapshot chat text for Archive, without queue state or unsent drafts."""
    snapshots = [dict(row) for row in chats]
    parents = {row.get('letter_id'): row for row in snapshots
               if row.get('channel') in ('qq', 'wechat')}
    result = []
    for row in snapshots:
        if row.get('channel') not in ('qq', 'wechat'):
            continue
        parent = parents.get(row['superseded_by']) if row.get('superseded_by') else None
        sources = row.get('source_messages')
        parent_sources = parent.get('source_messages') if parent else None
        # Merged children are kept in the live queue as receipts. Exclude them
        # only when their inputs really survive in the exported parent.
        if (parent and parent is not row and parent.get('channel') == row['channel']
                and parent.get('binding_id') == row.get('binding_id')
                and isinstance(parent.get('content'), str) and parent['content']
                and isinstance(sources, dict) and sources and isinstance(parent_sources, dict)
                and all(key in parent_sources and parent_sources[key] == text for key, text in sources.items())):
            continue
        delivered = row.get('delivery_status') == 'DELIVERED'
        content = row.get('content') or ''
        reply = (row.get('reply_text') or '') if delivered else ''
        if not content and not reply:
            continue
        result.append({
            'letter_id': row.get('letter_id') or '', 'content': content, 'reply_text': reply,
            'title': '', 'origin': row.get('origin') or 'user',
            'reply_mode': row.get('reply_mode') or 'future_im',
            'letter_status': 'COMPLETED' if delivered else 'RECEIVED',
            'created_at': row.get('user_sent_at') or row.get('created_at'),
            'replied_at': (row.get('replied_at') or row.get('private_world_occurred_at')) if delivered else None,
            'channel': row['channel'],
            'delivery_status': 'DELIVERED' if delivered else 'RECEIVED_ONLY',
        })
    return result


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
                **({key: letter.get(key) for key in ('channel', 'delivery_status')}
                   if 'channel' in letter else {}),
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


def _soul_backup(manifest):
    """Read only text exchanges from the SOUL0001 manifest (PR #508 format)."""
    if not isinstance(manifest, dict) or len(json.dumps(manifest, ensure_ascii=False).encode('utf-8')) > MAX_BYTES:
        raise ValueError('LETTER_BACKUP_INVALID')
    memory = manifest.get('memory')
    if not isinstance(memory, dict) or not isinstance(memory.get('exchanges'), list):
        raise ValueError('LETTER_BACKUP_INVALID')
    exchanges = memory['exchanges']
    if len(exchanges) > MAX_LETTERS:
        raise ValueError('LETTER_BACKUP_TOO_LARGE')
    rows = []
    for item in exchanges:
        if not isinstance(item, dict):
            raise ValueError('LETTER_BACKUP_INVALID')
        content, reply = (_text('' if item.get(key) is None else item[key])
                          for key in ('incoming', 'reply'))
        if not (content.strip() or reply.strip()):
            continue
        date, clock = (_text('' if item.get(key) is None else item[key], 32)
                       for key in ('date', 'time'))
        stamp = None
        if date:
            try:
                stamp = datetime.strptime(date + ' ' + (clock or '00:00'), '%Y-%m-%d %H:%M').replace(
                    tzinfo=timezone(timedelta(hours=8))).isoformat()
            except ValueError:
                # Unknown dates stay unknown; never substitute the import date.
                pass
        rows.append({'content': content, 'reply_text': reply, 'created_at': stamp,
                     'origin': 'user', 'reply_mode': 'text', 'letter_status': 'COMPLETED'})
    return {'schema_version': SCHEMA, 'letters': rows}


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
    soul = isinstance(payload, dict) and payload.get('format') == 'soul'
    if soul:
        payload = _soul_backup(payload.get('manifest'))
    rows = validate_backup(payload)
    def pair(row):
        return tuple(re.sub(r'\s+', ' ', row.get(key) or '').strip() for key in ('content', 'reply_text'))
    existing_pairs = {pair(row) for row in existing} if soul else set()
    seen = {identity(row) for row in export_letters(existing)['letters']} if existing else set()
    records, duplicates = [], 0
    for position,row in enumerate(rows):
        digest = identity(row)
        if digest in seen or (soul and pair(row) in existing_pairs):
            duplicates += 1
            continue
        seen.add(digest)
        if soul:
            existing_pairs.add(pair(row))
        offline = bool(re.fullmatch(r'offline-letter-pairs:[0-9a-f]{64}:\d{6}', row['source_id']))
        if offline:
            from .offline_letter_pairs import _pair_archive_content
        records.append(LegacyLetter(
            content=(_pair_archive_content((row['content'], row['reply_text'])) if offline
                     else json.dumps(row, ensure_ascii=False, sort_keys=True)),
            source_record_id=row['source_id'] if offline else 'letter-backup:' + digest, source='letter-backup',
            occurred_at=row['created_at'],
            metadata={'import_kind': KIND, 'backup_record': row, 'import_position': position,
                      **({'source_format': 'soul'} if soul else {}),
                      'user_content': row['content'], 'reply_text': row['reply_text'],
                      'replied_at': row['replied_at']},
        ))
    result = adapter.import_legacy_records(records, atomic=True)
    if result.rolled_back or result.rejected:
        raise ValueError('LETTER_BACKUP_WRITE_FAILED')
    return {'status': 'APPLIED', 'seen': len(rows), 'inserted': result.inserted,
            'duplicates': duplicates + result.duplicates, 'memory_mode': 'originals',
            'provider_calls': 0}
