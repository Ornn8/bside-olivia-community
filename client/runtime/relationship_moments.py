"""Shared memories belong to canonical messages and disappear with their sources."""
import hashlib
import json

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
               'reply_quote TEXT NOT NULL, source_id TEXT UNIQUE NOT NULL, sensitive INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS relationship_message_sources '
               '(message_id TEXT NOT NULL, source_id TEXT PRIMARY KEY)')
    db.execute('CREATE INDEX IF NOT EXISTS relationship_moments_recent ON relationship_moments(occurred_at DESC,id)')


def record_moment(db, source_id, at, moment):
    kind = KINDS.get(moment['moment'])
    if kind is None:
        return
    initialize(db)
    if db.execute('SELECT 1 FROM relationship_deleted_sources WHERE source_id=?', (source_id,)).fetchone():
        return
    inserted = db.execute('INSERT OR IGNORE INTO relationship_moments VALUES (?,?,?,?,?,?,?)', (
        hashlib.sha256(source_id.encode()).hexdigest(), at, kind, moment['user_quote'][:80],
        moment['reply_quote'][:80], source_id, int(kind == 'disclosure_vulnerable')))
    return kind if inserted.rowcount else None


def moments(ledger, limit=5):
    with ledger._connection() as db:
        initialize(db)
        rows = db.execute('SELECT * FROM relationship_moments ORDER BY occurred_at DESC,id LIMIT ?', (limit,)).fetchall()
    return [dict(zip(('id', 'occurred_at', 'kind', 'user_quote', 'reply_quote', 'source_id', 'sensitive'), row)) for row in rows]


def sync_messages(ledger, rows):
    identities = [row['letter_id'] for row in rows if isinstance(row.get('letter_id'), str)]
    with ledger._connection() as db:
        initialize(db)
        db.executemany('INSERT OR IGNORE INTO relationship_message_sources VALUES (?,?)',
            ((row['letter_id'], row['private_world_delivery_id']) for row in rows
             if isinstance(row.get('letter_id'), str) and isinstance(row.get('private_world_delivery_id'), str)))
        missing = 'message_id NOT IN (SELECT value FROM json_each(?))'
        db.execute('INSERT OR IGNORE INTO relationship_deleted_sources SELECT source_id '
                   'FROM relationship_message_sources WHERE ' + missing, (json.dumps(identities),))
        db.execute('DELETE FROM relationship_moments WHERE source_id IN '
                   '(SELECT source_id FROM relationship_deleted_sources)')
        db.execute('DELETE FROM relationship_message_sources WHERE ' + missing, (json.dumps(identities),))


def plans(life_store):
    """Reuse the existing shared-project IDs, so completing one never closes another."""
    if life_store is None:
        return []
    with life_store._db() as db:
        rows = db.execute("SELECT payload FROM life_projects WHERE json_extract(payload,'$.kind')='shared'")
        return [item for (raw,) in rows if (item := json.loads(raw)).get('status')
                in {'planned', 'ongoing', 'paused'}]
