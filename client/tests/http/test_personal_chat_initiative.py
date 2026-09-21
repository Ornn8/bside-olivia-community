import asyncio
from datetime import datetime
from types import SimpleNamespace
from runtime.private_world.life_rhythm import LOCAL

from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.initiative import Initiative, letter_invitation_allowed
from runtime.personal_chat.initiative_profile import profile_from_snapshot, profile_from_rows
from runtime.personal_chat.presentation import parse_social, parse
from runtime.personal_chat.service import PersonalChatService


def test_unconfirmed_followup_is_not_scheduled_again():
    now = datetime(2026, 9, 19, 12, tzinfo=LOCAL).timestamp()
    rows = [dict(letter_id='owner-request', delivery_status='DELIVERED', created_at=now-60,
                 followup_at=now-1),
            dict(letter_id='attempt', origin='proactive', delivery_status='DELIVERY_UNCONFIRMED',
                 followup_source_id='owner-request', created_at=now-1)]
    policy = Initiative(rows, clock=lambda: now, interval=lambda: 1)
    policy.received(PersonalMessage('wechat', 'bot', 'owner', '1', 'hi'), None)
    assert policy.pending_followup() is None
    policy.due = now - 1
    assert not policy.ready()


def test_relationship_profile_uses_existing_state_not_a_new_score():
    reserved = profile_from_snapshot(SimpleNamespace(
        familiarity=10, trust=10, comfort=10, closeness=0, tension=0,
        relationship_stage='acquaintance'))
    trusted = profile_from_snapshot(SimpleNamespace(
        familiarity=75, trust=78, comfort=50, closeness=40, tension=0,
        relationship_stage='familiar'))
    close = profile_from_snapshot(SimpleNamespace(
        familiarity=80, trust=82, comfort=76, closeness=55, tension=0,
        relationship_stage='familiar'))
    committed = profile_from_snapshot(SimpleNamespace(
        familiarity=80, trust=82, comfort=76, closeness=75, tension=0,
        relationship_stage='committed'))
    assert [reserved.tier, trusted.tier, close.tier, committed.tier] == [
        'reserved', 'trusted', 'close', 'committed']
    assert reserved.im_interval_min > trusted.im_interval_min > close.im_interval_min > committed.im_interval_min
    assert reserved.im_attempt_limit < trusted.im_attempt_limit < close.im_attempt_limit <= committed.im_attempt_limit
    tense = profile_from_snapshot(SimpleNamespace(
        familiarity=80, trust=82, comfort=76, closeness=75, tension=80,
        relationship_stage='committed'))
    assert tense.tier == 'committed' and tense.caution == 'high'
    assert tense.im_interval_min > committed.im_interval_min


def test_persisted_profile_falls_back_for_pre_profile_history():
    assert profile_from_rows([]).tier == 'reserved'
    assert profile_from_rows([{'created_at': 1, 'contact_qualification': False}]).tier == 'familiar'
    assert profile_from_rows([{'created_at': 1, 'contact_qualification': True}]).tier == 'close'
    assert profile_from_rows([{'created_at': 2, 'initiative_tier': 'trusted',
                               'initiative_caution': 'elevated'}]).tier == 'trusted'


def test_cadence_rearms_once_when_committed_relationship_changes(monkeypatch):
    base = datetime(2026, 9, 13, 12, tzinfo=LOCAL).timestamp()
    now = [base]
    rows = []
    monkeypatch.setattr('runtime.personal_chat.initiative.random.uniform', lambda low, high: low)
    policy = Initiative(rows, clock=lambda: now[0])
    policy.received(PersonalMessage('qq', 'bot', 'owner', '1', 'hi'), None)
    assert policy.due == base + 6 * 3600  # reserved at receipt time
    rows.append(dict(origin='user', delivery_status='DELIVERED', created_at=base,
                     initiative_tier='committed', initiative_caution='normal'))
    now[0] += 3600
    assert not policy.ready()
    assert policy.due == now[0] + 15 * 60  # re-armed from the committed profile, not the old 6h cadence
    due = policy.due
    now[0] += 16 * 60
    assert policy.ready()
    assert policy.due == due  # stable profile does not keep moving the deadline on every poll


