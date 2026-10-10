import asyncio
from copy import deepcopy
import hashlib
import json

import pytest

from runtime.reply.context_budget import ContextCapacityError, fit_reply_context, wire_size
from runtime.reply.reply_orchestrator import ReplyState
from runtime.personal_chat.service import PersonalChatService
from tests.persona.test_context_budget_groups import block
from tests.persona.test_bedtime_speech_offer import run_turn


def test_fixed_34k_plus_decision_and_rules_succeeds_once():
    result, engine, port = run_turn(user='hello', history=(
        dict(role='system', content='x' * 34000),), budget=100000)
    assert result.state is ReplyState.COMPLETED, result.error_code
    assert len(engine.requests) == len(port.turns) == 1
    messages = engine.requests[0].messages
    assert 'x' * 34000 in str(messages)
    assert wire_size(messages) <= 88000
    assert '<bedtime_audio_offer>' in str(messages)


def test_3001_exchanges_keep_only_the_explicit_dependency():
    records = [json.dumps([dict(source_id=f'source-{i}', text='x' * 100)])
               for i in range(3001)]
    recall = block('untrusted_history', '[ORIGINAL_CORRESPONDENCE_UNTRUSTED]\n' + '\n'.join(records))
    decision = '<companion_decision>{"source_ids":["source-0"]}</companion_decision>'
    messages = [dict(role='system', content=recall + decision), dict(role='user', content='hello')]
    before = deepcopy(messages)
    result = fit_reply_context(messages, max_input_chars=4000, max_input_bytes=6000)
    text = ''.join(m['content'] for m in result).replace('\\', '')
    assert '"source-0"' in text
    assert 'source-1"' not in text
    assert 'source-3000' in text
    assert sum(len(m['content']) for m in result) <= 4000
    assert messages == before


@pytest.mark.parametrize('value', [chr(0x5408) * 34000, chr(0x1f600) * 25000], ids=['cjk', 'emoji'])
def test_immutable_byte_overflow_reports_counts_only(value):
    with pytest.raises(ContextCapacityError) as raised:
        fit_reply_context([dict(role='system', content=value), dict(role='user', content='hi')],
                          max_input_chars=100000, max_input_bytes=88000)
    report = raised.value.failure_context
    assert report['input_bytes'] > 88000 and report['fixed_chars'] == len(value) + 2
    assert value not in str(report)


@pytest.mark.parametrize('retained', [False, True])
def test_restart_closes_orphan_without_generation_or_send(retained):
    row = dict(letter_id='synthetic', delivery_status='GENERATING', generation_run_id='old',
               generation_attempt_id='attempt', input_revision=1, companion_decision={'paid': True})
    saved = []
    async def forbidden(*args):
        pytest.fail('A completed stage must not be repeated')
    service = PersonalChatService([row], lambda: saved.append(deepcopy(row)), forbidden, forbidden, {},
                                  inspect_writer=lambda value: retained)
    asyncio.run(service.recover())
    assert row['delivery_status'] == 'FAILED' and row['generation_retryable'] is False
    assert row['generation_recovery'] == ('CANDIDATE_RETAINED' if retained else 'NO_CANDIDATE')
    assert row['companion_decision'] == {'paid': True} and saved


@pytest.mark.parametrize('matching', [False, True])
def test_restart_requires_receipt_bound_to_attempt_revision_and_text(matching):
    row = dict(letter_id='synthetic', delivery_status='GENERATING', generation_run_id='old',
               generation_attempt_id='attempt', send_attempt_id='attempt', input_revision=1,
               reply_text='answer', delivery_receipt=dict(attempt_id='attempt', input_revision=1,
                   confirmed=True, text_sha256=hashlib.sha256(b'answer' if matching else b'other').hexdigest()))
    async def forbidden(*args):
        pytest.fail('No resend')
    service = PersonalChatService([row], lambda: None, forbidden, forbidden, {})
    seen = []
    service._schedule_commit = lambda value: seen.append(value['delivery_status'])
    asyncio.run(service.recover())
    assert row['delivery_status'] == ('DELIVERED' if matching else 'DELIVERY_UNCONFIRMED')
    assert seen == [row['delivery_status']]


