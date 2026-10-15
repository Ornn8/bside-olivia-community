"""Bounded relationship quotations, owned by their source turn."""
import hashlib
import uuid

KINDS = {
    'rel_disclosure:personal': 'disclosure_personal', 'rel_disclosure:vulnerable': 'disclosure_vulnerable',
    'rel_care:user_to_her': 'care_user', 'rel_care:her_to_user': 'care_her', 'rel_care:mutual': 'care_her',
    'rel_shared:plan': 'plan', 'rel_shared:kept': 'kept', 'rel_shared:created': 'created',
    'rel_affection:warm': 'affection', 'rel_affection:clear': 'affection',
    'rel_boundary:respected': 'boundary', 'rel_friction:repair': 'repair', 'rel_friction:conflict': 'conflict',
}


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS relationship_deleted_sources (source_id TEXT PRIMARY KEY)')
    db.execute('CREATE TABLE IF NOT EXISTS relationship_moments ('
        'id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, kind TEXT NOT NULL, user_quote TEXT NOT NULL, '
        'reply_quote TEXT NOT NULL, source_id TEXT UNIQUE NOT NULL REFERENCES private_world_events(delivery_id) '
        'ON DELETE CASCADE, sensitive INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS relationship_story_marks ('
        'moment_id TEXT PRIMARY KEY REFERENCES relationship_moments(id) ON DELETE CASCADE, token TEXT NOT NULL DEFAULT \'\')')
    if 'token' not in {row[1] for row in db.execute('PRAGMA table_info(relationship_story_marks)')}:
        db.execute("ALTER TABLE relationship_story_marks ADD COLUMN token TEXT NOT NULL DEFAULT ''")


def record_moment(db, source_id, at, moment):
    kind = KINDS.get(moment['moment'])
    if kind is None:
        return
    initialize(db)
    if db.execute('SELECT 1 FROM relationship_deleted_sources WHERE source_id=?', (source_id,)).fetchone():
        return None
    inserted = db.execute('INSERT OR IGNORE INTO relationship_moments VALUES (?,?,?,?,?,?,?)', (
        hashlib.sha256(source_id.encode()).hexdigest(), at, kind, moment['user_quote'][:80],
        moment['reply_quote'][:80], source_id, int(kind == 'disclosure_vulnerable')))
    return kind if inserted.rowcount else None


def moments(ledger, *, after='', limit=100, oldest=False):
    with ledger._connection() as db:
        initialize(db)
        rows = db.execute('SELECT id,occurred_at,kind,user_quote,reply_quote,source_id,sensitive,'
                          'EXISTS(SELECT 1 FROM relationship_story_marks WHERE moment_id=id),'
                          'COALESCE((SELECT token FROM relationship_story_marks WHERE moment_id=id),\'\') '
                          'FROM relationship_moments WHERE occurred_at>? ORDER BY ' +
                          ('occurred_at ASC' if oldest else 'EXISTS(SELECT 1 FROM relationship_story_marks WHERE moment_id=id) DESC,occurred_at DESC') + ' LIMIT ?',
                          (after, min(1000, max(1, limit)))).fetchall()
    return [dict(zip(('id', 'occurred_at', 'kind', 'user_quote', 'reply_quote', 'source_id', 'sensitive', 'marked', 'mark_token'), row))
            for row in rows]


def mark(ledger, moment_id, enabled):
    if not isinstance(moment_id, str) or type(enabled) is not bool:
        raise ValueError('STORY_MOMENT_INVALID')
    with ledger._connection() as db:
        initialize(db)
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM relationship_moments WHERE id=?', (moment_id,)).fetchone():
            raise ValueError('STORY_MOMENT_NOT_FOUND')
        if enabled:
            if db.execute('SELECT 1 FROM relationship_story_marks WHERE moment_id=?', (moment_id,)).fetchone():
                return
            if db.execute('SELECT COUNT(*) FROM relationship_story_marks').fetchone()[0] >= 3:
                raise ValueError('STORY_MARK_LIMIT')
            db.execute('INSERT INTO relationship_story_marks VALUES (?,?)', (moment_id, uuid.uuid4().hex))
        else:
            db.execute('DELETE FROM relationship_story_marks WHERE moment_id=?', (moment_id,))


def consume(ledger, identities):
    with ledger._connection() as db:
        initialize(db)
        db.executemany('DELETE FROM relationship_story_marks WHERE moment_id=? AND token=?',
                       (value.split(':', 1) if ':' in value else (value, '') for value in identities))


def delete_sources(ledger, source_ids):
    source_ids = tuple(source_ids)
    with ledger._connection() as db:
        initialize(db)
        db.executemany('INSERT OR IGNORE INTO relationship_deleted_sources VALUES (?)', ((source,) for source in source_ids))
        db.executemany('DELETE FROM relationship_moments WHERE source_id=?', ((source,) for source in source_ids))


def sync_messages(ledger, rows):
    import json
    identities = {row['letter_id'] for row in rows if isinstance(row.get('letter_id'), str)}
    with ledger._connection() as db:
        initialize(db)
        db.execute('CREATE TABLE IF NOT EXISTS relationship_message_sources '
                   '(message_id TEXT NOT NULL, source_id TEXT PRIMARY KEY)')
        db.executemany('INSERT OR IGNORE INTO relationship_message_sources VALUES (?,?)',
            ((row['letter_id'], row['private_world_delivery_id']) for row in rows
             if isinstance(row.get('letter_id'), str) and isinstance(row.get('private_world_delivery_id'), str)))
        missing = 'message_id NOT IN (SELECT value FROM json_each(?))'
        db.execute('INSERT OR IGNORE INTO relationship_deleted_sources SELECT source_id '
                   'FROM relationship_message_sources WHERE ' + missing, (json.dumps(sorted(identities)),))
        db.execute('DELETE FROM relationship_moments WHERE source_id IN '
                   '(SELECT source_id FROM relationship_message_sources WHERE ' + missing + ')', (json.dumps(sorted(identities)),))
        db.execute('DELETE FROM relationship_message_sources WHERE ' + missing, (json.dumps(sorted(identities)),))