def test_cadence_shared_unanswered_budget_and_no_startup_backlog():
    now = [datetime(2026, 9, 13, 12, tzinfo=LOCAL).timestamp()]
    rows = []
    policy = Initiative(rows, clock=lambda: now[0], interval=lambda: 1200)
    now[0] += 86400
    assert not policy.ready()  # No live owner channel after restart.
    event = PersonalMessage('qq', 'bot', 'owner', '1', 'hi')
    policy.received(event, None)
    assert not policy.ready()
    now[0] += 1201
    assert policy.ready()
    rows.extend([dict(origin='proactive', channel=channel, delivery_status='DELIVERED', created_at=now[0])
                 for channel in ('qq', 'wechat')])
    assert not policy.ready()  # Unanswered contact first increases do-not-disturb pressure.
    rows.append(dict(delivery_status='DELIVERED', content='回来啦', origin='user', created_at=now[0],
                     initiative_tier='close', initiative_caution='normal'))
    assert policy.ready()  # A user reply ends the unanswered streak; close profile has room to initiate again.
    rows.extend(dict(origin='proactive', delivery_status='SKIPPED', created_at=now[0]) for _ in range(10))
    assert not policy.ready()  # Skipped model calls also consume the bounded relationship-specific budget.


def test_unanswered_pressure_decays_faster_for_close_relationships():
    base = datetime(2026, 9, 13, 12, tzinfo=LOCAL).timestamp()

    def ready_after(tier, hours):
        now = [base + hours * 3600]
        rows = [
            dict(origin='user', delivery_status='DELIVERED', created_at=base - 3600,
                 initiative_tier=tier, initiative_caution='normal'),
            dict(origin='proactive', delivery_status='DELIVERED', created_at=base),
        ]
        policy = Initiative(rows, clock=lambda: now[0], interval=lambda: 0)
        policy.received(PersonalMessage('qq', 'bot', 'owner', tier, ''), None)
        return policy.ready()

    assert not ready_after('reserved', 4)
    assert ready_after('committed', 4)
    assert not ready_after('close', 2)
    assert ready_after('close', 4)


def test_sleep_and_explicit_pause():
    now = [datetime(2026, 9, 13, 7, tzinfo=LOCAL).timestamp()]
    rows = []
    policy = Initiative(rows, clock=lambda: now[0], interval=lambda: 0)
    policy.received(None, None)
    assert not policy.ready()
    now[0] = datetime(2026, 9, 13, 8, 30, tzinfo=LOCAL).timestamp()
    assert policy.ready()
    rows.append(dict(delivery_status='DELIVERED', initiative_preference='pause'))
    assert not policy.ready()
    rows.append(dict(delivery_status='DELIVERED', initiative_preference='open'))
    assert policy.ready()
    rows.append(dict(delivery_status='DELIVERED', presentation_status='METADATA_MISSING'))
    assert policy.ready()
    assert letter_invitation_allowed(rows, [], now[0])
    rows.append(dict(delivery_status='DELIVERED'))
    assert policy.ready()  # A missing marker is not a permanent user refusal.


def test_letter_invitation_shared_history_and_decline():
    now = 1800000000
    assert letter_invitation_allowed([], [], now)
    assert not letter_invitation_allowed([], [{'created_at': now - 1, 'content': '信'}], now)
    rows = [dict(channel='qq', delivery_status='DELIVERED', letter_invitation=True, created_at=now - 1)]
    assert not letter_invitation_allowed(rows, [], now)
    assert letter_invitation_allowed(rows, [], now + 8 * 86400)
    rows.append(dict(channel='wechat', delivery_status='DELIVERED', letter_preference='pause'))
    assert not letter_invitation_allowed(rows, [], now + 8 * 86400)
    row = {}
    assert parse_social('有空写封信给我吧。[[letter:invite]][[initiative:pause]]', row, True) == '有空写封信给我吧。'
    assert row == {'letter_invitation': True, 'initiative_preference': 'pause'}
    row = {}
    raw = '等你忙完。[[chat:text|text_only|pause|pause|no]]'
    assert parse_social(raw, row, False) == '等你忙完。'
    assert row == {'letter_invitation': False, 'initiative_preference': 'pause', 'letter_preference': 'pause'}
    assert parse(raw) == ('等你忙完。', 'text', 'voice_ok')


def test_proactive_skip_never_sends_or_commits_and_delivery_has_no_fake_user():
    async def run():
        rows, sent, commits = [], [], []
        async def generate(event, row):
            return '[[skip]]' if event.message_id == 'skip' else '刚弹完琴，想和你说两句。'
        async def send(text):
            sent.append(text)
        async def commit(row):
            commits.append(dict(row))
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('bot', 'owner')})
        await service.proactive(PersonalMessage('qq', 'bot', 'owner', 'skip', ''), send)
        assert not sent and not commits
        await service.proactive(PersonalMessage('qq', 'bot', 'owner', 'send', ''), send)
        assert len(sent) == len(commits) == 1
        assert commits[0]['origin'] == 'proactive'
        assert commits[0]['content'] == '' and commits[0]['source_messages'] == {}
        await service.recover()
        assert len(sent) == 1
        await service.proactive(PersonalMessage('qq', 'bot', 'owner', 'stale', ''), send, lambda: False)
        assert len(rows) == 2
    asyncio.run(run())
