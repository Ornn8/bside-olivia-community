"""Capacity recovery preserves originals and never grants missing media authority."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from runtime.personal_chat.context import READ_WINDOW, freeze_read_window
from runtime.reply.character_emotion_context import project_emotion
from runtime.reply.companion_runtime import CompanionRuntimeError, prepare_decision
from runtime.reply.conversation_context import conversation_context
from runtime.reply.fact_attribution import prepare_dialogue_messages
from runtime.reply.reply_orchestrator import ReplyRequest, ReplyState
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import ReviewVerdict
from runtime.reply.reply_context import ReplyMode
from tests.persona.test_bedtime_speech_offer import run_turn
from tests.persona.test_jev_pipeline import Port
from tests.persona.test_jev_reply_recovery import NOW, invoke
from tests.persona.test_reply_semantic_wiring import Engine, Reviewer
from tests.http.test_personal_chat_decision import envelope


def test_final_speech_rules_take_priority_over_optional_emotion(monkeypatch):
    view = dict(interpretation_only=True, reaction_subject='character',
                concerns=[dict(summary='合成关注点。' * 100, source_ids=[])])
    emotion_chars = sum(len(m['content']) for m in project_emotion([], view, max_input_chars=6500))
    _, baseline, _ = run_turn(user='你好', offer='none', budget=40002)
    baseline_chars = sum(len(m['content']) for m in baseline.requests[0].messages)
    padding = 40002 - baseline_chars - emotion_chars + 100
    original_init = Engine.__init__

    async def emotion(*args, **kwargs):
        return deepcopy(view)

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.gateway = SimpleNamespace(adapter=SimpleNamespace(prepare_character_emotion=emotion))

    monkeypatch.setattr(Engine, '__init__', init)
    history = ({'role': 'system', 'content': 'x' * padding},)
    result, engine, port = run_turn(user='你好', history=history, offer='none', budget=40002)
    assert result.state is ReplyState.COMPLETED, result.error_code
    assert len(port.turns) == len(engine.requests) == 1
    wire = engine.requests[0].messages
    assert sum(len(m['content']) for m in wire) <= 40002
    assert wire[-1]['content'] == '你好' and history[0] in wire
    assert '<bedtime_audio_offer>' in str(wire) and '<character_emotion>' not in str(wire)


def original_context(size, *, pending=False):
    rows = [dict(letter_id='synthetic', channel='qq', binding_id='owner', created_at=1,
        delivery_status='DELIVERED', content=('请先发一张照片。' + '合' * size) if pending else '说说近况',
        reply_text='知道了。' if pending else '合' * size, reply_revision=1)]
    if pending:
        rows.append(dict(letter_id='later', channel='qq', binding_id='owner', created_at=2,
            delivery_status='DELIVERED', content='先聊聊', reply_text='好。', companion_decision=dict(
                source_id_map={'t1': 'reply:synthetic:user'},
                plan={'understanding': {'requirements': [dict(fulfillment='pending', evidence_turn_ids=['t1'])]}})))
    window = freeze_read_window(rows, channel='qq', binding_id='owner', current_id='new')
    recent, _ = conversation_context(rows, query='后来呢', now=NOW, max_chars=6000)
    wrapper = json.dumps(dict(fragment_id='chat.recent', text=recent), ensure_ascii=False)
    messages = prepare_dialogue_messages([
        dict(role='system', content='<untrusted_history>' + wrapper + '</untrusted_history>'),
        dict(role='user', content='后来呢')], max_input_chars=40000)
    return window, messages


@pytest.mark.parametrize('pending', [False, True])
def test_oversized_original_recovers_reviewed_text_without_rewriting_memory(pending):
    window, messages = original_context(8001, pending=pending)
    before = deepcopy(window)
    token = READ_WINDOW.set(window)
    port, reviewer = Port(), Reviewer(ReviewVerdict.PASS)
    engine = Engine(envelope(text='我先接着和你聊，媒体还没有发出。', delivery='text', sticker=None))
    pipeline = ReplyPipeline(engine, reviewer=reviewer, rewriter=UnavailableRewriter(),
        discover_runtime_ports=False, companion_decision_port=port)
    try:
        result = asyncio.run(invoke(pipeline, ReplyMode.FUTURE_IM, '后来呢',
            request=ReplyRequest(content='后来呢', request_id='capacity', messages=messages, max_input_chars=40000)))
    finally:
        READ_WINDOW.reset(token)
    assert result.state is ReplyState.COMPLETED, result.error_code
    assert result.companion_decision is None and result.companion_delivery == 'text'
    assert result.degraded_stages == {'decision': 'JEV_CONTEXT_BUDGET_EXCEEDED'}
    assert not port.turns and len(engine.requests) == len(reviewer.seen) == 1
    assert window == before
    assert '尚未完成' in str(engine.requests[0].messages)


def test_valid_8000_character_original_keeps_normal_decision():
    window, messages = original_context(8000)
    token = READ_WINDOW.set(window)
    port = Port()
    try:
        asyncio.run(prepare_decision(port, messages, '后来呢', source_id='new', input_revision=0,
                                    as_of=NOW.isoformat(), kinds=['text']))
    finally:
        READ_WINDOW.reset(token)
    assert len(port.turns) == 1
    assert '合' * 8000 in [m['text'] for m in port.turns[0].input['messages']]


def test_missing_required_source_has_safe_specific_reason():
    with pytest.raises(CompanionRuntimeError) as raised:
        asyncio.run(prepare_decision(Port(), [], '现在呢', source_id='new', input_revision=0,
            as_of=NOW.isoformat(), kinds=['text'], required_sources=['reply:private-missing:user']))
    assert str(raised.value) == 'JEV_CONTEXT_UNAVAILABLE'
    assert raised.value.failure_context == dict(failure_stage='decision_context',
                                               failure_detail='required_source_missing')
    assert 'private-missing' not in str(raised.value.failure_context)


def test_pending_original_cannot_be_dropped_to_fit_total_wire_limit():
    rows = [dict(letter_id=f'old{i}', channel='qq', binding_id='owner', created_at=i,
                 delivery_status='DELIVERED', content='合' * 7800, reply_text='好。') for i in range(4)]
    rows.append(dict(letter_id='latest', channel='qq', binding_id='owner', created_at=5,
        delivery_status='DELIVERED', content='先等一下', reply_text='好。', companion_decision=dict(
            source_id_map={f't{i}': f'reply:old{i}:user' for i in range(4)},
            plan={'understanding': {'requirements': [dict(fulfillment='pending',
                evidence_turn_ids=[f't{i}']) for i in range(4)]}})))
    window = freeze_read_window(rows, channel='qq', binding_id='owner', current_id='new')
    before = deepcopy(window)
    token = READ_WINDOW.set(window)
    port = Port()
    try:
        with pytest.raises(CompanionRuntimeError) as raised:
            asyncio.run(prepare_decision(port, [], '你好', source_id='new', input_revision=0,
                                        as_of=NOW.isoformat(), kinds=['text']))
    finally:
        READ_WINDOW.reset(token)
    assert str(raised.value) == 'JEV_CONTEXT_BUDGET_EXCEEDED'
    assert raised.value.failure_context['failure_detail'] == 'decision_wire_budget'
    assert not port.turns and window == before


def test_local_capacity_receipt_keeps_only_finite_reasons_and_numeric_sizes():
    from runtime.diagnostics.failure_context import provider_failure_context
    safe = dict(failure_stage='writer_context', failure_detail='final_rules_budget',
                input_chars=40101, max_input_chars=40002)
    assert provider_failure_context({**safe, 'text': 'private', 'input_bytes': True,
        'largest_message_chars': 'private', 'source_id': 'private'}) == safe


@pytest.mark.parametrize('changes', [dict(delivery='voice'), dict(skip=True, text='',
    silence={'kind': 'no_reply', 'evidence': '先别找我'}),
    dict(initiative='pause', evidence='先别找我')])
def test_capacity_recovery_cannot_send_media_or_change_controls(changes):
    window, messages = original_context(8001, pending=True)
    token = READ_WINDOW.set(window)
    engine = Engine(envelope(**{'delivery': 'text', 'sticker': None, **changes}))
    pipeline = ReplyPipeline(engine, reviewer=Reviewer(ReviewVerdict.PASS),
        rewriter=UnavailableRewriter(), discover_runtime_ports=False, companion_decision_port=Port())
    try:
        result = asyncio.run(invoke(pipeline, ReplyMode.FUTURE_IM, '先别找我',
            request=ReplyRequest(content='先别找我', request_id='capacity-control',
                messages=(*messages[:-1], dict(role='user', content='先别找我')), max_input_chars=40000)))
    finally:
        READ_WINDOW.reset(token)
    assert result.state is ReplyState.FAILED and not result.text
    assert result.decision_rejection_reason == 'RECOVERY_ACTION_WITHOUT_PLAN'


def test_capacity_recovery_still_requires_successful_review():
    window, messages = original_context(8001)
    token = READ_WINDOW.set(window)
    pipeline = ReplyPipeline(Engine(envelope(delivery='text', sticker=None)),
        reviewer=Reviewer(RuntimeError('synthetic unavailable')), rewriter=UnavailableRewriter(),
        discover_runtime_ports=False, companion_decision_port=Port())
    try:
        result = asyncio.run(invoke(pipeline, ReplyMode.FUTURE_IM, '后来呢',
            request=ReplyRequest(content='后来呢', request_id='capacity-review', messages=messages, max_input_chars=40000)))
    finally:
        READ_WINDOW.reset(token)
    assert result.error_code == 'REVIEW_FAILED' and not result.text
