from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from runtime.personal_chat.engagement import Engagement, adapt_profile, effective_profile
from runtime.personal_chat.initiative_profile import _BASE, _with_caution, unanswered_wait
from runtime.personal_chat.contact_activity import cross_channel_reason

NOW = datetime(2026, 10, 11, 4, tzinfo=timezone.utc).timestamp()


def messages(count, days=1):
    return sorted([dict(letter_id=str(i), channel='qq', content='synthetic',
        created_at=NOW - (i % days) * 86400 - 1, source_messages={str(i): 'synthetic'},
        delivery_status='DELIVERED') for i in range(count)], key=lambda r: r['created_at'])


@pytest.mark.parametrize('count,days,rank', [(4,1,0),(5,1,1),(19,3,1),(20,2,1),(20,3,2),
    (59,5,2),(60,4,2),(60,5,3),(99,1,1),(100,1,4),(559,7,3),(560,7,4)])
def test_engagement_thresholds(count, days, rank):
    assert Engagement().activity([], messages(count, days), NOW)[0] == rank


def test_user_only_merge_dedup_and_time_boundaries():
    rows = messages(4)
    rows.append(dict(rows[-1], source_messages={str(i): 'synthetic' for i in range(4)}, superseded_by='merged'))
    rows += [dict(rows[0], origin='proactive', source_messages={'self': 'x'}),
             dict(rows[0], channel='wechat', source_messages={'wechat': 'x'}),
             dict(rows[0], created_at=NOW+1, source_messages={'future':'x'})]
    old = dict(letter_id='old', content='synthetic', created_at=NOW-7*86400)
    assert Engagement().activity([old], rows, NOW)[0] == 0
    letters = [dict(letter_id='new', content='synthetic', created_at=NOW-1), old]
    assert Engagement().activity(letters, rows, NOW)[0] == 1


def test_beijing_days_and_usual_hour():
    rows = messages(20, 3)
    cache = Engagement()
    assert cache.activity([], rows, NOW) == (2, False)  # rows at 11:59:59
    assert cache.activity([], rows, NOW+3600-1) == (2, False)
    assert cache.activity([], rows, NOW-1) == (2, True)
    # UTC dates differ but both timestamps lie in the same Beijing day.
    rows = messages(20)
    midnight = datetime(2026,10,11,0,tzinfo=timezone.utc).timestamp()
    for i, row in enumerate(rows):
        row['created_at'] = midnight - 1 if i % 2 else midnight + 1
    assert cache.activity([], rows, NOW)[0] == 1


@pytest.mark.parametrize('relation', list(_BASE))
@pytest.mark.parametrize('rank', range(5))
@pytest.mark.parametrize('usual', [True, False])
def test_frequency_max_and_tone_cap_without_permission_changes(relation, rank, usual):
    relationship = _BASE[relation]
    before = deepcopy(relationship)
    result = adapt_profile(relationship, rank, usual)
    assert result.rank == max(relationship.rank, max(0, rank - (not usual)))
    assert result.tone_rank == max(relationship.rank, min(rank, 2))
    assert relationship == before
    if relationship.rank <= 2:
        assert '想念' not in result.reason_floor


def test_intense_caution_unanswered_and_decay():
    p = adapt_profile(_with_caution(_BASE['reserved'], 'high'), 4, True)
    assert p.tier == 'committed' and p.im_interval_min == 600 and p.im_attempt_limit == 69
    assert unanswered_wait(p, None, 1) == 900 * 1.8
    assert unanswered_wait(p, None, 3) > unanswered_wait(p, None, 2)
    cache, rows = Engagement(), messages(100)
    assert cache.activity([], rows, NOW)[0] == 4
    assert cache.activity([], rows, NOW+86400)[0] == 1
    assert cache.activity([], rows, NOW+7*86400)[0] == 0


def server(letters=None, chats=None):
    return SimpleNamespace(store=SimpleNamespace(letters=letters or [], personal_chats=chats or []))


def test_snapshot_never_mutated():
    snapshot = SimpleNamespace(relationship_stage='unknown', tension=0, familiarity=0, trust=0, comfort=0, closeness=0)
    s = server(chats=messages(100, 1))
    s.private_world_port = SimpleNamespace(snapshot=lambda: snapshot)
    before = deepcopy(vars(snapshot))
    assert effective_profile(s, NOW-1).tier == 'close'  # no repeated habitual hour yet
    assert vars(snapshot) == before