def test_compact_decision_ids_pin_the_resolved_source_only():
    from types import SimpleNamespace
    from runtime.reply.companion_runtime import decision_instruction
    decision = SimpleNamespace(source_ids=(('t1', 'source-0'), ('t2', 'source-1')), plan={},
        writer_projection=lambda: {'affect': {'evidence_turn_ids': ['t1']}, 'timing': 'now'})
    note = decision_instruction(decision, delivery='text')
    rows = [json.dumps([dict(source_id=f'source-{i}', text='x' * 1000)]) for i in range(2)]
    recall = block('untrusted_history', '[ORIGINAL_CORRESPONDENCE_UNTRUSTED]\n' + '\n'.join(rows))
    result = fit_reply_context([dict(role='system', content=recall + note),
                               dict(role='user', content='hello')], max_input_chars=2000)
    text = str(result)
    assert 'source-0' in text and 'source-1' not in text


@pytest.mark.parametrize('stream', [False, True])
def test_final_gateway_guard_runs_before_transport_and_keeps_sizes(stream):
    from llm_gateway import GatewayConfig, OpenAICompatibleAdapter, InvalidGatewayInput
    from runtime.diagnostics.failure_context import exception_context
    adapter = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible',
        base_url='http://127.0.0.1:1/v1', model='synthetic', requires_api_key=False,
        max_input_chars=100000, max_retries=0))
    messages = [dict(role='user', content='x' * 91000)]
    async def run():
        if stream:
            return [part async for part in adapter.stream(messages, request_id='personal-chat:guard')]
        return await adapter.complete(messages, request_id='personal-chat:guard')
    with pytest.raises(InvalidGatewayInput) as raised:
        asyncio.run(run())
    safe = exception_context(raised.value)
    assert safe['failure_stage'] == 'writer_context'
    assert safe['input_bytes'] > 90000 and safe['input_chars'] == 91000


def test_optional_world_and_canon_share_the_final_capacity():
    world = block('evidence_summary', {'current': {'event_id': 'world-1', 'detail': 'x' * 5000},
                                      'previous_observations': []}, fragment_id='linli.daily-life')
    messages = [dict(role='system', content='<constitution>core</constitution>' +
        '<public_canon>' + 'x' * 5000 + '</public_canon>' + world), dict(role='user', content='hello')]
    result = fit_reply_context(messages, max_input_chars=500)
    assert 'core' in str(result) and result[-1] == messages[-1]
    assert 'world-1' not in str(result) and '<public_canon>' not in str(result)


def test_selected_world_dependency_remains_fixed():
    world = block('evidence_summary', {'current': {'event_id': 'world-1', 'detail': 'x' * 500},
                                      'previous_observations': []}, fragment_id='linli.daily-life')
    contract = '<reply_delivery_plan>{"event_id":"world-1"}</reply_delivery_plan>'
    with pytest.raises(ContextCapacityError):
        fit_reply_context([dict(role='system', content=world + contract),
                           dict(role='user', content='hello')], max_input_chars=200)


def test_initial_assembly_failure_keeps_only_budget_counts():
    from runtime.reply.prompt_budget import PromptBudgetItem, PromptSection, plan_prompt_budget, PromptBudgetExceeded
    from runtime.reply.reply_pipeline import _assembly_capacity_failure
    with pytest.raises(PromptBudgetExceeded) as raised:
        plan_prompt_budget((PromptBudgetItem('private-id', PromptSection.CORE_PERSONA, 400),
                            PromptBudgetItem('optional-id', PromptSection.WORLD_FACT, 200)), max_units=300)
    safe = _assembly_capacity_failure(raised.value)
    assert safe['fixed_chars'] == 400 and safe['optional_chars'] == 200
    assert safe['input_chars'] == 600 and safe['packed_chars'] == 400
    assert 'private-id' not in str(safe)
