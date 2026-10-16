"""Durable deletion journal and local-store coordination for mailbox actions."""
from contextlib import ExitStack
from copy import deepcopy
import base64
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .letter_delete import RETENTION_SECONDS, Sources, apply_sql, legacy_plan, prune, sql_plan, world_plan


def _encode(value):
    if isinstance(value, bytes):
        return {'__letter_delete_bytes__': base64.b64encode(value).decode('ascii')}
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    return value


def _decode(value):
    if isinstance(value, dict):
        if set(value) == {'__letter_delete_bytes__'}:
            return base64.b64decode(value['__letter_delete_bytes__'])
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def _provider(server, stack):
    adapter = server.conversation_memory_adapter
    using = getattr(adapter, '_using_current', None)
    if using:
        adapter = stack.enter_context(using())
    if getattr(adapter, 'operation_pending', False):
        raise ValueError('LETTER_DELETE_MEMORY_BUSY')
    lock = getattr(adapter, '_lock', None)
    if lock:
        stack.enter_context(lock)
    return adapter


def _points(adapter, sources):
    store = getattr(getattr(adapter, 'backend', None), 'vector_store', None)
    if store is None:
        status = adapter.status()
        if status.status != 'disabled':
            raise ValueError('LETTER_DELETE_MEMORY_UNAVAILABLE')
        return []
    client = getattr(store, 'client', None)
    if not callable(getattr(client, 'scroll', None)):
        raise ValueError('LETTER_DELETE_MEMORY_UNAVAILABLE')
    if type(getattr(client, '_client', None)).__name__ != 'QdrantLocal':
        raise ValueError('LETTER_DELETE_LOCAL_MEMORY_REQUIRED')
    result = []
    for user in adapter._configured_user_aliases(sources.user):
        offset = None
        seen = set()
        while True:
            points, offset = client.scroll(collection_name=store.collection_name,
                scroll_filter=store._create_filter(adapter._provider_filters(user)),
                offset=offset, limit=256, with_payload=True, with_vectors=True)
            for point in points:
                payload = point.payload
                after = prune(payload, sources)
                if after != payload:
                    result.append({'id': str(point.id), 'vector': point.vector,
                                   'before': payload, 'after': after})
            if offset is None:
                break
            if str(offset) in seen:
                raise ValueError('LETTER_DELETE_MEMORY_UNAVAILABLE')
            seen.add(str(offset))
    return result


def _apply_points(adapter, patches, restore=False):
    if not patches:
        return
    from qdrant_client.models import PointStruct, PointIdsList
    store = adapter.backend.vector_store
    if type(getattr(store.client, '_client', None)).__name__ != 'QdrantLocal':
        raise ValueError('LETTER_DELETE_LOCAL_MEMORY_REQUIRED')
    for patch in patches:
        expected, desired = (patch['after'], patch['before']) if restore else (patch['before'], patch['after'])
        current = store.client.retrieve(collection_name=store.collection_name, ids=[patch['id']], with_payload=True)
        current = current[0].payload if current else None
        if current == desired:
            continue
        if current != expected:
            raise ValueError('LETTER_DELETE_RESTORE_CONFLICT')
        if desired is None:
            store.client.delete(collection_name=store.collection_name, points_selector=PointIdsList(points=[patch['id']]), wait=True)
        else:
            store.client.upsert(collection_name=store.collection_name,
                points=[PointStruct(id=patch['id'], vector=patch['vector'], payload=desired)], wait=True)
    adapter._evidence_cache.clear()


