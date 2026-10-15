from datetime import datetime, timedelta, timezone

from runtime.private_world.port import PrivateWorldSnapshot
from runtime.private_world.reducer import ReducerEvent, ReducerEventKind, reduce_private_world
from runtime.diary.diary import DiaryStore
from runtime.story import StoryStore, packet
import pytest


def advance(state, at, **signals):
    return reduce_private_world(state, ReducerEvent(
        kind=ReducerEventKind.RELATIONSHIP_EVIDENCE, occurred_at=at,
        semantic_key='turn', relationship_signals=signals)).snapshot


def test_everyday_cannot_starve_deep():
    at = datetime(2026, 10, 15, tzinfo=timezone.utc)
    state = PrivateWorldSnapshot()
    for i in range(30):
        state = advance(state, at + timedelta(minutes=i), rel_disclosure='everyday')
    assert (state.familiarity, state.comfort) == (3, 3)
    state = advance(state, at + timedelta(hours=1), rel_disclosure='vulnerable')
    assert (state.trust, state.closeness) == (2, 1)


def test_deep_daily_and_rolling_caps():
    at = datetime(2026, 10, 15, tzinfo=timezone.utc)
    state = PrivateWorldSnapshot()
    for day in range(3):
        for minute in range(10):
            state = advance(state, at + timedelta(days=day, minutes=minute), rel_disclosure='vulnerable')
        assert state.trust == min((day + 1) * 4, 10)
    state = advance(state, at + timedelta(days=7), rel_disclosure='vulnerable')
    assert state.trust == 12


def test_volume_floor():
    state = PrivateWorldSnapshot(trust=35, comfort=35, familiarity=35, story_volume=4)
    state = advance(state, datetime(2026, 10, 15, tzinfo=timezone.utc), rel_friction='conflict')
    assert state.trust == 35
    assert state.comfort == 35


def test_story_requires_opt_in_and_success_alone_consumes_number(tmp_path):
    store = StoryStore(DiaryStore(tmp_path / 'diary.db'))
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    state = PrivateWorldSnapshot()
    assert not store.due(state, [], now)
    store.enable(True)
    assert store.due(state, [], now)
    store.setting('status', 'deferred')
    assert store.chapters() == []
    entry = {'title': '序章', 'body': '窗前。', 'summary': '初识。'}
    store.publish('next', {'volume': 1, 'entry': entry, 'model': 'gemini-4-argon', 'request_id': 'story:first'}, now)
    assert not store.due(state, [{'occurred_at': (now + timedelta(hours=1)).isoformat()}] * 2, now + timedelta(days=1))
    assert store.due(state, [{'occurred_at': (now + timedelta(hours=1)).isoformat()}] * 2, now + timedelta(days=2))
    store.publish('next', {'volume': 1, 'entry': entry, 'model': 'gemini-4-argon', 'request_id': 'story:second'}, now + timedelta(days=2))
    assert [row['number'] for row in store.chapters()] == [1, 2]
    assert not store.due(PrivateWorldSnapshot(story_volume=2), [], now + timedelta(days=4))


def test_sensitive_moments_are_abstract_in_writer_packet(tmp_path):
    store = StoryStore(DiaryStore(tmp_path / 'diary.db'))
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    value = packet(store, PrivateWorldSnapshot(), [{
        'kind': 'disclosure_vulnerable', 'occurred_at': now.isoformat(), 'sensitive': True,
        'user_quote': 'secret-user', 'reply_quote': 'secret-reply'}], 'persona', {}, now)
    assert value['moments_since_last'] == [{'kind': 'disclosure_vulnerable', 'occurred_at': now.isoformat()}]


def test_route_migration_and_change_preserve_grants(tmp_path):
    from runtime.relationship_growth import route
    from runtime.relationship_story import change_route
    from runtime.private_world.ledger import SQLitePrivateWorldLedger
    assert route(PrivateWorldSnapshot(relationship_stage='committed')) == 'romantic'
    target = SQLitePrivateWorldLedger(tmp_path / 'world.db')
    state = change_route(target, 'friend', target.snapshot().version)
    assert route(target.snapshot()) == 'friend'
    assert state.intimacy_grants == ()
    state = change_route(target, 'romantic', state.version)
    assert state.relationship_stage == 'unknown'


