"""Client composition for relationship details and novel chapters."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
import uuid
import re
import asyncio
from decimal import Decimal, InvalidOperation

from runtime.cloud_policy import get_policy
from runtime.relationship_growth import route, volume_updates, DIMENSIONS
from runtime.relationship_moments import moments
from runtime.story_policy import ROUTES, volume_catalog


def ledger(server):
    committer = server.private_world_relationship_committer
    if committer is None:
        raise RuntimeError('RELATIONSHIP_UNAVAILABLE')
    migrate(committer.ledger)
    return committer.ledger


def migrate(target):
    from runtime.private_world.ledger import LedgerEvent, LedgerVersionConflictError
    from runtime.private_world.port import PrivateWorldSnapshot
    state = target.snapshot()
    if state.relationship_route or state == PrivateWorldSnapshot():
        return state
    changes = volume_updates(state, {}, len(state.relationship_days))
    for field, floor, minimum in (('familiarity', 1, 2), ('familiarity', 35, 3), ('comfort', 35, 3), ('trust', 35, 4)):
        if changes['story_volume'] >= minimum:
            changes[field] = max(floor, getattr(state, field))
    identity = 'relationship-migrate.' + str(state.version)
    try:
        target.apply_once(LedgerEvent(identity, identity, 'relationship_migration', {}, datetime.now(timezone.utc).isoformat()),
            replace(state, version=state.version + 1, **changes), expected_snapshot_version=state.version)
    except LedgerVersionConflictError:
        pass
    return target.snapshot()


def addressing(server, now):
    life = getattr(server.daily_life_runtime, 'store', None)
    if life is None:
        return {}
    return {key: values[0]['quote'][:16] for key, values in life.addressing_profile(now=now, per_kind=1).items() if values}


def change_route(target, chosen, version, now=None):
    from runtime.private_world.ledger import LedgerEvent
    if not isinstance(chosen, str) or chosen not in ROUTES or type(version) is not int:
        raise ValueError('RELATIONSHIP_ROUTE_INVALID')
    state = target.snapshot()
    if version != state.version:
        raise ValueError('RELATIONSHIP_VERSION_CONFLICT')
    now = now or datetime.now(timezone.utc)
    changes = volume_updates(state, {'relationship_route': chosen}, len(state.relationship_days))
    next_state = replace(state, version=state.version + 1, **changes)
    identity = 'route.' + uuid.uuid4().hex
    target.apply_once(LedgerEvent(identity, identity, 'relationship_route', {'route': chosen}, now.isoformat()),
                      next_state, expected_snapshot_version=version)
    return next_state


def writer_line(state, target=None):
    if target is not None and not state.relationship_route:
        state = migrate(target)
    texts = get_policy('relationship_story')
    line = texts['route_lines'][route(state)]
    if (target is not None and route(state) == 'undecided' and state.story_volume == 4
            and not state.route_asked and len(state.relationship_days) - state.volume_started_days >= 7):
        from runtime.private_world.ledger import LedgerEvent
        identity = 'route-ask.' + uuid.uuid4().hex
        from runtime.private_world.ledger import LedgerWriteError
        try:
            target.apply_once(LedgerEvent(identity, identity, 'relationship_route_ask', {}, datetime.now(timezone.utc).isoformat()),
                replace(state, version=state.version + 1, route_asked=True), expected_snapshot_version=state.version)
            line += '\n' + texts['route_ask']
        except LedgerWriteError:
            pass
    return line


def character_facts(path):
    from persona_loader import load_persona
    loaded = load_persona(path)
    if loaded.snapshot.status != 'READY':
        raise ValueError('STORY_FACTS_UNAVAILABLE')
    allowed = {'anchor.residence', 'anchor.school_timeline', 'anchor.reading', 'anchor.grandmother_piano',
               'anchor.hua', 'anchor.bilibili', 'anchor.physical', 'anchor.everyday_taste', 'anchor.listening_shelf'}
    result = [item.statement[:600] for item in loaded.snapshot.declarations if item.declaration_id in allowed][:12]
    for item in loaded.snapshot.declarations:
        if item.declaration_id == 'identity.linli_name':
            result.insert(0, item.statement.split('；')[0][:100])
        if item.declaration_id == 'anchor.current_piece':
            result.append('初始练习资料，实际进展以生活记录为准：' + item.statement[:500])
    return result


def gender_from_facts(records):
    genders = set()
    for record in records:
        value = getattr(record, 'metadata', {}).get('user_gender')
        if value in {'male', 'female'}:
            genders.add(value)
        text = getattr(record, 'text', '')
        for word, gender in (('男性', 'male'), ('男生', 'male'), ('女性', 'female'), ('女生', 'female')):
            if re.fullmatch(r'(?:用户(?:的性别)?(?:是|为|[:：])?|性别[:：])\s*' + word + r'[。.]?', text.strip()):
                genders.add(gender)
    return next(iter(genders)) if len(genders) == 1 else 'unknown'


async def story_material(server, stories, now, *, replace_id=None):
    facts = character_facts(server.letters_adapter.persona_v2_path)
    try:
        records = await asyncio.to_thread(server.conversation_memory_adapter.list_memories,
                                          user_id=server._memory_config.user_id, limit=1000)
        gender = gender_from_facts(records)
    except Exception:
        gender = 'unknown'
    chapters = stories.chapters()
    if replace_id:
        chapters = chapters[:next(i for i, row in enumerate(chapters) if row['id'] == replace_id)]
    cutoff = chapters[-1]['created_at'] if chapters else ''
    life_store = getattr(server.daily_life_runtime, 'store', None)
    life = []
    if life_store is not None:
        with life_store._db() as db:
            for at, raw in db.execute("SELECT occurred_at,payload FROM life_moments WHERE kind='daily' "
                                     'AND occurred_at>? ORDER BY occurred_at DESC LIMIT 30', (cutoff,)):
                value = json.loads(raw)
                life.append({'date': at[:10], **{key: str(value.get(key) or '')[:250]
                                                for key in ('location', 'activity', 'note')}})
            projects = db.execute("SELECT payload FROM life_projects WHERE json_extract(payload,'$.kind')='linli' LIMIT 10")
            for (raw,) in projects:
                value = json.loads(raw)
                life.append({key: str(value.get(key) or '')[:160] for key in ('title', 'status')})
    return facts, gender, life


def details(server, now=None):
    from runtime.memory.effective_relationship import level
    now = now or datetime.now(timezone.utc)
    target = ledger(server)
    state = target.snapshot()
    texts = get_policy('relationship_story')
    recent = moments(target)
    week = (now - timedelta(days=7)).date().isoformat()
    rising = {row[2] for row in state.relationship_growth if row[0] >= week and row[3] > 0}
    def public(item):
        return {'id': item['id'], 'marked': bool(item['marked']), 'date': item['occurred_at'][:10], 'text': texts['moments'][item['kind']],
                'quote': '' if item['sensitive'] else item['user_quote'][:40]}
    open_plans = []
    life = getattr(server.daily_life_runtime, 'store', None)
    rows = [*server.store.letters, *server.store.personal_chats]
    for moment in recent:
        if moment['kind'] != 'plan':
            continue
        source = next((row for row in rows if row.get('private_world_delivery_id') == moment['source_id']), None)
        completed = False
        if source and life is not None:
            source_id = f"reply:{source['letter_id']}:{source.get('reply_revision', 1)}"
            with life._db() as db:
                record = db.execute('SELECT payload FROM life_moments WHERE source_id=?', (source_id,)).fetchone()
                updates = [item for item in json.loads(record[0]).get('updates', []) if item.get('kind') == 'shared'] if record else []
                matches = updates if len(updates) == 1 else [item for item in updates if any(
                    quote and (quote in item['quote'] or item['quote'] in quote)
                    for quote in (moment['user_quote'], moment['reply_quote']))]
                states = [db.execute('SELECT payload FROM life_projects WHERE id=?', (item['id'],)).fetchone() for item in matches]
                completed = bool(states) and all(row and json.loads(row[0]).get('status') in {'completed', 'cancelled'} for row in states)
        if not completed:
            open_plans.append(public(moment))
    return {'version': state.version, 'route': route(state), 'routes': ROUTES,
        'volume': state.story_volume, **volume_catalog(state, now),
        'dimensions': {key: {'text': texts['dimensions'][key][level(state, key)], 'rising': key in rising,
                             'rising_text': texts['rising'][key]}
                       for key in DIMENSIONS}, 'addressing': addressing(server, now),
        'moments': [public(item) for item in sorted(recent, key=lambda row: row['occurred_at'], reverse=True)[:5]],
        'pending_moments': [public(item) for item in recent if item['marked']],
        'plans': open_plans[:5]}


async def tick(server, now=None, *, replace_id=None):
    from runtime.story import StoryStore, write
    from original_client_relay_api import relay_request, RELAY_BASE
    now = now or datetime.now(timezone.utc)
    if server.diary_store is None:
        return 'unavailable'
    target = ledger(server)
    gateway = getattr(server.letters_adapter.gateway, 'primary', server.letters_adapter.gateway)
    async def balance():
        config = gateway.config
        if config.base_url.rstrip('/') != RELAY_BASE:
            return False
        value = await relay_request(RELAY_BASE, os.environ.get(config.api_key_env, ''), 'GET', '/quota')
        return has_balance(value.get('remaining_yuan'))
    stories = StoryStore(server.diary_store)
    from runtime.relationship_moments import consume
    stories.clear_used_marks(lambda ids: consume(target, ids))
    if replace_id is None and (not stories.settings()['enabled'] or not stories.due(target.snapshot(), moments(target, limit=1000), now)):
        return 'skipped'
    try:
        facts, gender, life = await story_material(server, stories, now, replace_id=replace_id)
        material = moments(target, limit=1000)
        if not stories.chapters() or replace_id == stories.chapters()[0]['id']:
            material = list({item['id']: item for item in [*material, *moments(target, limit=5, oldest=True)]}.values())
        return await write(stories, gateway, target.snapshot(), material,
            facts=facts, user_gender=gender, life=life, addressing=addressing(server, now),
            positive_balance=balance, now=now, replace_id=replace_id, consume_marks=lambda ids: consume(target, ids))
    except Exception:
        stories.setting('status', 'failed')
        stories.setting('retry_at', (now + timedelta(minutes=30)).isoformat())
        server._safe_log('story_write_failed')
        return 'failed'


def has_balance(value):
    if type(value) not in (int, float, str):
        return False
    try:
        amount = Decimal(str(value))
        return amount.is_finite() and amount > 0
    except InvalidOperation:
        return False


def page(server):
    from runtime.story import StoryStore
    from runtime.model_routes import load
    store = StoryStore(server.diary_store)
    chapters = store.chapters()
    target = ledger(server)
    state = target.snapshot()
    now = datetime.now(timezone.utc)
    with target._connection() as db:
        db.execute('CREATE TABLE IF NOT EXISTS relationship_volumes (volume INTEGER PRIMARY KEY, opened_at TEXT NOT NULL)')
        opened = {number: datetime.fromisoformat(at) for number, at in db.execute('SELECT volume,opened_at FROM relationship_volumes')}
    return {**store.settings(), 'model': load()['story'] or 'gemini-4-argon',
        'unseen': sum(not chapter['seen'] for chapter in chapters), 'current_volume': state.story_volume,
        'volumes': [{'volume': number, **volume_catalog(replace(state, story_volume=number), opened.get(number, now)),
                     'chapters': [chapter for chapter in chapters if chapter['volume'] == number]}
                    for number in sorted({state.story_volume, *(chapter['volume'] for chapter in chapters)})]}
