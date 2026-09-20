import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from runtime.personal_chat.presentation import CURRENT
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_orchestrator import ReplyRequest, ReplyResult, ReplyState
from runtime.reply.reply_pipeline import ReplyPipeline


@pytest.mark.parametrize('mode', [ReplyMode.TEXT_LETTER, ReplyMode.FUTURE_IM])
def test_delivery_contract_follows_all_evidence_without_changing_current_input(monkeypatch, mode):
    calls = []
    async def recall(messages, gateway, **kwargs):
        return (*messages[:-1], {'role':'system', 'content':'历史证据：用户还没吃饭'}, messages[-1])
    monkeypatch.setattr('runtime.memory.recall_check.prepare_recall_messages', recall)
    async def generate(request):
        calls.append(request.normalized_messages())
        return ReplyResult(request.request_id, ReplyState.COMPLETED, text='好')
    orchestrator = SimpleNamespace(run=generate)
    pipeline = ReplyPipeline(orchestrator, reviewer=None, rewriter=None, discover_runtime_ports=False)
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
    pipeline = ReplyPipeline(SimpleNamespace(run=generate), reviewer=None, rewriter=None, discover_runtime_ports=False)
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
