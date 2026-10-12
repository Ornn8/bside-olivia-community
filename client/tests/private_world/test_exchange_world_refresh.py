import asyncio
import pytest
from datetime import datetime, timedelta, timezone

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime


def test_new_statement_reconsiders_fresh_world_once_without_certifying_reply(tmp_path, monkeypatch):
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: None)
    monkeypatch.setattr('runtime.private_world.daily_life_runtime.configured_duties', lambda: None)
    now = datetime(2026, 9, 27, 5, tzinfo=timezone.utc)
    store = DailyLifeStore(tmp_path / 'life.db')
    store.publish_day('day:old', {'location': '家里', 'activity': '休息', 'note': '在家里休息。'}, [],
                      occurred_at=now - timedelta(minutes=5), activity_kind='rest')
    store.record_exchange('reply:latest', '去吃饭吧', '现在往厨房走。', [],
                          occurred_at=now, current_quote='现在往厨房走。')
    with store._db() as db:
        db.execute('INSERT INTO life_exchange_world_gate VALUES (?,?,?)', ('reply:latest', 'reconsider', '现在往厨房走。'))
    assert store.snapshot(now)['current']['activity'] == '休息'
    assert not store.snapshot(now)['stale']
    calls = []
    runtime = DailyLifeRuntime(store, lambda: None, lambda: '')

    async def complete(prompt, data, request_id, **kwargs):
        calls.append(data)
        return {'activity': {'kind': 'meal', 'place_id': 'home', 'focus': ''},
                'meal': {'slot': 'lunch', 'food': '面条', 'status': 'eating'}, 'project': None}

    monkeypatch.setattr(runtime, '_complete', complete)
    asyncio.run(runtime.refresh(now))
    assert runtime.error_code is None
    assert calls[0]['exchange_actions'][0]['evidence_kind'] == 'character_statement'
    assert store.snapshot(now)['current']['activity'] == '正在吃午饭'
    asyncio.run(runtime.refresh(now + timedelta(seconds=1)))
    assert len(calls) == 1


def test_exchange_actions_excludes_future_old_and_non_action_chat(tmp_path):
    now = datetime(2026, 9, 27, 5, tzinfo=timezone.utc)
    store = DailyLifeStore(tmp_path / 'life.db')
    for name, stamp in [('old', now-timedelta(hours=7)), ('future', now+timedelta(minutes=1)), ('new', now)]:
        store.record_exchange('reply:'+name, 'PRIVATE', '现在往厨房走。', [],
                              occurred_at=stamp, current_quote='现在往厨房走。')
        with store._db() as db:
            db.execute('INSERT INTO life_exchange_world_gate VALUES (?,?,?)', ('reply:'+name, 'reconsider', '现在往厨房走。'))
    store.record_exchange('reply:chat', 'PRIVATE', '谢谢。', [], occurred_at=now)
    result = store.pending_exchange_actions(now)
    assert [a['source_id'] for a in result] == ['reply:new']
    assert 'PRIVATE' not in str(result)
    assert not store.pending_exchange_actions(now, after=now)


class CheckPort:
    """Answers the per-exchange world check; records what it was asked."""

    def __init__(self, change='none'):
        self.change, self.calls = change, []

    async def ask(self, state, questions, *, purpose):
        self.calls.append((state, questions, purpose))
        return {key: self.change if key == 'change' else 'none' for key in questions}


@pytest.mark.parametrize(('change', 'decision'), [('none', 'none'), ('outside', 'reconsider')])
def test_delivered_exchange_check_is_one_request_durable_and_recovers_existing_source(tmp_path, monkeypatch, change, decision):
    now = datetime.now(timezone.utc)
    store = DailyLifeStore(tmp_path / 'life.db')
    # A delivery recorded before its check ran: replaying it asks once, then never again.
    store.record_exchange('reply:existing', '去吃饭吧', '我这就去。', [], occurred_at=now)
    port, scheduled = CheckPort(change), []
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: port)
    runtime = DailyLifeRuntime(store, lambda: None, lambda: '')
    monkeypatch.setattr(runtime, 'schedule_refresh', scheduled.append)
    for _ in range(2):
        assert not asyncio.run(runtime.consume_exchange('reply:existing', '去吃饭吧', '我这就去。', occurred_at=now))
    assert len(port.calls) == 1
    state, questions, purpose = port.calls[0]
    assert purpose == 'exchange-world-check'
    # Only the exchange's own words and the world as it stands; no persona, no journal.
    assert set(state) == {'contract', 'exchange', 'current', 'meals_today', 'projects', 'quotes', 'quote_contract'}
    assert state['exchange']['user_text'] == '去吃饭吧' and state['exchange']['reply_text'] == '我这就去。'
    # Without a laid-out day only a plain no, an agreed meal or a full decision can follow.
    assert set(questions['change']['criteria']) == {'none', 'meal', 'outside'}
    assert {key for key in questions['meal']['criteria'] if key != 'none'} <= {'agreed_breakfast', 'agreed_lunch', 'agreed_dinner'}
    assert bool(scheduled) == (decision == 'reconsider')
    actions = DailyLifeStore(store.path).pending_exchange_actions(now)
    assert bool(actions) == (decision == 'reconsider')
    if actions:
        assert actions[0]['reply_text'] == '我这就去。'
    with store._db() as db:
        assert db.execute('SELECT decision FROM life_exchange_world_gate').fetchone()[0] == decision


def test_a_check_that_cannot_be_answered_changes_nothing_and_does_not_lose_the_exchange(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    store = DailyLifeStore(tmp_path / 'life.db')
    store.record_exchange('reply:existing', '去吃饭吧', '我这就去。', [], occurred_at=now)
    outcome = []

    class Port:
        async def ask(self, *args, **kwargs):
            outcome.append(1)
            raise RuntimeError('JEV_UNAVAILABLE')

    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: Port())
    runtime = DailyLifeRuntime(store, lambda: None, lambda: '')
    scheduled = []
    monkeypatch.setattr(runtime, 'schedule_refresh', scheduled.append)
    assert asyncio.run(runtime.consume_exchange('reply:existing', '去吃饭吧', '我这就去。', occurred_at=now)) is False
    assert runtime.world_check_error_code == 'JEV_UNAVAILABLE'
    assert not scheduled and not store.pending_exchange_actions(now)
    with store._db() as db:
        assert db.execute('SELECT count(*) FROM life_exchange_world_gate').fetchone()[0] == 0
    # Nothing was marked checked, so a later replay asks again.
    asyncio.run(runtime.consume_exchange('reply:existing', '去吃饭吧', '我这就去。', occurred_at=now))
    assert len(outcome) == 2


def test_a_proactive_message_is_checked_too_with_no_user_words(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    store = DailyLifeStore(tmp_path / 'life.db')
    port = CheckPort()
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: port)
    runtime = DailyLifeRuntime(store, lambda: None, lambda: '')
    store.record_exchange('reply:p:1', '', '我想你了。', [], occurred_at=now, origin='proactive')
    asyncio.run(runtime.consume_exchange('reply:p:1', '', '我想你了。', occurred_at=now, origin='proactive'))
    assert [call[0]['exchange']['origin'] for call in port.calls] == ['proactive']
