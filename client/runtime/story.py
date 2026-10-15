"""Opt-in chapters with durable drafts and bounded automatic generation."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import uuid
import sqlite3
from dataclasses import replace
from contextvars import ContextVar

from runtime.diary.diary import _complete as _diary_complete, local_day
from runtime.cloud_policy import get_policy
from runtime.model_routes import using, routed_model
from runtime.relationship_growth import route
from runtime.story_policy import volume_catalog

_locks = {}
REQUEST_FREE = ContextVar('story_request_free', default=None)


async def _complete(gateway, request_id, system, material, response_format, timeout, *, model):
    token = REQUEST_FREE.set(material.get('free', False))
    try:
        return await _diary_complete(gateway, request_id, system, material, response_format, timeout, model=model)
    finally:
        REQUEST_FREE.reset(token)


FORMAT = {'type': 'json_schema', 'json_schema': {'name': 'story_chapter', 'strict': True, 'schema': {
    'type': 'object', 'properties': {key: {'type': 'string'} for key in ('title', 'body', 'summary')},
    'required': ['title', 'body', 'summary'], 'additionalProperties': False}}}


def validate(value, *, prologue=False):
    if not isinstance(value, dict) or any(not isinstance(value.get(key), str) or not value[key].strip()
                                         for key in ('title', 'body', 'summary')):
        raise ValueError('STORY_OUTPUT_INVALID')
    return {key: value[key].strip()[:limit] for key, limit in (('title', 20), ('body', 1000 if prologue else 2800), ('summary', 200))}


class StoryStore:
    def __init__(self, diary):
        self.diary = diary
        with diary._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS story_chapters (
                    id TEXT PRIMARY KEY, volume INTEGER NOT NULL, number INTEGER NOT NULL,
                    title TEXT NOT NULL, body TEXT NOT NULL, summary TEXT NOT NULL,
                    created_at TEXT NOT NULL, model TEXT NOT NULL, request_id TEXT NOT NULL,
                    seen INTEGER NOT NULL DEFAULT 0, UNIQUE(volume, number));
                CREATE TABLE IF NOT EXISTS story_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS story_work (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS story_publications (id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS story_comments (chapter_id TEXT PRIMARY KEY, text TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS story_used_marks (moment_id TEXT PRIMARY KEY);
            ''')

    def settings(self):
        with self.diary._db() as db:
            values = {row['key']: json.loads(row['value']) for row in db.execute('SELECT * FROM story_settings')}
        return {'enabled': False, 'status': 'paused', **values}

    def setting(self, key, value):
        with self.diary._db() as db:
            db.execute('INSERT INTO story_settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (key, json.dumps(value)))

    def enable(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('STORY_SETTINGS_INVALID')
        self.setting('enabled', enabled)
        self.setting('status', 'ready' if enabled else 'paused')
        from runtime.diagnostics.reply_telemetry import emit
        emit('app', 'ok', relationship_story={'kind': 'story_enabled' if enabled else 'story_paused'})

    def chapters(self):
        with self.diary._db() as db:
            return [dict(row) for row in db.execute('SELECT c.*,COALESCE(m.text,\'\') AS comment FROM story_chapters c '
                'LEFT JOIN story_comments m ON c.id=m.chapter_id ORDER BY volume,number')]

    def comment(self, chapter_id, text):
        if not isinstance(text, str) or len(text) > 200:
            raise ValueError('STORY_COMMENT_INVALID')
        with self.diary._db() as db:
            if db.execute('SELECT 1 FROM story_chapters WHERE id=?', (chapter_id,)).fetchone() is None:
                raise ValueError('STORY_CHAPTER_NOT_FOUND')
            if text.strip():
                db.execute('INSERT INTO story_comments VALUES (?,?) ON CONFLICT(chapter_id) DO UPDATE SET text=excluded.text',
                           (chapter_id, text.strip()))
            else:
                db.execute('DELETE FROM story_comments WHERE chapter_id=?', (chapter_id,))

    def delete(self, chapter_id):
        with self.diary._db() as db:
            db.execute('DELETE FROM story_chapters WHERE id=?', (chapter_id,))
            db.execute('DELETE FROM story_work WHERE id=?', (chapter_id,))
            db.execute('DELETE FROM story_comments WHERE chapter_id=?', (chapter_id,))

    def seen(self, chapter_id):
        with self.diary._db() as db:
            db.execute('UPDATE story_chapters SET seen=1 WHERE id=?', (chapter_id,))

    def work(self, work_id):
        with self.diary._db() as db:
            row = db.execute('SELECT payload FROM story_work WHERE id=?', (work_id,)).fetchone()
        return json.loads(row['payload']) if row else None

    def checkpoint(self, work_id, payload):
        with self.diary._db() as db:
            db.execute('INSERT INTO story_work VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                       (work_id, json.dumps(payload, ensure_ascii=False)))

    def due(self, state, moments, now):
        if not self.settings()['enabled']:
            return False
        retry_at = self.settings().get('retry_at', '')
        if retry_at and now.isoformat() < retry_at:
            return False
        chapters = self.chapters()
        with self.diary._db() as db:
            published = [row[0] for row in db.execute('SELECT created_at FROM story_publications ORDER BY created_at')]
        if sum(datetime.fromisoformat(at) > now - timedelta(days=7) for at in published) >= 2:
            return False
        if not published:
            return True
        last = datetime.fromisoformat(published[-1])
        if now - last < timedelta(days=2):
            return False
        if not chapters:
            return False
        chapter = chapters[-1]
        if state.story_volume > chapter['volume']:
            return True
        after = chapter['created_at']
        if sum(moment['occurred_at'] > after for moment in moments) >= 2:
            return True
        days = set(state.relationship_days)
        return sum(day > local_day(last) and day in days for day in self.diary.days() if len(day) == 10) >= 5

    def publish(self, work_id, work, now, *, replace_id=None):
        with self.diary._db() as db:
            if replace_id:
                old = db.execute('SELECT volume,number FROM story_chapters WHERE id=?', (replace_id,)).fetchone()
                if old is None:
                    db.execute('DELETE FROM story_work WHERE id=?', (work_id,))
                    return False
                volume, number = old['volume'], old['number']
            else:
                volume = work['volume']
                number = db.execute('SELECT COALESCE(MAX(number),0)+1 FROM story_chapters WHERE volume=?', (volume,)).fetchone()[0]
            entry = work['entry']
            db.execute('INSERT INTO story_chapters VALUES (?,?,?,?,?,?,?,?,?,0) '
                'ON CONFLICT(id) DO UPDATE SET title=excluded.title,body=excluded.body,summary=excluded.summary,'
                'model=excluded.model,request_id=excluded.request_id,seen=0',
                (replace_id or work['request_id'], volume, number, entry['title'], entry['body'], entry['summary'],
                 now.isoformat(), work['model'], work['request_id']))
            if not replace_id:
                db.execute('INSERT OR IGNORE INTO story_publications VALUES (?,?)', (work['request_id'], now.isoformat()))
                db.executemany('INSERT OR IGNORE INTO story_used_marks VALUES (?)', ((value,) for value in work.get('marked', [])))
            db.execute('DELETE FROM story_work WHERE id=?', (work_id,))
        self.setting('status', 'written')
        self.setting('retry_at', '')
        from runtime.diagnostics.reply_telemetry import emit
        emit('app', 'ok', model=work['model'], relationship_story={
            'kind': 'chapter_written', 'billing_request_id': work['request_id']})
        return True

    def clear_used_marks(self, consume):
        with self.diary._db() as db:
            identities = [row[0] for row in db.execute('SELECT moment_id FROM story_used_marks')]
            consume(identities)
            db.executemany('DELETE FROM story_used_marks WHERE moment_id=?', ((value,) for value in identities))


def packet(store, state, moments, facts, addressing, now, *, replace_id=None, user_gender='unknown', life=()):
    if replace_id:
        moments = [{**item, 'marked': False} for item in moments]
    chapters = store.chapters()
    if replace_id:
        state = replace(state, story_volume=next(row['volume'] for row in chapters if row['id'] == replace_id))
        chapters = chapters[:next(i for i, row in enumerate(chapters) if row['id'] == replace_id)]
    previous = chapters[-1] if chapters else None
    cutoff = previous['created_at'] if previous else ''
    result = {'as_of': local_day(now), 'chapter_kind': 'prologue' if previous is None else 'opening' if previous['volume'] < state.story_volume else 'chapter',
        'her_facts': facts, 'route': route(state), 'route_rule': get_policy('relationship_story')['route_lines'][route(state)],
        'user_gender': user_gender if user_gender in {'male', 'female'} else 'unknown',
        'how_she_calls_user': addressing.get('linli_calls_user', '')[:16] or '那个人',
        'user_comments_on_last_chapter': previous.get('comment', '') if previous else '',
        'her_life_since_last': list(life),
        'volume': volume_catalog(state, now), 'relationship_stage': state.relationship_stage,
        'intimacy_allowed': [grant.tier.value for grant in state.intimacy_grants],
        'addressing': {key: text[:16] for key, text in addressing.items() if isinstance(text, str)},
        'previous': {'summary': previous['summary'], 'last_paragraph': previous['body'].split('\n')[-1][:1000]} if previous else None,
        'diary_since_last': [], 'moments_since_last': []}
    for moment in sorted(moments, key=lambda item: (not item.get('marked', False), item['occurred_at'])):
        if moment['occurred_at'] > cutoff or moment.get('marked'):
            result['moments_since_last'].append({key: moment[key] for key in
                (('kind', 'occurred_at') if moment['sensitive'] else ('kind', 'occurred_at', 'user_quote', 'reply_quote'))})
    with store.diary._db() as db:
        if previous is None:
            first = db.execute('SELECT day,title,substr(body,1,2000) AS body FROM diary_entries '
                               'WHERE length(day)=10 ORDER BY day LIMIT 1').fetchone()
            result['first_recorded_diary'] = dict(first) if first else None
        result['diary_since_last'] = [dict(row) for row in db.execute('SELECT day,title,body FROM diary_entries '
            'WHERE written_at>? ORDER BY day DESC LIMIT 30', (cutoff,))]
        if previous is None:
            result['memoirs'] = [dict(row) for row in db.execute(
                'SELECT day,title,substr(body,1,300) AS body FROM diary_entries WHERE length(day)=7 ORDER BY day DESC LIMIT 24')]
        else:
            result['memoirs'] = []
    while len(json.dumps(result, ensure_ascii=False)) > 14500:
        if result['diary_since_last']:
            result['diary_since_last'].pop()
        elif result['her_life_since_last']:
            result['her_life_since_last'].pop()
        elif result['moments_since_last']:
            result['moments_since_last'].pop()
        elif result['memoirs']:
            result['memoirs'].pop()
        else:
            raise ValueError('STORY_PACKET_TOO_LARGE')
    return result


async def write(store, gateway, state, moments, *, facts, addressing, positive_balance, now=None, replace_id=None,
                user_gender='unknown', life=(), consume_marks=None):
    now = now or datetime.now(timezone.utc)
    lock = _locks.setdefault(str(store.diary.path.resolve()), asyncio.Lock())
    if lock.locked():
        return 'busy'
    async with lock:
        if not store.settings()['enabled'] and replace_id is None:
            return 'paused'
        if replace_id is None and not store.due(state, moments, now):
            return 'skipped'
        if not await positive_balance():
            store.setting('status', 'deferred')
            store.setting('retry_at', (now + timedelta(minutes=30)).isoformat())
            from runtime.diagnostics.reply_telemetry import emit
            emit('app', 'skipped', relationship_story={'kind': 'chapter_deferred'})
            return 'deferred'
        store.setting('status', 'writing')
        work_id = replace_id or 'next'
        work = store.work(work_id)
        gateway = getattr(gateway, 'primary', gateway)
        config = gateway.config
        if work is None:
            with using('story'):
                model = routed_model(config.base_url, config.model)
            work = {'volume': state.story_volume, 'model': model, 'base_url': config.base_url,
                    'request_id': 'story:' + uuid.uuid4().hex,
                    'packet': packet(store, state, moments, facts, addressing, now, replace_id=replace_id,
                                     user_gender=user_gender, life=life), 'stage': 'pending'}
            work['marked'] = [item['id'] + ':' + item.get('mark_token', '') for item in moments if replace_id is None and item.get('marked') and any(
                material['kind'] == item['kind'] and material['occurred_at'] == item['occurred_at']
                for material in work['packet']['moments_since_last'])]
            work['free'] = replace_id is None and work['packet']['chapter_kind'] == 'prologue' and get_policy('relationship_story')['prologue_free']
            store.checkpoint(work_id, work)
        try:
            if work['base_url'] != config.base_url:
                raise ValueError('STORY_MODEL_CHANGED')
            with using('story'):
                prologue = work['packet']['chapter_kind'] == 'prologue'
                work['packet']['free'] = work.get('free', False)
                length_rule = ('本次为相遇那一天的序章，正文800–1000字；没有相遇资料时不得补造。' if prologue
                               else '本章正文2000–2800字。')
                if work['stage'] == 'pending':
                    work['entry'] = validate(await _complete(gateway, work['request_id'], get_policy('relationship_story')['writer'] + '\n' + length_rule,
                        work['packet'], FORMAT, 240, model=work['model']), prologue=prologue)
                    work['stage'] = 'draft'
                    store.checkpoint(work_id, work)
                if work['stage'] == 'draft':
                    work['stage'] = 'revising'
                    store.checkpoint(work_id, work)
                    try:
                        work['entry'] = validate(await _complete(gateway, work['request_id'].replace('story:', 'story-revise:'),
                            get_policy('relationship_story')['editor'] + '\n' + length_rule,
                            {'material': work['packet'], 'draft': work['entry'], 'free': work.get('free', False)},
                            FORMAT, 240, model=work['model']), prologue=prologue)
                    except Exception:
                        pass
                if not store.settings()['enabled'] and replace_id is None:
                    work['stage'] = 'final'
                    store.checkpoint(work_id, work)
                    return 'paused'
                if not store.publish(work_id, work, now, replace_id=replace_id):
                    return 'skipped'
                if consume_marks:
                    try:
                        store.clear_used_marks(consume_marks)
                    except (OSError, sqlite3.Error):
                        pass
                return 'written'
        except Exception:
            store.setting('status', 'failed')
            store.setting('retry_at', (now + timedelta(minutes=30)).isoformat())
            from runtime.diagnostics.reply_telemetry import emit
            emit('app', 'failed', relationship_story={'kind': 'chapter_failed', 'billing_request_id': work['request_id']})
            raise
