import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from llm_gateway import InvalidGatewayInput, validate_messages
from runtime.imports.historical_memory import (
    HistoricalExchange, HistoricalRelationshipError, assess_historical_relationship,
)
from runtime.reply import jev_questions
from runtime.reply.companion_decision import _json
from runtime.model_policy import encode_decision
from runtime.reply.jev_limits import JEV_MAX_INPUT_BYTES


def exchanges():
    return tuple(HistoricalExchange('synthetic:' + str(i),
        datetime.fromtimestamp(i, timezone.utc), '中' * 10000, '文' * 20000)
        for i in range(5))


PREVIOUS = dict(familiarity=10, trust=10, comfort=10, closeness=10,
                tension=0, relationship_stage='unknown')


@pytest.mark.parametrize('budget', [1, 500, 999, 1000])
def test_small_budget_is_rejected_before_gateway_and_preserves_actual_limit(monkeypatch, budget):
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: None)
    class Gateway:
        config = SimpleNamespace(max_input_chars=budget)
        calls = 0
        async def complete(self, messages, **kwargs):
            self.calls += 1
            validate_messages(messages, max_input_chars=budget)
            raise AssertionError('Insufficient budget must not call the gateway')
    gateway = Gateway()
    with pytest.raises(HistoricalRelationshipError) as caught:
        asyncio.run(assess_historical_relationship(exchanges(), gateway=gateway,
            persona_policy='中' * 12000, previous_state=PREVIOUS))
    assert gateway.calls == 0
    assert caught.value.code == 'PRIVATE_WORLD_HISTORY_LLM_INPUT_TOO_LONG'
    context = caught.value.failure_context
    assert context['failure_stage'] == 'prepare' and context['cause_code'] == 'INPUT_TOO_LONG'
    assert context['max_input_chars'] == budget and context['input_chars'] > budget


@pytest.mark.parametrize('cause,expected', [
    (InvalidGatewayInput('INPUT_TOO_LONG'), 'PRIVATE_WORLD_HISTORY_LLM_INPUT_TOO_LONG'),
    (ValueError('JEV_UNAVAILABLE'), 'PRIVATE_WORLD_HISTORY_JEV_UNAVAILABLE'),
    (ValueError('JEV_INPUT_TOO_LARGE'), 'PRIVATE_WORLD_HISTORY_JEV_INPUT_TOO_LARGE'),
    (ValueError('JEV_TIMEOUT'), 'PRIVATE_WORLD_HISTORY_JEV_TIMEOUT'),
    (ValueError('JEV_BALANCE_INSUFFICIENT'), 'PRIVATE_WORLD_HISTORY_JEV_BALANCE_INSUFFICIENT'),
    (ValueError('JEV_HTTP_413'), 'PRIVATE_WORLD_HISTORY_JEV_HTTP_413'),
])
def test_fixed_failure_codes_and_safe_metrics_survive_wrapping(monkeypatch, cause, expected):
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: None)
    class Gateway:
        config = SimpleNamespace(max_input_chars=100000)
        async def complete(self, messages, **kwargs):
            validate_messages(messages, max_input_chars=self.config.max_input_chars)
            raise cause
    with pytest.raises(HistoricalRelationshipError) as caught:
        asyncio.run(assess_historical_relationship(exchanges(), gateway=Gateway(),
            persona_policy='Synthetic public policy', previous_state=PREVIOUS))
    failure = caught.value
    assert failure.code == expected and failure.__cause__ is cause
    assert failure.failure_context['cause_code'] == getattr(cause, 'code', str(cause))
    assert 0 < failure.failure_context['input_chars'] <= 100000
    assert failure.failure_context['failure_stage'] == 'gateway'
    assert set(failure.failure_context) <= {
        'cause_code', 'failure_stage', 'exception_type', 'input_chars', 'max_input_chars',
        'input_bytes', 'max_input_bytes', 'exchange_count',
    }


@pytest.mark.parametrize('code', ['JEV_PRIVATE_API_KEY', {'private': 'secret'}])
def test_unknown_provider_code_cannot_be_exported_or_break_error_wrapping(monkeypatch, code):
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: None)
    cause = RuntimeError('private-provider-content')
    cause.code = code
    class Gateway:
        config = SimpleNamespace(max_input_chars=100000)
        async def complete(self, *args, **kwargs):
            raise cause
    with pytest.raises(HistoricalRelationshipError) as caught:
        asyncio.run(assess_historical_relationship(exchanges(), gateway=Gateway(),
            persona_policy='Synthetic policy'))
    assert caught.value.code == 'PRIVATE_WORLD_HISTORY_LLM_FAILED'
    assert caught.value.__cause__ is cause
    assert caught.value.failure_context['cause_code'] == 'UNKNOWN'
    assert 'private' not in json.dumps(caught.value.failure_context).lower()


def test_long_chinese_policy_fits_actual_jev_packet_and_preserves_five_excerpts(monkeypatch):
    class Questions(jev_questions.JevQuestionsPort):
        def __init__(self):
            super().__init__('http://127.0.0.1:1/v1/companion/decide')
            self.packets = []
        async def ask(self, state, questions, *, purpose):
            packet = dict(state=state, questions=questions, purpose=purpose)
            body = _json(encode_decision(packet, self.transport.endpoint)).encode('utf-8')
            if len(body) > JEV_MAX_INPUT_BYTES:
                raise ValueError('JEV_INPUT_TOO_LARGE')
            self.packets.append((state, len(body)))
            return {k: 'unknown' if k == 'stage' else 'yes' if k.startswith('e')
                    else '10' if '10' in q['criteria'] else 'same'
                    for k, q in questions.items()}
    port = Questions()
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
    gateway = SimpleNamespace(config=SimpleNamespace(max_input_chars=100000))
    result = asyncio.run(assess_historical_relationship(exchanges(), gateway=gateway,
        persona_policy='中' * 60000, previous_state=PREVIOUS))
    assert len(port.packets) == 1 and result.evidence_indexes == (1, 2, 3, 4, 5)
    state, wire_bytes = port.packets[0]
    history = state['history']['ordered_exchanges']
    assert len(history) == 5 and all(len(row['user_letter']) == 600 for row in history)
    assert wire_bytes <= JEV_MAX_INPUT_BYTES
    assert state['history']['previous_state'] == PREVIOUS


def test_result_validation_has_safe_durable_failure_context(monkeypatch):
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: None)
    class Gateway:
        config = SimpleNamespace(max_input_chars=100000)
        async def complete(self, *args, **kwargs):
            return SimpleNamespace(text='synthetic-secret invalid JSON')
    with pytest.raises(HistoricalRelationshipError) as caught:
        asyncio.run(assess_historical_relationship(exchanges(), gateway=Gateway(),
            persona_policy='Synthetic policy'))
    assert caught.value.code == 'PRIVATE_WORLD_HISTORY_RESULT_INVALID'
    assert caught.value.failure_context['failure_stage'] == 'result_validation'
    assert caught.value.failure_context['cause_code'] == 'RESULT_INVALID'
    assert 'synthetic-secret' not in json.dumps(caught.value.failure_context)
