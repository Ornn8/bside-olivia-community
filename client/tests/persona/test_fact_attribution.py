import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from runtime.reply.reply_orchestrator import ReplyRequest, ReplyResult, ReplyState
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_pipeline import ReplyPipeline
from runtime.personal_chat.context import chat_context


@pytest.mark.parametrize('mode', list(ReplyMode))
def test_shared_generation_preserves_speakers_without_extra_provider_call(mode):
    row = dict(letter_id='dinner', delivery_status='DELIVERED', channel='wechat',
               created_at=1, content='你晚饭吃什么？', reply_text='我吃青菜腐竹配饭',
               life_received_at='2026-09-20T10:00:00+00:00',
               private_world_occurred_at='2026-09-20T10:01:00+00:00')
    recent, _ = chat_context([row], query='吃的不是小馄饨吗？', now=datetime.now(timezone.utc))
    system = '<untrusted_history>\n' + json.dumps({'untrusted': True, 'text': recent}, ensure_ascii=False) + '\n</untrusted_history>'
    requests = []
    async def generate(request):
        requests.append(request)
        return ReplyResult(request.request_id, ReplyState.COMPLETED, text='是我说串了')
    async def no_checker(**kwargs):
        pytest.fail('unexpected model verification call')
    gateway = SimpleNamespace(config=SimpleNamespace(provider='openai_compatible'), complete_with_tools=no_checker)
    orchestrator = SimpleNamespace(run=generate, gateway=SimpleNamespace(adapter=SimpleNamespace(gateway=gateway)))
    context = ReplyContext.create(mode, trusted_time=TrustedTime(datetime.now(timezone.utc)), future_im_enabled=True)
    request = ReplyRequest(messages=({'role': 'system', 'content': system}, {'role': 'user', 'content': '吃的不是小馄饨吗？'}))
    result = asyncio.run(ReplyPipeline(orchestrator, reviewer=None, rewriter=None).run(request, context))
    assert result.state is ReplyState.COMPLETED
    assert len(requests) == 1
    messages = requests[0].normalized_messages()
    expected = ['system', 'user', 'assistant', 'system']
    if mode is ReplyMode.TEXT_LETTER:
        expected.append('system')
    assert [m['role'] for m in messages] == expected + ['user']
    assert '青菜腐竹配饭' in messages[2]['content']
    assert '"actor": "user"' in messages[1]['content']
    assert '"actor": "linli"' in messages[2]['content']
    assert 'statement_only' in messages[2]['content']
    assert '2026-09-20T18:01' in messages[2]['content']
    assert messages[-1]['content'] == '吃的不是小馄饨吗？'
    assert sum(m['content'].count('我吃青菜腐竹配饭') for m in messages) == 1


def test_context_capacity_never_drops_current_input():
    from runtime.reply.fact_attribution import prepare_dialogue_messages
    messages = ({'role': 'system', 'content': 'x'}, {'role': 'user', 'content': 'y'})
    assert prepare_dialogue_messages(messages, max_input_chars=2) == messages


def test_old_retrieval_is_not_promoted_to_recent_native_dialogue():
    from runtime.reply.fact_attribution import prepare_dialogue_messages
    old = json.dumps({'untrusted': True, 'text': json.dumps({'letters': [
        {'user_letter': '昨天吃过汤包', 'linli_reply': '慢慢吃'}]})})
    messages = ({'role': 'system', 'content': '<untrusted_history>' + old + '</untrusted_history>'},
                {'role': 'user', 'content': '今天还没吃，准备做饭'})
    prepared = prepare_dialogue_messages(messages, max_input_chars=10000)
    assert [m['role'] for m in prepared] == ['system', 'system', 'user']
    assert prepared[-1] == messages[-1]
    assert old in prepared[0]['content']
