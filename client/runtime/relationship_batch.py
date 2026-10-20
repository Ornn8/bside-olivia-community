"""Durable, background-only relationship judgments: fifty exchanges or one day."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from runtime.relationship_growth import CHOICES, validate_signals

from .relationship_contract import MODEL, PROMPT, validate_result


_BATCH_ERROR_CODES = frozenset({
    'invalid_api_key', 'invalid_request', 'request_too_large', 'idempotency_conflict',
    'request_already_submitted', 'relationship_background_busy', 'rate_limited',
    'pending_requests_limit', 'insufficient_quota', 'insufficient_balance',
    'model_not_priced', 'model_requires_money_account', 'account_tiers_unavailable',
    'relationship_upstream_unavailable', 'relationship_batch_unavailable',
})


class BatchRequestError(RuntimeError):
    def __init__(self, http_status, error_code):
        super().__init__('RELATIONSHIP_BATCH_UNAVAILABLE')
        self.http_status = http_status
        self.error_code = error_code if error_code in _BATCH_ERROR_CODES else 'RELATIONSHIP_BATCH_UNAVAILABLE'


class BatchQueue:
    def __init__(self, path):
        self.path = Path(path)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS relationship_queue ('
                       'id TEXT PRIMARY KEY, message_id TEXT NOT NULL, payload TEXT NOT NULL, '
                       'queued_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT \'pending\')')
            db.execute('CREATE TABLE IF NOT EXISTS relationship_batch_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def sync(self, rows, now):
        ids = [r['letter_id'] for r in rows if isinstance(r.get('letter_id'), str)]
        with self.db() as db:
            deleted = db.execute('DELETE FROM relationship_queue WHERE message_id NOT IN (SELECT value FROM json_each(?))', (json.dumps(ids),))
            if deleted.rowcount:
                db.execute("DELETE FROM relationship_batch_state WHERE key LIKE 'relationship-batch:%'")
            for row in rows:
                if (not row.get('private_world_delivery_id') or not row.get('content') or not row.get('reply_text')
                        or row.get('private_world_status') not in {None, 'COMMITTED'}
                        or row.get('origin') == 'proactive' or not row.get('private_world_occurred_at')
                        or row.get('relationship_status') in {'COMMITTED', 'DUPLICATE'}):
                    continue
                value = {'id': row['private_world_delivery_id'], 'user': row['content'],
                         'reply': row['reply_text'], 'at': row['private_world_occurred_at']}
                db.execute('INSERT OR IGNORE INTO relationship_queue(id,message_id,payload,queued_at) VALUES (?,?,?,?)',
                           (value['id'], row['letter_id'], json.dumps(value, ensure_ascii=False), now.isoformat()))

    def due(self, now):
        with self.db() as db:
            retry = db.execute("SELECT value FROM relationship_batch_state WHERE key='retry_at'").fetchone()
            if retry and now < datetime.fromisoformat(retry[0]):
                return False
            count, oldest = db.execute("SELECT COUNT(*),MIN(queued_at) FROM relationship_queue WHERE status='pending'").fetchone()
        return count >= 50 or bool(oldest and now >= datetime.fromisoformat(oldest) + timedelta(days=1))

    def pending(self, now):
        with self.db() as db:
            rows = db.execute("SELECT payload FROM relationship_queue WHERE status='pending' "
                              "ORDER BY julianday(json_extract(payload,'$.at')),id LIMIT 50").fetchall()
        turns, size = [], 0
        for (raw,) in rows:
            # Whole exchanges only; a very long letter gets its own batch.
            if turns and size + len(raw) > 100000:
                break
            turns.append(json.loads(raw))
            size += len(raw)
        digest = hashlib.sha256(json.dumps(turns, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return {'request_id': 'relationship-batch:' + digest, 'turns': turns}

    def result(self, request_id):
        with self.db() as db:
            row = db.execute('SELECT value FROM relationship_batch_state WHERE key=?', (request_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_result(self, request_id, value):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO relationship_batch_state VALUES (?,?)', (request_id, json.dumps(value)))

    def finish(self, ids, request_id):
        with self.db() as db:
            db.executemany("UPDATE relationship_queue SET status='done',payload='{}' WHERE id=?", ((i,) for i in ids))
            db.execute("DELETE FROM relationship_batch_state WHERE key IN (?, 'retry_at')", (request_id,))

    def defer(self, now):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO relationship_batch_state VALUES (?,?)',
                       ('retry_at', (now + timedelta(hours=1)).isoformat()))




async def request_batch(gateway, batch, chosen):
    """A dedicated relay endpoint rejects pre-installer servers; no supplier fallback."""
    import os
    from aiohttp import ClientSession, ClientTimeout, ClientError
    from original_client_relay_api import RELAY_BASE
    from runtime import ui_language
    from runtime.tls import client_tls_context
    gateway = getattr(gateway, 'primary', gateway)
    config = gateway.config
    if config.base_url.rstrip('/') != RELAY_BASE:
        raise RuntimeError('RELATIONSHIP_RELAY_REQUIRED')
    from runtime.cloud_policy import get_policy
    packet = {**batch, 'route': chosen, 'criteria': get_policy('relationship_criteria')}
    async with ClientSession(timeout=ClientTimeout(total=240), trust_env=False) as session:
        async with session.post(RELAY_BASE + '/relationship-batch', json=packet,
            headers={'Authorization': 'Bearer ' + os.environ.get(config.api_key_env, ''), **ui_language.headers()},
            ssl=client_tls_context(), allow_redirects=False) as response:
            if response.status != 200:
                code = None
                try:
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(4096):
                        body.extend(chunk)
                        if len(body) > 4096:
                            break
                    if len(body) <= 4096:
                        payload = json.loads(body)
                        error = payload.get('error') if isinstance(payload, dict) else None
                        if isinstance(error, dict) and isinstance(error.get('code'), str):
                            code = error['code']
                except (ValueError, ClientError, OSError, asyncio.TimeoutError):
                    pass
                raise BatchRequestError(response.status, code)
            raw = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                raw.extend(chunk)
                if len(raw) > 256000:
                    raise ValueError('RELATIONSHIP_BATCH_INVALID')
            return json.loads(raw)


async def tick(server, now=None, request=request_batch):
    from runtime.relationship_lite import ledger
    from runtime.relationship_moments import sync_messages
    now = now or datetime.now(timezone.utc)
    target = ledger(server)
    rows = [*server.store.letters, *server.store.personal_chats]
    def prepare():
        queue = BatchQueue(target._database_path)
        queue.sync(rows, now)
        sync_messages(target, rows)
        return queue, queue.pending(now) if queue.due(now) else None
    queue, batch = await asyncio.to_thread(prepare)
    # Terms of address are judged on their own daily cadence from the same loop. Neither request affects the other.
    await _judge_nicknames(server, rows, now)
    if batch is None:
        return 'waiting'
    return await _submit(server, queue, batch, target, request, now)


async def _judge_nicknames(server, rows, now):
    """How the two address each other, read from the newest exchanges at most once a day; never raises."""
    life = getattr(getattr(server, 'daily_life_runtime', None), 'store', None)
    adapter = getattr(server, 'letters_adapter', None)
    if life is None or adapter is None:
        return
    try:
        from runtime.private_world.addressing_judgment import run_daily
        if await run_daily(life, rows, adapter.gateway, now) == 'unavailable':
            server._safe_log('addressing_judgment_deferred')
    except Exception:
        try:
            server._safe_log('addressing_judgment_deferred')
        except Exception:
            pass


async def _submit(server, queue, batch, target, request, now):
    from runtime.relationship_growth import route
    try:
        result = queue.result(batch['request_id'])
        if result is None:
            result = await request(server.letters_adapter.gateway, batch, route(target.snapshot()))
            validate_result(result, batch['turns'])
            queue.save_result(batch['request_id'], result)
        judgments = {row['id']: row for row in validate_result(result, batch['turns'])}
        # The user can delete messages while the network request is in flight.
        current = {r.get('private_world_delivery_id') for r in [*server.store.letters, *server.store.personal_chats]}
        def commit():
            for turn in batch['turns']:
                if turn['id'] not in current:
                    continue
                row = judgments[turn['id']]
                at = datetime.fromisoformat(turn['at'])
                if row['moment'] is None:
                    status = server.private_world_relationship_committer.commit_interaction_day(turn['id'], turn['reply'], occurred_at=at)
                else:
                    signal = {k: row[k] for k in ('signals', 'moment', 'user_quote', 'reply_quote')}
                    status = server.private_world_relationship_committer.commit_exchange(turn['id'], turn['user'], turn['reply'],
                        {'kind': 'relationship_evidence', **signal}, occurred_at=at)
                if status.value not in {'COMMITTED', 'DUPLICATE'}:
                    raise RuntimeError('RELATIONSHIP_BATCH_COMMIT_FAILED')
            queue.finish([t['id'] for t in batch['turns']], batch['request_id'])
        await asyncio.to_thread(commit)
        await asyncio.to_thread(server._refresh_contact_relationship_projection)
        return 'updated'
    except Exception as exc:
        queue.defer(now)
        status = exc.http_status if isinstance(exc, BatchRequestError) else 0
        code = exc.error_code if isinstance(exc, BatchRequestError) else 'RELATIONSHIP_BATCH_UNAVAILABLE'
        server._safe_log('relationship_batch_deferred', http_status=status,
                         status=f'HTTP_{status}' if status else 'DEFERRED', error_code=code)
        return 'deferred'
