import json
from datetime import datetime, timezone

from runtime.personal_chat.context import chat_context


def test_current_chat_clock_does_not_reuse_old_receipt():
    import local_server
    from runtime.personal_chat.presentation import CURRENT
    adapter = object.__new__(local_server.LetterAdapter)
    adapter.recent_letters = lambda: []
    now = datetime(2026, 9, 19, 8, tzinfo=timezone.utc)
    adapter._now = lambda: now
    receipt = local_server._CURRENT_LETTER_RECEIPT.set(datetime(2026, 9, 18, 14, tzinfo=timezone.utc))
    presentation = CURRENT.set({'channel': 'qq'})
    try:
        packet = json.loads(adapter.recent_letter_fragments('现在几点')[0].text)
        assert packet['current_time'] == '2026-09-19T16:00:00+08:00'
    finally:
        CURRENT.reset(presentation)
        local_server._CURRENT_LETTER_RECEIPT.reset(receipt)


def test_world_clock_uses_generation_time_for_delayed_replies():
    import local_server
    from types import SimpleNamespace
    adapter = object.__new__(local_server.LetterAdapter)
    now = datetime(2026, 9, 19, 8, tzinfo=timezone.utc)
    observed = []
    def context(content, *, now, related_text):
        observed.append(now)
        return 'current world'
    def snapshot(stamp):
        observed.append(stamp)
        return {'rhythm': {}}
    adapter._now = lambda: now
    adapter.daily_life = SimpleNamespace(store=SimpleNamespace(reply_context=context), snapshot=snapshot)
    receipt = local_server._CURRENT_LETTER_RECEIPT.set(datetime(2026, 9, 18, 14, tzinfo=timezone.utc))
    try:
        assert adapter.daily_life_fragments('午饭吃什么', recent_fragments=())
        assert observed == [now, now]
    finally:
        local_server._CURRENT_LETTER_RECEIPT.reset(receipt)


def test_cross_night_and_cross_channel_keep_contiguous_tail_and_separate_history():
    rows = []
    for index in range(20):
        stamp = f'2026-09-{15 if index < 10 else 16}T{10 + index % 10:02}:00:00+00:00'
        rows.append(dict(letter_id=str(index), reply_revision=1, channel='qq' if index % 2 else 'wechat',
                         reply_mode='future_im', delivery_status='DELIVERED', letter_status='COMPLETED',
                         created_at=index, life_received_at=stamp, private_world_occurred_at=stamp,
                         content='摸鱼' if index in {0, 19} else f'话题{index}', reply_text='回复' * 40))
    recent, history = chat_context(rows, query='摸鱼', now=datetime(2026, 9, 16, tzinfo=timezone.utc))
    packet = json.loads(recent)
    ids = [int(r['source_id'].split(':')[1]) for r in packet['letters']]
    assert ids == list(range(ids[0], 20))
    assert all(r['received_at'].endswith('+08:00') and r['sent_at'] is None for r in packet['letters'])
    assert {r['channel'] for r in packet['letters']} == {'qq', 'wechat'}
    assert '昨晚与今早不是连说两次' in packet['meaning']
    assert len(recent) + len(history) <= 6000
    if history:
        assert not ({r['source_id'] for r in packet['letters']} &
                    {r['source_id'] for r in json.loads(history)['letters']})


def test_consumer_failure_does_not_skip_other_state_updates(monkeypatch):
    import asyncio
    from runtime.personal_chat import backend
    calls = []
    async def fail(*args):
        calls.append('world')
        raise RuntimeError('WORLD_UNAVAILABLE')
    async def life(*args):
        calls.append('life')
    async def memory(*args):
        calls.append('memory')
    monkeypatch.setattr(backend, '_commit_world', fail)
    monkeypatch.setattr(backend, '_commit_life', life)
    monkeypatch.setattr(backend, '_commit_memory', memory)
    import pytest
    with pytest.raises(RuntimeError, match='WORLD_UNAVAILABLE'):
        asyncio.run(backend.commit(None, {'delivery_status': 'DELIVERED'}))
    assert calls == ['world', 'life', 'memory']


def test_platform_time_survives_service_and_actual_context_adapter():
    import asyncio
    import local_server
    from runtime.personal_chat.events import owner_message
    from runtime.personal_chat.service import PersonalChatService
    from runtime.personal_chat.presentation import CURRENT
    event = owner_message('qq', {'post_type': 'message', 'message_type': 'private',
        'self_id': 'bot', 'user_id': 'owner', 'message_id': 'q1', 'time': 1789488000,
        'message': [{'type': 'text', 'data': {'text': '昨晚摸鱼'}}]}, account_id='bot', owner_id='owner')
    rows = []
    async def generate(*args): return '今天去上课'
    async def noop(*args): pass
    service = PersonalChatService(rows, lambda: None, generate, noop, {'qq': ('bot', 'owner')})
    asyncio.run(service.handle(event, noop))
    assert rows[0]['user_sent_at'] == event.sent_at
    adapter = object.__new__(local_server.LetterAdapter)
    adapter.recent_letters = lambda: rows
    adapter._now = lambda: datetime.now(timezone.utc)
    token = CURRENT.set({'channel': 'qq'})
    try:
        fragments = adapter.recent_letter_fragments('今天摸鱼')
        assert fragments[0].fragment_id == 'chat.recent'
        assert json.loads(fragments[0].text)['letters'][0]['sent_at'].endswith('+08:00')
    finally:
        CURRENT.reset(token)
