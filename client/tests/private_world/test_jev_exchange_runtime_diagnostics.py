"""The configured production path retains bounded, copied exchange metadata."""
import asyncio
from copy import deepcopy
import json

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime
from runtime.reply import jev_questions
from tests.private_world.test_jev_exchange_diagnostics import (
    DiagnosticPort, PRIVATE_REQUEST_ID, PRIVATE_TEXT, _data,
)


def _runtime(tmp_path, monkeypatch, port):
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)

    def unused_gateway():
        raise AssertionError('configured exchange must use only the questions port')

    return DailyLifeRuntime(DailyLifeStore(tmp_path / 'life.sqlite3'), unused_gateway, lambda: '')


def _complete(runtime):
    return asyncio.run(runtime._complete('PRIVATE_INSTRUCTIONS', _data(), 'life:' + PRIVATE_REQUEST_ID))


@pytest.mark.parametrize('detailed', [True, False])
def test_real_complete_retains_both_phases_with_detailed_or_legacy_choices(tmp_path, monkeypatch, detailed):
    native = DiagnosticPort()

    class Legacy:
        async def ask(self, state, questions, *, purpose):
            native.calls.append((state, questions, purpose))
            return native.choices(state, questions)

    runtime = _runtime(tmp_path, monkeypatch, native if detailed else Legacy())
    result = _complete(runtime)
    events = runtime.last_exchange_diagnostics
    assert [event['phase'] for event in events] == ['exchange-facts', 'exchange-anchored-facts']
    assert result['updates'][0]['status'] == 'completed'
    assert len(native.calls) == 2
    assert 'diagnostics' not in result
    for event, (_state, questions, _purpose) in zip(events, native.calls):
        assert set(event) == {'phase', 'decisions'}
        assert set(event['decisions']) == set(questions)
        for detail in event['decisions'].values():
            assert set(detail) == ({'choice', 'probabilities', 'confidence', 'confidence_source'} if detailed
                                   else {'choice', 'confidence_source'})
            assert detail['confidence_source'] == ('unavailable' if not detailed else
                                                    'deterministic' if len(detail['probabilities']) == 1 else 'upstream')
    encoded = json.dumps(events, ensure_ascii=False)
    for private in (PRIVATE_TEXT, PRIVATE_REQUEST_ID, 'PRIVATE_USER_TEXT', 'PRIVATE_PROJECT_ID',
                    'PRIVATE_PROJECT_TITLE', 'PRIVATE_INSTRUCTIONS', 'PRIVATE_BILLING_RECEIPT'):
        assert private not in encoded


def test_capacity_expansion_retains_three_phases_and_next_exchange_replaces_them(tmp_path, monkeypatch):
    class Expanded(DiagnosticPort):
        def choices(self, state, questions):
            result = super().choices(state, questions)
            if len(self.calls) == 1:
                result['capacity'] = 'unsupported'
            return result

    expanded = Expanded()
    runtime = _runtime(tmp_path, monkeypatch, expanded)
    _complete(runtime)
    assert [event['phase'] for event in runtime.last_exchange_diagnostics] == [
        'exchange-facts', 'exchange-facts', 'exchange-anchored-facts']
    assert len(expanded.calls) == 3
    replacement = DiagnosticPort()
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: replacement)
    _complete(runtime)
    assert len(runtime.last_exchange_diagnostics) == len(replacement.calls) == 2


def test_runtime_diagnostics_are_read_only_and_return_a_deepcopy(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch, DiagnosticPort())
    _complete(runtime)
    expected = runtime.last_exchange_diagnostics
    exposed = runtime.last_exchange_diagnostics
    exposed[0]['decisions']['capacity']['probabilities'].clear()
    exposed[1]['decisions'].clear()
    exposed.clear()
    assert runtime.last_exchange_diagnostics == expected
    with pytest.raises(AttributeError):
        runtime.last_exchange_diagnostics = []


