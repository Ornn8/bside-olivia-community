"""Source-exact, reversible local letter removal. Backups never enter retrieval."""
from copy import deepcopy
import hashlib
import json
import re
import sqlite3


RETENTION_SECONDS = 30 * 24 * 60 * 60
SOURCE_KEYS = {'source', 'source_id', 'source_letter_id', 'letter_id',
               'canonical_delivery_id', 'canonical_reply_id', 'delivery_id',
               'evidence_ref_id', 'earlier_source', 'later_source'}
SOURCE_LISTS = {'sources', 'source_ids', 'basis_event_ids', 'evidence_refs'}


class Sources:
    def __init__(self, letter, part, user='local-user'):
        self.letter = letter
        self.part = part
        self.user = user
        self.id = letter['letter_id']
        self.exact = {self.id}
        if part == 'user':
            self.exact.add('received-user:letter:' + hashlib.sha256(self.id.encode()).hexdigest())
            self.exact.add('reply:' + self.id + ':user')
        original = letter.get('source_record_id')
        if original:
            self.exact.add(original)
            digest = hashlib.sha256(original.encode()).hexdigest()
            self.exact.add(('history:offline:' if original.startswith('offline-letter-pairs:') else 'history:') + digest)

    def matches(self, value):
        if not isinstance(value, str):
            return False
        return value in self.exact or bool(re.fullmatch(
            r'(?:reply:)?' + re.escape(self.id) + r':[1-9][0-9]*(?:\.[A-Za-z0-9._:-]+)?', value))


def prune(value, sources):
    """Remove only explicit provenance; free text is never an identity match."""
    if isinstance(value, list):
        return [new for item in value if (new := prune(item, sources)) is not None]
    if not isinstance(value, dict):
        return value
    actor = value.get('history_actor', value.get('actor'))
    if actor in {'user', 'linli'} and actor != ('user' if sources.part == 'user' else 'linli'):
        return deepcopy(value)
    result = deepcopy(value)
    shared = False
    for key in SOURCE_LISTS & value.keys():
        if not isinstance(value[key], list):
            continue
        remaining = [item for item in value[key] if not sources.matches(item)]
        if remaining != value[key]:
            if not remaining:
                return None
            result[key] = remaining
            shared = True
    if any(sources.matches(value.get(key)) for key in SOURCE_KEYS):
        if not shared:
            return None
        for key in SOURCE_KEYS & value.keys():
            if sources.matches(value[key]):
                survivors = next((items for name, items in result.items()
                                  if name in SOURCE_LISTS and isinstance(items, list) and items and isinstance(items[0], str)), [])
                if key == 'source_id' and survivors:
                    result[key] = survivors[0]
                else:
                    result.pop(key)
    for key, item in list(result.items()):
        if isinstance(item, (dict, list)):
            result[key] = prune(item, sources)
    return result


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def sql_plan(path, sources):
    """Read owned tables only. Each patch stores a complete before/after row."""
    patches = []
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        allowed = {'originals', 'chunks', 'relationship_quotes', 'source_vectors', 'vector_pending',
                   'source_dependencies', 'conversation_memory', 'conversation_memory_fts', 'private_world_candidates'}
        allowed |= {t for t in tables if t.startswith(('life_', 'character_emotion_', 'character_development_'))}
        # Legacy pairs are intentionally handled separately: mailbox identity and
        # retrieval text must change together, including the protected SQL table.
        for table in sorted(tables & allowed):
            if table == 'life_settings':
                continue
            for record in db.execute('SELECT rowid AS _rowid_, * FROM ' + _quote(table)):
                before = dict(record)
                if before.get('user', before.get('user_id', sources.user)) != sources.user:
                    continue
                own = any(sources.matches(before.get(key)) for key in SOURCE_KEYS)
                if table in {'originals', 'chunks'} and own:
                    if before.get('actor') != ('user' if sources.part == 'user' else 'linli'):
                        continue
                after = None if own else deepcopy(before)
                if own and table == 'life_moments' and before.get('kind') == 'exchange':
                    payload = json.loads(before['payload'])
                    payload['updates'] = prune(payload.get('updates', []), sources)
                    payload['relationship'] = None
                    if sources.part == 'reply':
                        payload['current'], payload['boundaries'] = None, []
                    else:
                        payload['contact_choice'] = None
                    after = {**before, 'payload': json.dumps(payload, ensure_ascii=False, sort_keys=True)}
                if own and sources.part == 'reply' and table in {'life_exchange_user_text', 'life_user_routine'}:
                    continue
                if own and sources.part == 'user' and table == 'life_exchange_world_gate':
                    continue
                if after is not None:
                    for column in ('payload', 'payload_json', 'metadata_json', 'basis'):
                        if own and table == 'life_moments' and before.get('kind') == 'exchange' and column == 'payload':
                            continue
                        raw = after.get(column)
                        if not isinstance(raw, str):
                            continue
                        try:
                            decoded = json.loads(raw)
                        except ValueError:
                            raise ValueError('LETTER_DELETE_STORED_DATA_INVALID') from None
                        if isinstance(decoded, dict) and decoded.get('user_id', sources.user) != sources.user:
                            continue
                        cleaned = prune(decoded, sources)
                        if cleaned is None:
                            after = None
                            break
                        if cleaned != decoded:
                            after[column] = json.dumps(cleaned, ensure_ascii=False, sort_keys=True)
                if after != before:
                    patches.append({'table': table, 'before': before, 'after': after})
        # A shared project is a projection of its attributed updates. Removing
        # its newest update reveals the last surviving source, not an empty item.
        if 'life_moments' in tables:
            changed = {p['before']['_rowid_']: p['after'] for p in patches if p['table'] == 'life_moments'}
            surviving = {}
            columns = {r[1] for r in db.execute('PRAGMA table_info(life_moments)')}
            if {'occurred_at', 'kind'} <= columns:
                for raw in db.execute('SELECT rowid AS _rowid_,* FROM life_moments ORDER BY occurred_at,rowid'):
                    moment = changed.get(raw['_rowid_'], dict(raw))
                    if moment is not None:
                        payload = json.loads(moment['payload'])
                        for item in payload.get('progress' if moment['kind'] == 'daily' else 'updates', []):
                            if isinstance(item, dict) and 'id' in item:
                                surviving[item['id']] = item
                for patch in patches:
                    if patch['table'] == 'life_projects' and patch['after'] is None:
                        item = surviving.get(patch['before']['id'])
                        if item:
                            patch['after'] = {**patch['before'], 'payload': json.dumps(item, ensure_ascii=False, sort_keys=True)}
        removed_candidates = {p['before']['candidate_id'] for p in patches if p['table'] == 'private_world_candidates'}
        if removed_candidates and 'private_world_candidate_decisions' in tables:
            for row in db.execute('SELECT rowid AS _rowid_,* FROM private_world_candidate_decisions'):
                if row['candidate_id'] in removed_candidates:
                    patches.insert(0, {'table': 'private_world_candidate_decisions', 'before': dict(row), 'after': None})
        removed_memory = {p['before']['memory_id'] for p in patches if p['table'] == 'conversation_memory' and p['after'] is None}
        if removed_memory and 'conversation_memory_fts' in tables:
            for row in db.execute('SELECT rowid AS _rowid_,* FROM conversation_memory_fts'):
                if row['memory_id'] in removed_memory:
                    patches.append({'table': 'conversation_memory_fts', 'before': dict(row), 'after': None})
    return patches