def test_intense_frequency_does_not_offer_affection_or_claim_committed_to_model():
    from runtime.reply.proactive_letters import make_context
    from runtime.reply.proactive_runtime import packet
    p = adapt_profile(_BASE['reserved'], 4, True)
    world = {'rhythm': {'availability':'open'}, 'stale': False, 'current': {
        'source_id':'life:synthetic', 'occurred_at':datetime.fromtimestamp(NOW-60, timezone.utc).isoformat()}}
    context = make_context([], now=NOW, world=world, profile=p)
    assert context['initiative_profile']['tier'] == 'committed'
    assert not context['relationship_high']
    assert 'affection_checkin' not in [c['kind'] for c in context['candidates']]
    value = packet(channel='qq', now=datetime.fromtimestamp(NOW, timezone.utc), profile=p,
        world={}, rhythm={}, emotion={}, messages=[], opportunities=[], rows=[], available_media=['text'], hard_gates={})
    assert value['relationship']['tier'] == 'trusted'


def test_history_cost_independent_of_old_history():
    class Rows(list):
        reads = 0
        def __getitem__(self, key):
            self.reads += 1
            return super().__getitem__(key)
        def __iter__(self):
            raise AssertionError('full history iteration')
    old = dict(created_at=NOW-30*86400, content='old', channel='qq')
    for size in (100, 100000):
        rows = Rows([old] * size + messages(60,5))
        letters = Rows([dict(letter_id='new', content='x', created_at=NOW-1)] + [old] * size)
        cache = Engagement()
        cache.activity(letters, rows, NOW)
        first = rows.reads + letters.reads
        cache.activity(letters, rows, NOW+15)
        assert first < 75 and rows.reads + letters.reads - first < 12


@pytest.mark.parametrize('channel', ['letter','qq'])
def test_cross_channel_recent_pending_composing_and_cooldown(channel):
    s = server(chats=messages(1))
    p = adapt_profile(_BASE['reserved'], 4, True)
    reason = 'in_flight' if channel == 'letter' else 'user_turn_open'
    assert cross_channel_reason(s, channel=channel, now=NOW, profile=p) == reason
    s.store.personal_chats[0]['created_at'] = NOW-601
    assert cross_channel_reason(s, channel=channel, now=NOW, profile=p) is None
    s.store.personal_chats[0]['delivery_status'] = 'GENERATING'
    assert cross_channel_reason(s, channel=channel, now=NOW, profile=p) == reason
    s.store.personal_chats.clear()
    s._letter_composing_until = NOW+600
    assert cross_channel_reason(s, channel=channel, now=NOW, profile=p) == 'user_turn_open'
    s._letter_composing_until = NOW
    other = dict(letter_id='other', origin='proactive', created_at=NOW-1799, letter_status='COMPLETED')
    if channel == 'letter':
        other.update(channel='qq', delivery_status='DELIVERED')
    s.store.letters.append(other)
    assert cross_channel_reason(s, channel=channel, now=NOW, profile=p) == 'not_due'
    assert cross_channel_reason(s, channel=channel, now=NOW+1, profile=p) is None


def test_slow_delivered_reply_uses_delivery_time_and_excludes_own_draft():
    s = server(chats=[dict(letter_id='user', created_at=NOW-10000, delivery_status='DELIVERED',
        channel='qq', private_world_occurred_at=datetime.fromtimestamp(NOW-10, timezone.utc).isoformat()),
        dict(letter_id='draft', origin='proactive', channel='qq', created_at=NOW, delivery_status='SENDING')])
    assert cross_channel_reason(s, channel='qq', now=NOW, profile=_BASE['committed'], exclude_id='draft') == 'user_turn_open'
    s.store.personal_chats.pop(0)
    assert cross_channel_reason(s, channel='qq', now=NOW, profile=_BASE['committed'], exclude_id='draft') is None


def test_recovered_old_failed_turn_does_not_add_cross_channel_suppression():
    s = server(chats=[dict(letter_id='failed', channel='qq', created_at=NOW-3600, delivery_status='FAILED'),
        dict(letter_id='recovered', channel='qq', created_at=NOW-601, delivery_status='DELIVERED')])
    assert cross_channel_reason(s, channel='letter', now=NOW, profile=_BASE['committed']) is None


def test_candidate_and_topic_not_shared_twice():
    import json
    from runtime.reply.proactive_letters import make_context
    source = dict(letter_id='u', content='x', created_at=NOW-86400, letter_status='COMPLETED')
    context = make_context([source], now=NOW)
    candidate = context['candidates'][0]
    qq = dict(channel='qq', origin='proactive', delivery_status='DELIVERED', proactive_opportunity={
        'id': candidate['id'], 'description': json.dumps({'source_id': candidate['source_id']})})
    assert not make_context([source, qq], now=NOW)['candidates']


