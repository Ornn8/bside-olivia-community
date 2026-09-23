"""Current-letter regressions based on reported old-topic replies; no provider calls."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from runtime.reply.conversation_context import conversation_context
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_orchestrator import ReplyRequest, ReplyResult, ReplyState
from runtime.reply.reply_pipeline import ReplyPipeline


@pytest.mark.parametrize('current', [
    '那我推荐《七里香》给你，高中时候单曲循环最多的就是这首。下午戴耳机听一下？',
    '那你先练琴，我整理一下作业照片，今天或者明天把作业交了。等你叫我。',
    '我准备去接娃啦，晚上微信聊哈。',
])
def test_old_topics_do_not_replace_current_letter_at_generation(current):
    now = datetime(2026, 9, 23, 9, 7, tzinfo=timezone.utc)
    rows = [dict(letter_id=str(i), created_at=i, letter_status='COMPLETED',
                 reply_revision=1, content=text, reply_text='收到，回头聊。',
                 life_received_at=now.isoformat(), private_world_occurred_at=now.isoformat())
            for i, text in enumerate(['好的呢，我出发啦。你的老姜。',
                                     '食堂吃什么？下午扫街，课推到下周二。',
                                     '把招式拆开练，落款还是林离。'])]
    recent, _ = conversation_context(rows, query=current, now=now)
    wrapper = json.dumps({'untrusted': True, 'text': recent}, ensure_ascii=False)
    seen = []
    async def generate(request):
        seen.append(request)
        return ReplyResult(request.request_id, ReplyState.COMPLETED, text='收到。')
    pipeline = ReplyPipeline(SimpleNamespace(run=generate), reviewer=None,
                             rewriter=None, discover_runtime_ports=False)
    request = ReplyRequest(request_id='letter-reply:current', messages=(
        {'role': 'system', 'content': '<untrusted_history>' + wrapper + '</untrusted_history>'},
        {'role': 'user', 'content': current}), max_input_chars=20000)
    result = asyncio.run(pipeline.run(request, ReplyContext.create(
        ReplyMode.TEXT_LETTER, trusted_time=TrustedTime(now))))
    assert result.state is ReplyState.COMPLETED
    assert result.request_id == request.request_id
    assert len(seen) == 1
    messages = seen[0].normalized_messages()
    assert messages[-1] == {'role': 'user', 'content': current}
    assert sum(m['content'] == current for m in messages) == 1
    assert any(m['role'] == 'user' and '食堂吃什么' in m['content'] for m in messages[:-1])
    assert all(m['content'].startswith('[历史消息 ') for m in messages[:-1] if m['role'] == 'user')