def apply_sql(path, patches, *, restore=False):
    """Compare-and-swap each row; retry safely, never overwrite newer records."""
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        guards = []
        if any(p['table'] == 'legacy_letters' for p in patches):
            guards = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name IN ('legacy_letters_no_update','legacy_letters_no_delete')").fetchall()
            for name, _ in guards:
                db.execute('DROP TRIGGER ' + _quote(name))
        for patch in reversed(patches) if restore else patches:
            expected, desired = (patch['after'], patch['before']) if restore else (patch['before'], patch['after'])
            table = _quote(patch['table'])
            rowid = patch['before']['_rowid_']
            primary = [r[1] for r in db.execute(f'PRAGMA table_info({table})') if r[5]]
            where = ' AND '.join(_quote(key) + '=?' for key in primary) if primary else 'rowid=?'
            identity = tuple(patch['before'][key] for key in primary) if primary else (rowid,)
            row = db.execute(f'SELECT rowid AS _rowid_, * FROM {table} WHERE {where}', identity).fetchone()
            current = dict(row) if row is not None else None
            comparable = lambda value: {k: v for k, v in value.items() if k != '_rowid_'} if value is not None and primary else value
            if comparable(current) == comparable(desired):
                continue
            if comparable(current) != comparable(expected):
                raise ValueError('LETTER_DELETE_RESTORE_CONFLICT')
            if desired is None:
                db.execute(f'DELETE FROM {table} WHERE {where}', identity)
            elif current is None:
                if primary and db.execute(f'SELECT 1 FROM {table} WHERE rowid=?', (rowid,)).fetchone():
                    desired = {k: v for k, v in desired.items() if k != '_rowid_'}
                columns = ['rowid' if key == '_rowid_' else key for key in desired]
                db.execute(f'INSERT INTO {table} ({",".join(map(_quote, columns))}) VALUES ({",".join("?" for _ in columns)})', tuple(desired.values()))
            else:
                columns = [key for key in desired if key != '_rowid_']
                db.execute(f'UPDATE {table} SET ' + ','.join(_quote(key) + '=?' for key in columns) + f' WHERE {where}',
                           (*[desired[key] for key in columns], *identity))
        for _, statement in guards:
            db.execute(statement)