def test_composing_route_local_validated_and_clear(monkeypatch, tmp_path):
    import asyncio
    from tests.http.test_proactive_gate_recovery import letter_server
    s = letter_server(monkeypatch, tmp_path)
    monkeypatch.setattr(s, '_letter_composing_until', 0, raising=False)
    async def run():
        await s.route('POST','/toy/letter/composing', {'active':True},{},companion_confirmed=True)
        assert s._letter_composing_until == NOW+600
        assert s._proactive_gate_reason() == 'user_turn_open'
        await s.route('POST','/toy/letter/composing', {'active':False},{},companion_confirmed=True)
        assert s._letter_composing_until == 0
        await s.route('POST','/toy/letter/composing', {'active':True,'content':'never accepted'},{},companion_confirmed=True)
        assert s._letter_composing_until == 0
        await s.route('POST','/toy/letter/composing', {'active':True},{})
        assert s._letter_composing_until == 0
    asyncio.run(run())


@pytest.mark.parametrize('signal', ['letter', 'composing'])
def test_qq_generation_abandoned_when_other_channel_becomes_active(signal):
    import asyncio
    from runtime.personal_chat.service import PersonalChatService
    from runtime.personal_chat.events import PersonalMessage
    s, sent = server(), []
    event = PersonalMessage('qq', 'bot', 'owner', 'proactive-test', '')
    async def generate(event, row):
        if signal == 'letter':
            s.store.letters.insert(0, dict(letter_id='new-user', content='x', created_at=NOW, letter_status='PENDING'))
        else:
            s._letter_composing_until = NOW+600
        return 'synthetic reply'
    async def send(text):
        sent.append(text)
        return 'receipt'
    async def commit(row):
        raise AssertionError('discarded generation committed')
    svc = PersonalChatService(s.store.personal_chats, lambda: None, generate, commit, {'qq': ('bot','owner')})
    eligible = lambda: not cross_channel_reason(s, channel='qq', now=NOW, profile=_BASE['committed'], exclude_id=event.exchange_id)
    asyncio.run(svc.proactive(event, send, eligible))
    assert not sent
    assert s.store.personal_chats[0]['skip_reason'] == 'CONTACT_SUPERSEDED'


@pytest.mark.parametrize('signal', ['qq', 'composing'])
def test_letter_generation_abandoned_on_new_activity(tmp_path, signal):
    from tests.http.test_proactive_expression_context import SETUP
    from tests.http.test_expression_context_routes import run_isolated
    run_isolated(tmp_path, SETUP + '\nsignal = ' + repr(signal) + r'''
original = server._proactive_complete
async def complete(*args, **kwargs):
    result = await original(*args, **kwargs)
    if not kwargs['planning']:
        if signal == 'qq':
            server.store.personal_chats.append(dict(letter_id='new', channel='qq', content='new input',
                created_at=NOW.timestamp(), delivery_status='RECEIVED'))
        else:
            server._letter_composing_until = NOW.timestamp()+600
    return result
server._proactive_complete = complete
async def main():
    turn = await server._prepare_proactive_turn(intent, now=NOW)
    plan = json.loads(await server._proactive_complete(intent, planning=True, turn=turn))
    await server._publish_proactive(intent, plan, turn=turn)
    assert not any(row.get('origin') == 'proactive' and row.get('letter_status') == 'COMPLETED' for row in server.store.letters)
asyncio.run(main())
''')


def test_intense_still_obeys_quiet_hours_and_unanswered():
    from runtime.personal_chat.initiative import Initiative
    from runtime.personal_chat.engagement import BEIJING
    p = adapt_profile(_BASE['reserved'], 4, True)
    now = [datetime(2026,10,11,8,29,tzinfo=BEIJING).timestamp()]
    rows = []
    policy = Initiative(rows, clock=lambda: now[0], interval=lambda: 0, profile_provider=lambda: p)
    policy.received(None, None)
    assert not policy.ready()
    now[0] += 60
    assert policy.ready()
    rows.append(dict(origin='proactive', delivery_status='DELIVERED', created_at=now[0]-600))
    assert not policy.ready()
    now[0] += 301
    assert policy.ready()


def test_legacy_letter_can_use_qq_user_source(tmp_path):
    from tests.http.test_proactive_expression_context import SETUP
    from tests.http.test_expression_context_routes import run_isolated
    setup = SETUP.replace('server.store.letters[:] = [source]\nserver.store.personal_chats[:] = []',
        "source.update(channel='qq', delivery_status='DELIVERED')\nserver.store.letters[:] = []\nserver.store.personal_chats[:] = [source]")
    run_isolated(tmp_path, setup + r'''
async def main():
    turn = await server._prepare_proactive_turn(intent, now=NOW)
    assert turn['query'] == source['content']
    assert server._proactive_turn_current(intent, turn)
asyncio.run(main())
''')
