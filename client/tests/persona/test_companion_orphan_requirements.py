import asyncio
from copy import deepcopy

import pytest

from runtime.personal_chat.context import READ_WINDOW, freeze_read_window
from runtime.reply import companion_runtime as runtime
from tests.persona.test_jev_pipeline import Port


def pending(source):
    return dict(source_id_map={'t1': source}, plan={'understanding': {
        'requirements': [dict(fulfillment='pending', evidence_turn_ids=['t1'])]}})


def row(key, **extra):
    return dict(letter_id=key, channel='qq', binding_id='owner', created_at=1,
                content='original user input', reply_text='unsent draft', **extra)


@pytest.mark.parametrize('case', ['absent', 'superseded', 'empty', 'failed_assistant',
                                 'skipped_assistant', 'generated_assistant', 'invalid'])
def test_orphan_pending_does_not_poison_successive_decisions(case):
    source = 'reply:old:user'
    rows = []
    if case == 'superseded':
        rows = [row('old', delivery_status='SKIPPED', superseded_by='merged')]
    elif case == 'empty':
        rows = [row('old', delivery_status='FAILED')]
        rows[0]['content'] = ''
    elif case.endswith('_assistant'):
        rows = [row('old', delivery_status=case.split('_')[0].upper())]
        source = 'reply:old:linli'
    elif case == 'invalid':
        source = None
    rows.append(row('latest', delivery_status='DELIVERED', companion_decision=pending(source)))
    window = freeze_read_window(rows, channel='qq', binding_id='owner', current_id='new')
    before = deepcopy(window)
    token = READ_WINDOW.set(window)
    port = Port()
    try:
        for number in range(2):
            decision = asyncio.run(runtime.prepare_decision(port, [], f'current input {number}',
                source_id=f'reply:new{number}:user', input_revision=0,
                as_of='2026-10-11T10:00:00+00:00', kinds=['text']))
            assert decision.record()['input']['messages'][-1]['text'] == f'current input {number}'
        assert window == before
        assert all('unsent draft' not in str(t.input) for t in port.turns)
    finally:
        READ_WINDOW.reset(token)


def test_missing_map_entry_and_role_mismatch_record_abandoned_evidence(monkeypatch):
    record = pending('reply:old:1:linli')
    record['plan']['understanding']['requirements'].append(
        dict(fulfillment='pending', evidence_turn_ids=['missing-map-entry']))
    window = [dict(letter_id='latest', companion_decision=record)]
    monkeypatch.setattr(runtime, '_recent_dialogue', lambda _: [dict(
        source='reply:old:1', event_id='reply:old:1:user', role='user', text='keep original', truncated=False)])
    saved = []

    async def save(items):
        saved.extend(items)

    token = READ_WINDOW.set(window)
    try:
        port = Port()
        asyncio.run(runtime.prepare_decision(port, [], 'new input', source_id='new', input_revision=0,
            as_of='2026-10-11T10:00:00+00:00', kinds=['text'], record_context_recovery=save))
        assert [item['reason'] for item in saved] == ['required_source_missing', 'required_source_invalid']
        assert all(item['status'] == 'abandoned' for item in saved)
        assert port.turns[0].input['messages'][-1]['text'] == 'new input'
        assert all(m['role'] == 'user' for m in port.turns[0].input['messages'])
    finally:
        READ_WINDOW.reset(token)


def test_missing_excerpt_is_discarded_but_explicit_requirement_stays_strict(monkeypatch):
    monkeypatch.setattr(runtime, '_recent_dialogue', lambda _: [dict(
        source='reply:old:1', event_id='reply:old:1:user', role='user', text='clipped', truncated=True)])
    token = READ_WINDOW.set([dict(letter_id='latest', companion_decision=pending('reply:old:1:user'))])
    try:
        assert runtime._decision_context([]) == []
        with pytest.raises(runtime.CompanionRuntimeError):
            asyncio.run(runtime.prepare_decision(Port(), [], 'new input', source_id='new', input_revision=0,
                as_of='2026-10-11T10:00:00+00:00', kinds=['text'], required_sources=['reply:old:1:user']))
    finally:
        READ_WINDOW.reset(token)


def test_pipeline_saves_new_decision_after_abandoning_unavailable_evidence():
    from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
    from runtime.reply.reply_reviewer import NullReviewer
    from runtime.reply.reply_context import ReplyMode
    from runtime.reply.reply_orchestrator import ReplyState
    from tests.persona.test_jev_reply_recovery import invoke
    from tests.persona.test_reply_semantic_wiring import Engine
    from tests.http.test_personal_chat_decision import envelope
    saved, diagnostics = [], []

    async def save(record):
        saved.append(deepcopy(record))

    async def record(items):
        diagnostics.extend(items)

    token = READ_WINDOW.set([dict(letter_id='latest', companion_decision=pending('reply:missing:user'))])
    try:
        pipeline = ReplyPipeline(Engine(envelope(text='收到新消息', delivery='text', sticker=None)),
            reviewer=NullReviewer(), rewriter=UnavailableRewriter(), discover_runtime_ports=False,
            companion_decision_port=Port())
        result = asyncio.run(invoke(pipeline, ReplyMode.FUTURE_IM, 'new input', metadata={
            'save_companion_decision': save, 'record_context_recovery': record}))
        assert result.state is ReplyState.COMPLETED, result.error_code
        assert saved[0] == result.companion_decision
        assert saved[0]['input']['messages'][-1]['text'] == 'new input'
        assert diagnostics == [dict(requirement_index=0, status='abandoned', reason='required_source_missing')]
    finally:
        READ_WINDOW.reset(token)


@pytest.mark.parametrize('state', ['FAILED', 'GENERATED', 'DELIVERY_UNCONFIRMED'])
def test_pending_user_original_is_retained_from_received_only_row(state):
    rows = [row('old', delivery_status=state),
            row('latest', delivery_status='DELIVERED', companion_decision=pending('reply:old:user')),
            {**row('new', delivery_status='GENERATING'), 'created_at': 2}]
    token = READ_WINDOW.set(freeze_read_window(rows, channel='qq', binding_id='owner', current_id='new'))
    try:
        context = runtime._decision_context([])
        assert context[0]['text'] == 'original user input' and context[0]['required']
        assert all(r['role'] == 'user' for r in context)
    finally:
        READ_WINDOW.reset(token)


@pytest.mark.parametrize('state', ['FAILED', 'SKIPPED', 'GENERATED', 'DELIVERY_UNCONFIRMED'])
def test_old_pending_requirement_cannot_revive_outside_current_read_window(state):
    rows = [row('old', delivery_status=state),
            {**row('latest', delivery_status='DELIVERED', companion_decision=pending('reply:old:user')),
             'created_at': 500},
            {**row('new', delivery_status='GENERATING'), 'created_at': 600}]
    window = freeze_read_window(rows, channel='qq', binding_id='owner', current_id='new')
    assert [item['letter_id'] for item in window] == ['latest']
    token = READ_WINDOW.set(window)
    try:
        assert runtime._decision_context([]) == []
    finally:
        READ_WINDOW.reset(token)