def legacy_plan(path, sources):
    """Keep the other half of an imported pair, including its export metadata."""
    patches = []
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_letters'").fetchone():
            return []
        row = db.execute('SELECT rowid AS _rowid_, * FROM legacy_letters WHERE memory_id=?', (sources.id,)).fetchone()
        if row is None:
            return []
        before = dict(row)
        metadata = json.loads(before['metadata_json'])
        metadata['user_content'] = '' if sources.part == 'user' else metadata.get('user_content', before['content'])
        metadata['reply_text'] = '' if sources.part == 'reply' else metadata.get('reply_text', '')
        if isinstance(metadata.get('backup_record'), dict):
            metadata['backup_record']['content' if sources.part == 'user' else 'reply_text'] = ''
        content = '\n\n'.join(filter(None, (metadata['user_content'], metadata['reply_text'])))
        after = {**before, 'content': content, 'content_hash': hashlib.sha256(content.encode()).hexdigest(),
                 'metadata_json': json.dumps(metadata, ensure_ascii=False, sort_keys=True)} if content else None
        patches.append({'table': 'legacy_letters', 'before': before, 'after': after})
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_letters_fts'").fetchone():
            for row in db.execute('SELECT rowid AS _rowid_, * FROM legacy_letters_fts WHERE memory_id=?', (sources.id,)):
                before = dict(row)
                patches.append({'table': 'legacy_letters_fts', 'before': before,
                                'after': {**before, 'content': content} if content else None})
    return patches


def world_plan(ledger, sources):
    """Rebuild stored relationship state without the removed evidence.

    The live projection replays stored reducer inputs in event-time order and
    de-duplicates repeated evidence, so write order and repeated wording are
    normal. The same rules are replayed here, which yields the state the ledger
    would hold had the removed events never been written; nothing is asked of a
    model and no unrelated record is reset. A set of records the replay cannot first
    reproduce exactly, or an unexplained stored change after a removed event,
    is rejected before any deletion rather than guessed.
    """
    from dataclasses import replace
    from .letter_delete_replay import TOMBSTONE, Replay
    patches = sql_plan(ledger._database_path, sources)
    with ledger._connection() as db:
        db.row_factory = sqlite3.Row
        events = [dict(row) for row in db.execute('SELECT rowid AS _rowid_,* FROM private_world_events ORDER BY rowid')]
        removed = {row['event_id'] for row in events
                   if not (sources.part == 'user' and row['event_type'] == 'canonical_reply_delivered')
                   and (sources.matches(row['delivery_id']) or prune(json.loads(row['payload_json']), sources) is None)}
        if not removed:
            return patches
        # Derived relationship events explicitly bind to the canonical delivery.
        sources.exact.update(removed)
        removed |= {row['event_id'] for row in events if prune(json.loads(row['payload_json']), sources) is None}
        # An earlier deletion leaves an inert identity tombstone; it is not removed again.
        removed -= {row['event_id'] for row in events if row['event_type'] == TOMBSTONE}
        has_inputs = db.execute("SELECT 1 FROM sqlite_master WHERE name='private_world_projection_inputs'").fetchone()
        inputs = {row['event_id']: dict(row) for row in db.execute('SELECT rowid AS _rowid_,* FROM private_world_projection_inputs')} if has_inputs else {}
        snapshots = {row['event_id']: dict(row) for row in db.execute('SELECT rowid AS _rowid_,* FROM private_world_snapshots ORDER BY version')}
        metadata = db.execute("SELECT rowid AS _rowid_,* FROM private_world_metadata WHERE key='ordered_projection_v1'").fetchone()
    replay = Replay(ledger, events, inputs, snapshots)
    replay.check_removed_opaque(removed)
    rebuilt = replay.rebuild(removed)
    for event in events:
        event_id = event['event_id']
        frozen = inputs.get(event_id)
        if event_id in removed:
            # Keep an inert identity tombstone for state-record foreign keys and
            # late delivery deduplication, without retaining its evidence.
            patches.append({'table': 'private_world_events', 'before': event,
                'after': {**event, 'event_type': TOMBSTONE, 'payload_json': '{}'}})
            if frozen:
                patches.append({'table': 'private_world_projection_inputs', 'before': frozen, 'after': None})
        elif rebuilt is not None and event_id in rebuilt.outcomes and 'result_json' in frozen:
            outcome = {**json.loads(frozen['result_json'] or '{}'), **rebuilt.outcomes[event_id]}
            after = {**frozen, 'result_json': json.dumps(outcome, sort_keys=True)}
            if after != frozen:
                patches.append({'table': 'private_world_projection_inputs', 'before': frozen, 'after': after})
        if rebuilt is not None and event_id in rebuilt.rows:
            row = snapshots[event_id]
            projected = replace(rebuilt.rows[event_id], version=row['version'])
            patches.append({'table': 'private_world_snapshots', 'before': row,
                            'after': {**row, 'payload_json': ledger._snapshot_json(projected)}})
    # Future ordered projections start at the rebuilt checkpoint; otherwise
    # their old checkpoint would silently reintroduce the removed evidence.
    if rebuilt is not None and metadata:
        before = dict(metadata)
        latest_version = max((row['version'] for row in snapshots.values()), default=1)
        base = {'cursor': max((e['_rowid_'] for e in events), default=0),
                'snapshot': ledger._snapshot_json(replace(rebuilt.final, version=latest_version))}
        patches.append({'table': 'private_world_metadata', 'before': before,
                        'after': {**before, 'value': json.dumps(base, sort_keys=True)}})
    return patches