def test_evidence_persists_moments_and_reconfirmation_is_not_an_event(tmp_path):
    import hashlib
    from runtime.private_world.ledger import SQLitePrivateWorldLedger
    from runtime.memory.private_world_delivery import DeliveryEvent, PrivateWorldDeliveryCommitter
    from runtime.memory.private_world_relationship import PrivateWorldRelationshipCommitter
    from runtime.relationship_moments import moments, delete_sources
    target = SQLitePrivateWorldLedger(tmp_path / 'world.db')
    at = datetime(2026, 10, 15, tzinfo=timezone.utc)
    user, reply = '合成用户原句。', '合成角色原句。'
    signal = {'kind': 'relationship_evidence', 'signals': {'rel_disclosure': 'vulnerable'},
        'moment': 'rel_disclosure:vulnerable', 'user_quote': user, 'reply_quote': reply, 'relationship_stage': 'committed'}
    committer = PrivateWorldRelationshipCommitter(target)
    for i in range(2):
        identity = f'chat:{i}'
        when = at + timedelta(days=i)
        PrivateWorldDeliveryCommitter(target).commit(DeliveryEvent(delivery_id=identity, occurred_at=when,
            semantic_key=identity, canonical_reply_sha256=hashlib.sha256(reply.encode()).hexdigest()))
        assert committer.commit_exchange(identity, user, reply, signal, occurred_at=when).value == 'COMMITTED'
    assert len([event for event in target.events() if event.event_type == 'stage_confirmed']) == 1
    assert len(moments(target)) == 2
    assert moments(target)[0]['sensitive'] == 1
    assert user not in str([event.payload for event in target.events()])
    delete_sources(target, ['chat:0'])
    assert len(moments(target)) == 1


def test_friend_route_rejects_romantic_confirmation(tmp_path):
    import hashlib
    from runtime.private_world.ledger import SQLitePrivateWorldLedger
    from runtime.memory.private_world_delivery import DeliveryEvent, PrivateWorldDeliveryCommitter
    from runtime.memory.private_world_relationship import PrivateWorldRelationshipCommitter
    from runtime.relationship_story import change_route
    target = SQLitePrivateWorldLedger(tmp_path / 'world.db')
    change_route(target, 'friend', 1)
    at = datetime(2026, 10, 15, tzinfo=timezone.utc)
    user, reply = '合成用户原句。', '合成角色原句。'
    PrivateWorldDeliveryCommitter(target).commit(DeliveryEvent(delivery_id='chat:1', occurred_at=at,
        semantic_key='chat:1', canonical_reply_sha256=hashlib.sha256(reply.encode()).hexdigest()))
    PrivateWorldRelationshipCommitter(target).commit_exchange('chat:1', user, reply,
        {'kind': 'relationship_evidence', 'signals': {'rel_affection': 'clear'}, 'moment': 'rel_affection:clear',
         'user_quote': user, 'reply_quote': reply, 'relationship_stage': 'committed'}, occurred_at=at)
    assert target.snapshot().relationship_stage == 'unknown'