def _database_plans(server, adapter, sources):
    plans = []
    expected_world, _, _ = server.resolve_private_world_database(
        {**server._os.environ, 'OLIVIA_PRIVATE_WORLD_ENABLED': '1'}, user_id=sources.user)
    if expected_world:
        if expected_world.exists() and not hasattr(server.private_world_port, '_database_path'):
            raise ValueError('LETTER_DELETE_MEMORY_UNAVAILABLE')
        if expected_world.with_name('daily_life.sqlite3').exists() and server.daily_life_runtime is None:
            raise ValueError('LETTER_DELETE_MEMORY_UNAVAILABLE')
        if expected_world.with_name('diary.sqlite3').exists() and server.diary_store is None:
            raise ValueError('LETTER_DELETE_MEMORY_UNAVAILABLE')
    archive = server.memory_adapter
    path = getattr(archive, 'db_path', None)
    if path and Path(path).exists():
        plans.append({'path': str(path), 'patches': sql_plan(path, sources) + legacy_plan(path, sources)})
    originals = getattr(adapter, '_originals', None)
    if originals and originals.path.exists():
        plans.append({'path': str(originals.path), 'patches': sql_plan(originals.path, sources)})
    life = server.daily_life_runtime
    if life:
        plans.append({'path': str(life.store.path), 'patches': sql_plan(life.store.path, sources)})
    ledger = server.private_world_port
    if hasattr(ledger, '_database_path'):
        plans.append({'path': str(ledger._database_path), 'patches': world_plan(ledger, sources)})
    return [p for p in plans if p['patches']]


def _check_diary_provenance(server, row):
    diary = getattr(server, 'diary_store', None)
    if diary is None:
        return
    from runtime.diary.diary import _received_at, local_day
    stamp = _received_at(row)
    with sqlite3.connect(diary.path) as db:
        if stamp is None:
            exists = db.execute('SELECT 1 FROM diary_entries LIMIT 1').fetchone()
        else:
            day = local_day(stamp)
            exists = db.execute('SELECT 1 FROM diary_entries WHERE day IN (?,?) LIMIT 1', (day, day[:7])).fetchone()
        if exists:
            # These older narratives/facts contain quotations but no per-letter
            # provenance. Deleting the entire day would erase other sources.
            raise ValueError('LETTER_DELETE_UNATTRIBUTED_DIARY')


def _apply(server, adapter, entry, restore=False):
    originals = getattr(adapter, '_originals', None)
    if originals and not restore:
        originals.mark_letter_deleted(server._memory_config.user_id, entry['letter'], entry['part'])
    for database in entry['databases']:
        apply_sql(database['path'], _decode(database['patches']), restore=restore)
    _apply_points(adapter, entry['points'], restore=restore)
    getattr(adapter, '_evidence_cache', {}).clear()
    if originals and restore:
        originals.mark_letter_deleted(server._memory_config.user_id, entry['letter'], entry['part'], restore=True)
    row = next((r for r in server.store.letters if r.get('letter_id') == entry['letter_id']), None)
    if row is None:
        row = next((r for r in server.store.legacy_letters if r.get('letter_id') == entry['letter_id']), None)
    if row is not None:
        field = 'content' if entry['part'] == 'user' else 'reply_text'
        if restore:
            row[field] = entry['letter'].get(field, '')
            if 'metadata' in entry['letter']:
                row['metadata'] = deepcopy(entry['letter']['metadata'])
            if entry['part'] == 'user' and row.get('letter_status') == 'CANCELED':
                row['letter_status'] = entry['letter'].get('letter_status', 'FAILED')
            for key, value in entry.get('media', {}).items():
                row[key] = value
            row['_deleted_parts'] = [p for p in row.get('_deleted_parts', []) if p != entry['part']]
        else:
            row[field] = ''
            metadata = row.get('metadata')
            if isinstance(metadata, dict):
                metadata['user_content' if entry['part'] == 'user' else 'reply_text'] = ''
                if isinstance(metadata.get('backup_record'), dict):
                    metadata['backup_record'][field] = ''
            row['_deleted_parts'] = sorted(set(row.get('_deleted_parts', [])) | {entry['part']})
            for key in entry.get('media', {}):
                row.pop(key, None)
            if row.get('letter_status') not in {'COMPLETED', 'FAILED', 'CANCELED', 'CANCELLED'}:
                row['letter_status'] = 'CANCELED'
            row['input_revision'] = row.get('input_revision', 0) + 1
    entry['status'] = 'restored' if restore else 'deleted'
    server._persist_store_state()


def purge(server, now=None):
    now = time.time() if now is None else now
    trash = server.store.letter_trash
    expired = [key for key, value in trash.items()
               if value['status'] == 'restored' or value['status'] == 'deleted' and value['deleted_at'] + RETENTION_SECONDS <= now]
    if expired:
        previous = deepcopy(trash)
        for key in expired:
            del trash[key]
        try:
            server._persist_store_state()
        except Exception:
            server.store.letter_trash = previous
            raise
    return len(expired)