def test_retention_limit_cannot_grow_past_three_events(tmp_path, monkeypatch):
    from runtime.private_world import jev_exchange
    runtime = _runtime(tmp_path, monkeypatch, object())
    emitted = [{'phase': f'synthetic-{i}', 'decisions': {'capacity': {
        'choice': 'ok', 'confidence_source': 'unavailable'}}} for i in range(5)]

    async def extract(_port, _data, _prompt, _request_id, *, diagnostic_observer):
        for event in emitted:
            diagnostic_observer(event)
        return {'updates': []}

    monkeypatch.setattr(jev_exchange, 'extract', extract)
    _complete(runtime)
    expected = deepcopy(emitted[-3:])
    emitted[-1]['decisions'].clear()
    assert runtime.last_exchange_diagnostics == expected


def test_failed_exchange_keeps_only_its_partial_metadata(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch, DiagnosticPort())
    _complete(runtime)

    class Failed(DiagnosticPort):
        async def ask_detailed(self, state, questions, *, purpose):
            if purpose == 'exchange-anchored-facts':
                raise ValueError('JEV_UNAVAILABLE')
            return await super().ask_detailed(state, questions, purpose=purpose)

    failed = Failed()
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: failed)
    with pytest.raises(ValueError, match='JEV_UNAVAILABLE'):
        _complete(runtime)
    assert len(failed.calls) == 1
    assert [event['phase'] for event in runtime.last_exchange_diagnostics] == ['exchange-facts']


def test_conflict_counters_never_push_a_decision_phase_out_of_the_retained_window(tmp_path, monkeypatch):
    from tests.private_world.test_jev_exchange_conflicts import PRACTICE_DATA, SPECS, Port, decide
    runtime = _runtime(tmp_path, monkeypatch, Port(SPECS, conflict=decide('ongoing')))
    result = asyncio.run(runtime._complete('PRIVATE_INSTRUCTIONS', PRACTICE_DATA, 'life:' + PRIVATE_REQUEST_ID))
    assert [item['status'] for item in result['updates']] == ['ongoing']
    events = runtime.last_exchange_diagnostics
    assert [event['phase'] for event in events] == [
        'exchange-facts', 'exchange-anchored-facts', 'exchange-anchored-facts', 'exchange-conflict']
    assert events[-1] == {'phase': 'exchange-conflict', 'outcome': 'decided', 'count': 1}
    assert 'diagnostics' not in result
    # The next exchange starts from nothing, counters included.
    asyncio.run(runtime._complete('PRIVATE_INSTRUCTIONS', PRACTICE_DATA, 'life:next'))
    assert len(runtime.last_exchange_diagnostics) == 4


def test_retained_counters_are_bounded_separately_and_keep_their_order(tmp_path, monkeypatch):
    from runtime.private_world import jev_exchange
    runtime = _runtime(tmp_path, monkeypatch, object())
    emitted = []
    for i in range(5):
        emitted.append({'phase': f'synthetic-{i}', 'decisions': {}})
        emitted.append({'phase': 'exchange-conflict', 'outcome': 'detached', 'count': i + 1})

    async def extract(_port, _data, _prompt, _request_id, *, diagnostic_observer):
        for event in emitted:
            diagnostic_observer(event)
        return {'updates': []}

    monkeypatch.setattr(jev_exchange, 'extract', extract)
    _complete(runtime)
    kept = runtime.last_exchange_diagnostics
    assert [event['phase'] for event in kept] == [
        'synthetic-2', 'exchange-conflict', 'synthetic-3', 'exchange-conflict', 'synthetic-4', 'exchange-conflict']
    assert [event['count'] for event in kept if event['phase'] == 'exchange-conflict'] == [3, 4, 5]
    emitted[-1]['count'] = 99
    assert kept[-1]['count'] == 5 and runtime.last_exchange_diagnostics[-1]['count'] == 5