def test_paid_story_defers_without_calls_and_revision_failure_keeps_draft(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import runtime.story as story
    from original_client_relay_api import RELAY_BASE
    from runtime import model_routes
    model_routes.configure(tmp_path)
    store = StoryStore(DiaryStore(tmp_path / 'diary.db'))
    store.enable(True)
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    gateway = SimpleNamespace(config=SimpleNamespace(base_url=RELAY_BASE, model='claude-sonnet-5-5'))
    calls = []
    async def complete(gateway, request_id, system, packet, response_format, timeout, *, model):
        calls.append((request_id, model))
        if request_id.startswith('story-revise:'):
            raise RuntimeError('synthetic editor failure')
        return {'title': '窗前', 'body': '她望向窗外。', 'summary': '初识。'}
    monkeypatch.setattr(story, '_complete', complete)
    async def balance_no():
        return False
    async def balance_yes():
        return True
    async def scenario():
        args = dict(facts=['合成角色事实'], addressing={}, now=now)
        assert await story.write(store, gateway, PrivateWorldSnapshot(), [], positive_balance=balance_no, **args) == 'deferred'
        assert not calls and not store.chapters()
        args['now'] += timedelta(hours=1)
        assert await story.write(store, gateway, PrivateWorldSnapshot(), [], positive_balance=balance_yes, **args) == 'written'
        assert store.chapters()[0]['body'] == '她望向窗外。'
        assert store.chapters()[0]['number'] == 1
        assert [model for _, model in calls] == ['gemini-4-argon'] * 2
        assert await story.write(store, gateway, PrivateWorldSnapshot(), [], positive_balance=balance_yes, **args) == 'skipped'
        assert len(calls) == 2
    try:
        asyncio.run(scenario())
    finally:
        model_routes._path, model_routes._cache = None, (None, {})


@pytest.mark.parametrize(('stage', 'volume', 'chosen'), [('familiar', 2, 'undecided'),
    ('friend', 3, 'undecided'), ('trusted_friend', 4, 'undecided'), ('close', 5, 'romantic'), ('committed', 5, 'romantic')])
def test_migration_is_durable_and_idempotent(tmp_path, stage, volume, chosen):
    from runtime.private_world.ledger import SQLitePrivateWorldLedger, LedgerEvent
    from runtime.relationship_story import migrate
    target = SQLitePrivateWorldLedger(tmp_path / 'world.db')
    target.apply_once(LedgerEvent('seed', 'seed', 'seed', {}, '2026-10-15T00:00:00+00:00'),
                      PrivateWorldSnapshot(version=2, relationship_stage=stage))
    state = migrate(target)
    assert (state.story_volume, state.relationship_route) == (volume, chosen)
    assert migrate(target) == state
    assert SQLitePrivateWorldLedger(tmp_path / 'world.db').snapshot() == state


def test_new_evidence_replay_deduplicates_late_identical_turn(tmp_path):
    import hashlib
    from runtime.private_world.ledger import SQLitePrivateWorldLedger
    from runtime.memory.private_world_delivery import DeliveryEvent, PrivateWorldDeliveryCommitter
    from runtime.memory.private_world_relationship import PrivateWorldRelationshipCommitter
    at = datetime(2026, 10, 15, tzinfo=timezone.utc)
    def run(name, sequence):
        target = SQLitePrivateWorldLedger(tmp_path / name)
        for index in sequence:
            identity, when = f'chat:{index}', at + timedelta(minutes=index)
            PrivateWorldDeliveryCommitter(target).commit(DeliveryEvent(delivery_id=identity, occurred_at=when,
                semantic_key=identity, canonical_reply_sha256=hashlib.sha256('reply'.encode()).hexdigest()))
            PrivateWorldRelationshipCommitter(target).commit_exchange(identity, 'user', 'reply',
                {'kind':'relationship_evidence','signals':{'rel_disclosure':'vulnerable'},
                 'moment':'rel_disclosure:vulnerable','user_quote':'user','reply_quote':'reply'}, occurred_at=when)
        return target.snapshot()
    ordered, late = run('ordered.db', [0, 1]), run('late.db', [1, 0])
    assert ordered.trust == late.trust == 2
    assert ordered.relationship_growth == late.relationship_growth


def test_marks_limit_reset_and_comments(tmp_path):
    from runtime.private_world.ledger import SQLitePrivateWorldLedger, LedgerEvent
    from runtime.relationship_moments import record_moment, moments, mark
    from private_world_admin import PrivateWorldAdmin
    target = SQLitePrivateWorldLedger(tmp_path / 'world.db')
    at = datetime(2026, 10, 15, tzinfo=timezone.utc)
    for index in range(4):
        identity = f'source:{index}'
        target.apply_once(LedgerEvent(identity, identity, 'source', {}, at.isoformat()), target.snapshot())
        with target._connection() as db:
            record_moment(db, identity, at.isoformat(), {'moment':'rel_shared:plan','user_quote':'约定','reply_quote':'答应'})
    items = moments(target)
    for item in items[:3]:
        mark(target, item['id'], True)
    mark(target, items[0]['id'], True)
    with pytest.raises(ValueError, match='STORY_MARK_LIMIT'):
        mark(target, items[3]['id'], True)
    mark(target, items[0]['id'], False)
    mark(target, items[3]['id'], True)
    store = StoryStore(DiaryStore(tmp_path / 'diary.db'))
    store.publish('next', {'volume':1,'entry':{'title':'初识','body':'窗前。','summary':'初识'},
                          'model':'synthetic','request_id':'story:first'}, at)
    store.comment('story:first', '请多写一点琴房。')
    value = packet(store, PrivateWorldSnapshot(), moments(target), ['人物事实'], {}, at + timedelta(days=3))
    assert value['user_comments_on_last_chapter'] == '请多写一点琴房。'
    assert value['moments_since_last'] and 'persona' not in value
    store.comment('story:first', '')
    assert store.chapters()[0]['comment'] == ''
    with pytest.raises(ValueError, match='STORY_COMMENT_INVALID'):
        store.comment('story:first', '字' * 201)
    PrivateWorldAdmin(tmp_path / 'world.db').reset(confirmed=True)
    assert moments(target) == []
    with target._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM relationship_story_marks').fetchone()[0] == 0


def test_consuming_an_older_selection_preserves_a_new_selection(tmp_path):
    from runtime.private_world.ledger import SQLitePrivateWorldLedger, LedgerEvent
    from runtime.relationship_moments import record_moment, moments, mark, consume
    target = SQLitePrivateWorldLedger(tmp_path / 'world.db')
    at = '2026-10-15T00:00:00+00:00'
    target.apply_once(LedgerEvent('turn', 'turn', 'source', {}, at), target.snapshot())
    with target._connection() as db:
        record_moment(db, 'turn', at, {'moment':'rel_shared:plan','user_quote':'约定','reply_quote':'答应'})
    identity = moments(target)[0]['id']
    mark(target, identity, True)
    selected = moments(target)[0]
    mark(target, identity, False)
    mark(target, identity, True)
    consume(target, [identity + ':' + selected['mark_token']])
    assert moments(target)[0]['marked']
    current = moments(target)[0]
    consume(target, [identity + ':' + current['mark_token']])
    assert not moments(target)[0]['marked']


def test_story_facts_gender_and_output_limits():
    from pathlib import Path
    from types import SimpleNamespace
    from runtime.relationship_story import character_facts, gender_from_facts
    from runtime.story import validate
    facts = character_facts(Path(__file__).resolve().parents[1] / 'linli_character/persona_release_v2.json')
    assert any('上海音乐学院' in text for text in facts)
    assert all('回复' not in text and '前缀' not in text for text in facts)
    assert gender_from_facts([SimpleNamespace(text='用户是女生。', metadata={})]) == 'female'
    assert gender_from_facts([SimpleNamespace(text='用户的朋友是女生。', metadata={})]) == 'unknown'
    assert gender_from_facts([SimpleNamespace(text='用户是女生。', metadata={'user_gender':'male'})]) == 'unknown'
    output = {'title':'章','body':'字' * 4000,'summary':'概述'}
    assert len(validate(output)['body']) == 2800
    assert len(validate(output, prologue=True)['body']) == 1000


@pytest.mark.parametrize(('value','expected'), [('0.00000000',False),('1.25000000',True),(0,False),
    (1,True),(True,False),('NaN',False),('Infinity',False),('bad',False),(None,False)])
def test_story_balance_accepts_decimal_quota(value, expected):
    from runtime.relationship_story import has_balance
    assert has_balance(value) is expected
