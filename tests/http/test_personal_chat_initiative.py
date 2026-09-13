import asyncio
from datetime import datetime

from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.initiative import Initiative, letter_invitation_allowed
from runtime.personal_chat.presentation import parse_social, parse
from runtime.personal_chat.service import PersonalChatService


def test_cadence_shared_unanswered_budget_and_no_startup_backlog():
    now = [datetime(2026, 9, 13, 12).timestamp()]
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
    assert not policy.ready()
    rows.append(dict(delivery_status='DELIVERED', content='回来啦'))
    assert policy.ready()
    rows.extend(dict(origin='proactive', delivery_status='SKIPPED', created_at=now[0]) for _ in range(10))
    assert not policy.ready()  # Skipped model calls also consume budget.


def test_sleep_and_explicit_pause():
    now = [datetime(2026, 9, 13, 7).timestamp()]
    rows = []
    policy = Initiative(rows, clock=lambda: now[0], interval=lambda: 0)
    policy.received(None, None)
    assert not policy.ready()
    now[0] = datetime(2026, 9, 13, 8, 30).timestamp()
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
    assert parse(raw) == ('等你忙完。', 'text', 'text_only')


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
