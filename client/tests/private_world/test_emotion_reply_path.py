"""A reply appraises its own input; older unappraised sources never ride along on it."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from runtime.memory.received_user_originals import ReceivedOriginal
from runtime.private_world import character_emotion_runtime as runtime_module
from runtime.private_world import jev_emotion
from runtime.private_world.character_emotion_runtime import CharacterEmotionRuntime
from runtime.private_world.daily_life import DailyLifeStore
from runtime.reply import jev_questions


NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


def receipt(key, text='今天有点累，想早点休息。', when=NOW - timedelta(minutes=1)):
    return ReceivedOriginal('received-user:qq:' + key, text, when, ('reply:' + key + ':1',), 'qq', key)


class Port:
    """A decision service double that answers every question with the first neutral choice."""

    def __init__(self, fail=lambda ids: False):
        self.calls, self.fail = [], fail

    async def ask(self, state, questions, purpose=None):
        ids = [item['source']['source_id'] for item in state.get('sources', {}).values()]
        wire = json.dumps(dict(state=state, questions=questions, purpose=purpose), ensure_ascii=False, separators=(',', ':'))
        self.calls.append(dict(purpose=purpose, ids=ids, bytes=len(wire.encode()), mood='current_affect' in state))
        if purpose == 'character-emotion' and self.fail(ids):
            raise ValueError('JEV_UNAVAILABLE')
        answers = {}
        for key, question in questions.items():
            criteria = question.get('criteria') or {}
            if key.startswith('affect_') or purpose == 'character-current-affect':
                answers[key] = {'label': 'unknown', 'intensity': 'low', 'reason': 'unknown'}.get(key.removeprefix('affect_'), 'none')
            else:
                answers[key] = 'none' if 'none' in criteria else next(iter(criteria))
        return answers


@pytest.fixture
def make(tmp_path, monkeypatch):
    def build(port, name='life'):
        monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
        life = DailyLifeStore(tmp_path / f'{name}.sqlite3')
        return CharacterEmotionRuntime(life, lambda: SimpleNamespace(), lambda: '[]')
    return build


def reply(runtime, *receipts, now=NOW):
    return asyncio.run(_reply(runtime, receipts, now))


async def _reply(runtime, receipts, now):
    view = await runtime.evaluate_received(list(receipts), now=now)
    if runtime._background is not None:
        await runtime._background  # let the background pass finish inside the same loop
    return view


def backlog(runtime, size, *, age=timedelta(days=1)):
    for index in range(size):
        runtime.store.receive(f'old-{index:03d}', f'很早以前的第{index}句话', occurred_at=NOW - age - timedelta(minutes=index))


def test_reply_request_is_the_same_size_whatever_backlog_waits(make):
    sizes = {}
    for backlog_size in (0, 6, 60):
        port = Port()
        runtime = make(port, f'life-{backlog_size}')
        backlog(runtime, backlog_size)
        view = reply(runtime, receipt('now'))
        first = port.calls[0]
        assert first['purpose'] == 'character-emotion' and first['ids'] == ['received-user:qq:now']
        assert 'pending_current_input' not in view  # the reply's own input is appraised
        assert view['reactions'] == []
        sizes[backlog_size] = first['bytes']
        # The waiting sources are taken afterwards, the oldest few per pass, without the present mood.
        background = [call for call in port.calls[1:] if call['purpose'] == 'character-emotion']
        assert len(background) == (1 if backlog_size else 0)
        if backlog_size:
            limit = runtime_module._BACKGROUND_LIMIT
            assert len(background[0]['ids']) == min(backlog_size, limit)
            assert not background[0]['mood'] and all(key.startswith('old-') for key in background[0]['ids'])
            left = runtime.store.pending_source_ids(before=NOW, limit=33)
            assert left[:1] == ([f'old-{backlog_size - limit - 1:03d}'] if backlog_size > limit else [])
    # The request does not grow with what is waiting (a longer row number is a few digits).
    assert max(sizes.values()) - min(sizes.values()) <= 8


def test_pending_from_the_last_reaction_window_shares_the_reply_request(make):
    port = Port()
    runtime = make(port)
    runtime.store.receive('earlier', '刚才那条消息没处理上', occurred_at=NOW - timedelta(hours=2))
    backlog(runtime, 3)
    reply(runtime, receipt('now'))
    first = port.calls[0]
    assert first['ids'] == ['earlier', 'received-user:qq:now']  # one request, as before: no second appraisal
    assert first['mood']
    assert all(call['purpose'] != 'character-emotion' or call['ids'][0].startswith('old-') for call in port.calls[1:])


def test_failed_background_pass_never_reaches_the_reply_or_the_stored_error_state(make, monkeypatch):
    clock = {'now': 1000.0}
    monkeypatch.setattr(runtime_module, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    port = Port(fail=lambda ids: bool(ids) and ids[0].startswith('old-'))
    runtime = make(port)
    backlog(runtime, 3)
    view = reply(runtime, receipt('first'))
    assert runtime.background_error is not None
    assert runtime.error_code is None and 'pending_current_input' not in view and 'last_evaluation_error' not in view
    assert view['reactions'] == [] or view['reactions'][-1]['source_id'] == 'received-user:qq:first'
    calls = len([c for c in port.calls if c['ids'] and c['ids'][0].startswith('old-')])
    assert calls == 1
    # Rate limited: the next reply, soon after, starts no pass. Later, the persisted retry pacing still holds it back.
    reply(runtime, receipt('second'), now=NOW + timedelta(minutes=2))
    assert len([c for c in port.calls if c['ids'] and c['ids'][0].startswith('old-')]) == 1
    clock['now'] += runtime_module._BACKGROUND_INTERVAL + 1
    reply(runtime, receipt('third'), now=NOW + timedelta(minutes=3))
    assert len([c for c in port.calls if c['ids'] and c['ids'][0].startswith('old-')]) == 1
    clock['now'] += runtime_module._BACKGROUND_INTERVAL + 1
    reply(runtime, receipt('fourth'), now=NOW + timedelta(minutes=30))
    assert len([c for c in port.calls if c['ids'] and c['ids'][0].startswith('old-')]) == 2


def test_background_passes_are_bounded_and_spaced(make, monkeypatch):
    clock = {'now': 1000.0}
    monkeypatch.setattr(runtime_module, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    port = Port()
    runtime = make(port)
    backlog(runtime, 20)
    reply(runtime, receipt('a'))
    reply(runtime, receipt('b'), now=NOW + timedelta(minutes=1))
    passes = [call for call in port.calls if call['ids'] and call['ids'][0].startswith('old-')]
    assert [len(call['ids']) for call in passes] == [8]  # one pass of at most eight
    clock['now'] += runtime_module._BACKGROUND_INTERVAL + 1
    reply(runtime, receipt('c'), now=NOW + timedelta(minutes=2))
    passes = [call for call in port.calls if call['ids'] and call['ids'][0].startswith('old-')]
    assert [len(call['ids']) for call in passes] == [8, 8]
    assert len(runtime.store.pending_source_ids(before=NOW + timedelta(hours=1), limit=33)) == 4


def test_when_a_request_does_not_fit_context_waits_and_the_reply_input_stays(make, monkeypatch):
    port = Port()
    runtime = make(port)
    runtime.store.receive('earlier', '刚才那条消息没处理上', occurred_at=NOW - timedelta(hours=2))
    one_source = lambda plan: plan is None or len(plan[0]['sources']) <= 1
    monkeypatch.setattr(jev_emotion, 'fits', one_source)
    view = reply(runtime, receipt('now'))
    assert port.calls[0]['ids'] == ['received-user:qq:now']
    assert 'pending_current_input' not in view
    assert runtime.store.pending_source_ids(before=NOW, limit=5) == ['earlier']  # waits for the next pass


def test_a_reply_whose_own_appraisal_fails_is_flagged_even_with_no_backlog(make):
    port = Port(fail=lambda ids: True)
    runtime = make(port)
    view = reply(runtime, receipt('now'))
    assert view['pending_current_input'] is True and runtime.error_code == 'EMOTION_EVALUATION_UNAVAILABLE'


def test_reply_path_statements_do_not_depend_on_how_much_is_waiting(make, monkeypatch):
    import sqlite3
    statements, counts = [], {}
    connect = sqlite3.connect

    def counting(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db
    monkeypatch.setattr(sqlite3, 'connect', counting)
    for waiting in (0, 200):
        port = Port()
        runtime = make(port, f'life-{waiting}')
        backlog(runtime, waiting)
        runtime._background_after = float('inf')  # measure the reply path alone
        statements.clear()
        asyncio.run(runtime.evaluate_received([receipt('now')], now=NOW))
        counts[waiting] = (len(statements), len(port.calls), port.calls[0]['bytes'])
    assert counts[0][:2] == counts[200][:2] and abs(counts[0][2] - counts[200][2]) <= 8


def test_backlog_older_than_the_horizon_is_never_billed(make):
    port = Port()
    runtime = make(port, 'ancient')
    backlog(runtime, 5, age=runtime_module._BACKGROUND_HORIZON + timedelta(hours=1))
    reply(runtime, receipt('now'))
    assert [call['ids'] for call in port.calls if call['purpose'] == 'character-emotion'] == [['received-user:qq:now']]