def operate(server, action, body):
    """Prepared intent is persisted before the first external-store mutation.

    An interrupted operation remains visible and retryable. Never discard its
    backup or report success when only some stores have been changed.
    """
    purge(server)
    if action == 'list':
        return {'status': 'READY', 'retention_days': 30, 'items': [
                {'id': key, 'letter_id': item['letter_id'], 'part': item['part'], 'status': item['status'],
                 'deleted_at': item['deleted_at'], 'expires_at': item['deleted_at'] + RETENTION_SECONDS,
                 'summary': item['letter'].get('content' if item['part'] == 'user' else 'reply_text', '')[:120]}
                for key, item in server.store.letter_trash.items()
                if item.get('user_id', 'local-user') == server._memory_config.user_id]}
    with ExitStack() as stack:
        adapter = _provider(server, stack)
        if action == 'restore':
            entry = server.store.letter_trash.get(body.get('id'))
            if entry is None:
                raise ValueError('LETTER_DELETE_NOT_FOUND')
            if entry.get('user_id', 'local-user') != server._memory_config.user_id:
                raise ValueError('LETTER_DELETE_NOT_FOUND')
            if entry.get('provider_root') != str(getattr(getattr(adapter, 'config', None), 'data_root', '')):
                raise ValueError('LETTER_DELETE_MEMORY_UNAVAILABLE')
            # Older stores attribute memories to an exchange, not to each side.
            # Restore surviving backups in reverse deletion order so restoring
            # one side cannot reintroduce evidence of a still-deleted other side.
            group = sorted((item for item in server.store.letter_trash.values()
                            if item['letter_id'] == entry['letter_id'] and item.get('user_id', 'local-user') == server._memory_config.user_id),
                           key=lambda item: item['deleted_at'], reverse=True)
            if any(item.get('provider_root') != entry.get('provider_root') for item in group):
                raise ValueError('LETTER_DELETE_MEMORY_UNAVAILABLE')
            for item in group:
                item['status'] = 'restoring'
            server._persist_store_state()
            for item in group:
                _apply(server, adapter, item, restore=True)
            purge(server)
            return {'status': 'RESTORED'}
        if action != 'delete' or body.get('part') not in {'user', 'reply'} or not isinstance(body.get('letter_id'), str):
            raise ValueError('LETTER_DELETE_INVALID')
        lid, part = body['letter_id'], body['part']
        entry = next((e for e in server.store.letter_trash.values() if e['letter_id'] == lid and e['part'] == part
                      and e.get('user_id', 'local-user') == server._memory_config.user_id), None)
        if entry:
            if entry['status'] == 'deleted':
                return {'status': 'DELETED'}
            if entry['status'] == 'restoring':
                raise ValueError('LETTER_DELETE_RESTORE_PENDING')
        else:
            rows = server._letter_collection('current', strict=True, maintenance=False)
            row = next((row for row in rows if row.get('letter_id') == lid), None)
            field = 'content' if part == 'user' else 'reply_text'
            if row is None or not row.get(field) or part == 'user' and row.get('origin') == 'proactive':
                raise ValueError('LETTER_DELETE_NOT_FOUND')
            _check_diary_provenance(server, row)
            sources = Sources(row, part, server._memory_config.user_id)
            entry = {'letter_id': lid, 'part': part, 'letter': deepcopy(row), 'status': 'prepared',
                     'user_id': server._memory_config.user_id,
                     'provider_root': str(getattr(getattr(adapter, 'config', None), 'data_root', '')),
                     'deleted_at': time.time(), 'databases': _encode(_database_plans(server, adapter, sources)),
                     'points': _points(adapter, sources),
                     'media': {k: v for k, v in row.items() if (
                         part == 'reply' and (k.startswith(('media_', 'image_', 'music_', 'video_', 'audio_')) or k in {'reply_segments', 'reply_video_url', 'reply_audio_url', 'reply_structure', 'reply_signature'})
                         or part == 'user' and k in {'material', 'incoming_media_observations'})}}
            key = uuid.uuid4().hex
            server.store.letter_trash[key] = entry
            try:
                server._persist_store_state()
            except Exception:
                del server.store.letter_trash[key]
                raise
        _apply(server, adapter, entry)
        return {'status': 'DELETED', 'retention_days': 30}
