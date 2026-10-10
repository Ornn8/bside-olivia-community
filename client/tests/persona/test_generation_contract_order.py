import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from runtime.personal_chat.presentation import CURRENT
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_orchestrator import ReplyRequest, ReplyResult, ReplyState
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer


@pytest.mark.parametrize('mode', [ReplyMode.TEXT_LETTER, ReplyMode.FUTURE_IM])
def test_delivery_contract_follows_all_evidence_without_changing_current_input(monkeypatch, mode):
    calls = []
    async def recall(messages, gateway, **kwargs):
        return (*messages[:-1], {'role':'system', 'content':'历史证据：用户还没吃饭'}, messages[-1])
    monkeypatch.setattr('runtime.memory.history_selection.select_history_messages', recall)
    async def generate(request):
        calls.append(request.normalized_messages())
        return ReplyResult(request.request_id, ReplyState.COMPLETED, text='好')
    orchestrator = SimpleNamespace(run=generate)
    pipeline = ReplyPipeline(orchestrator, reviewer=NullReviewer(), rewriter=UnavailableRewriter(), discover_runtime_ports=False)
    current = '你晚饭吃什么？'
    request = ReplyRequest(messages=({'role':'system','content':'人设'},
        {'role':'user','content':'我还没做饭'}, {'role':'assistant','content':'我在吃青菜配饭'},
        {'role':'user','content':current}))
    context = ReplyContext.create(mode, trusted_time=TrustedTime(datetime.now(timezone.utc)), future_im_enabled=True)
    token = CURRENT.set({'structured':True})
    try:
        result = asyncio.run(pipeline.run(request, context))
    finally:
        CURRENT.reset(token)
    assert result.state is ReplyState.COMPLETED
    assert result.reviewer_calls == result.rewrite_calls == 0
    assert len(calls) == 1
    messages = calls[0]
    from runtime.personal_chat.decision import INSTRUCTION
    from runtime.reply.letter_presentation import LETTER_PRESENTATION_INSTRUCTION
    instruction = INSTRUCTION if mode is ReplyMode.FUTURE_IM else LETTER_PRESENTATION_INSTRUCTION
    assert messages[-2]['role'] == 'system'
    assert instruction in messages[-2]['content']
    assert sum(m['content'].count(instruction) for m in messages) == 1
    assert messages[-1] == {'role':'user', 'content':current}
    assert any(m['content'] == '历史证据：用户还没吃饭' for m in messages[:-2])
    assert any(m['role'] == 'assistant' and m['content'] == '我在吃青菜配饭' for m in messages)


def test_contract_overflow_fails_before_generation():
    async def generate(request):
        pytest.fail('must not generate without the required delivery contract')
    pipeline = ReplyPipeline(SimpleNamespace(run=generate), reviewer=NullReviewer(), rewriter=UnavailableRewriter(), discover_runtime_ports=False)
    request = ReplyRequest(messages=({'role':'system','content':'人设'}, {'role':'user','content':'你好'}), max_input_chars=10)
    context = ReplyContext.create(ReplyMode.FUTURE_IM, trusted_time=TrustedTime(datetime.now(timezone.utc)), future_im_enabled=True)
    token = CURRENT.set({'structured':True})
    try:
        result = asyncio.run(pipeline.run(request,context))
    finally:
        CURRENT.reset(token)
    assert result.state is ReplyState.FAILED
    assert result.error_code == 'INPUT_TOO_LONG'


def test_finalization_preserves_current_input_and_is_idempotent():
    from runtime.reply.fact_attribution import finalize_reply_messages
    note = '输出正文'
    messages = ({'role':'system','content':note}, {'role':'system','content':'later evidence'},
                {'role':'user','content':note})
    final = finalize_reply_messages(messages, note, max_input_chars=100)
    assert final == ({'role':'system','content':'later evidence'},
                     {'role':'system','content':note}, {'role':'user','content':note})
    assert finalize_reply_messages(final, note, max_input_chars=100) == final


def test_late_output_contract_evicts_old_dialogue_not_current_input_or_rules():
    from runtime.reply.fact_attribution import finalize_reply_messages
    core = {'role': 'system', 'content': '核心人格和已有关系'}
    old = {'role': 'user', 'content': '[历史消息 {}]\n' + '旧话' * 1000}
    recent = {'role': 'assistant', 'content': '[历史消息 {}]\n刚刚说好的约定'}
    # A current message can contain the same prefix; it is never droppable history.
    current = {'role': 'user', 'content': '[历史消息 {}]\n我是在问刚才的约定'}
    messages = (core, old, recent, current)
    final = finalize_reply_messages(messages, '只回复当前输入', max_input_chars=120)
    assert final == (core, recent, {'role': 'system', 'content': '只回复当前输入'}, current)
    assert messages == (core, old, recent, current)
    with pytest.raises(ValueError, match='INPUT_TOO_LONG'):
        finalize_reply_messages((core, current), '规则' * 100, max_input_chars=120)


def test_large_recent_dialogue_still_reaches_writer_with_qq_contract(monkeypatch):
    monkeypatch.delenv('OLIVIA_JEV_DECISION_URL', raising=False)
    calls = []
    async def generate(request):
        calls.append(request.normalized_messages())
        return ReplyResult(request.request_id, ReplyState.COMPLETED, text='好')
    pipeline = ReplyPipeline(SimpleNamespace(run=generate), reviewer=NullReviewer(),
        rewriter=UnavailableRewriter(), discover_runtime_ports=False)
    history = tuple({'role': 'user' if i % 2 == 0 else 'assistant',
        'content': '[历史消息 {}]\n' + '旧内容' * 400} for i in range(40))
    messages = ({'role': 'system', 'content': '核心人格与已确认关系'}, *history,
                {'role': 'user', 'content': '在吗？'})
    request = ReplyRequest(messages=messages, max_input_chars=sum(len(m['content']) for m in messages))
    context = ReplyContext.create(ReplyMode.FUTURE_IM, future_im_enabled=True,
                                  trusted_time=TrustedTime(datetime.now(timezone.utc)))
    token = CURRENT.set({'structured': True})
    try:
        result = asyncio.run(pipeline.run(request, context))
    finally:
        CURRENT.reset(token)
    assert result.state is ReplyState.COMPLETED, result.error_code
    assert len(calls) == 1 and calls[0][-1] == messages[-1] and calls[0][0] == messages[0]
    assert sum(len(m['content']) for m in calls[0]) <= request.max_input_chars
