import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from runtime.reply.reply_orchestrator import ReplyRequest, ReplyResult, ReplyState
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer
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
    result = asyncio.run(ReplyPipeline(orchestrator, reviewer=NullReviewer(), rewriter=UnavailableRewriter(), discover_runtime_ports=False).run(request, context))
    assert result.state is ReplyState.COMPLETED
    assert result.reviewer_calls == result.rewrite_calls == 0
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


def test_native_projection_keeps_recent_turns_when_wrapper_already_fills_budget():
    from runtime.reply.fact_attribution import prepare_dialogue_messages, finalize_reply_messages
    rows = [dict(source_id=f'reply:old{i}', user_letter='用户' * 200,
                 linli_reply='回复' * 200, channel='qq') for i in range(8)]
    wrapper = json.dumps({'text': json.dumps({'kind': 'recent_dialogue', 'letters': rows},
                                           ensure_ascii=False)}, ensure_ascii=False)
    messages = (dict(role='system', content='核心规则<untrusted_history>' + wrapper + '</untrusted_history>'),
                dict(role='user', content='现在呢'))
    budget = sum(len(m['content']) for m in messages)
    projected = prepare_dialogue_messages(messages, max_input_chars=budget)
    assert any(m['content'].startswith('[历史消息 ') for m in projected)
    final = finalize_reply_messages(projected, '本轮视频指令' * 100, max_input_chars=budget)
    assert final[-1] == messages[-1] and '核心规则' in final[0]['content']
    assert 'reply:old7' in str(final)
    assert sum(len(m['content']) for m in final) <= budget


def test_late_budget_trimming_keeps_original_referenced_by_compacted_evidence():
    from runtime.reply.fact_attribution import finalize_reply_messages
    original = dict(role='assistant', content='[历史消息 ' + json.dumps(dict(
        source='reply:old', event_id='reply:old:linli', actor='linli')) + ']\n' + '原话' * 300)
    removable = dict(role='assistant', content='[历史消息 {}]\n' + '过时闲聊' * 300)
    wrapper = json.dumps({'text': json.dumps({'previous_observations': [
        {'text_ref': 'reply:old:linli', 'evidence_kind': 'character_statement'}]})})
    messages = (dict(role='system', content='<evidence_summary>' + wrapper + '</evidence_summary>'),
                original, removable, dict(role='user', content='现在呢'))
    result = finalize_reply_messages(messages, '本轮规则',
        max_input_chars=sum(len(m['content']) for m in messages)-500)
    assert original in result and removable not in result
    assert result[-1] == messages[-1]


def test_long_chat_drops_oldest_cited_originals_before_failing_the_reply():
    # QQ users with long histories: evidence cited every old turn, so nothing
    # uncited was left to drop and the reply failed with JEV_CONTEXT_BUDGET_EXCEEDED.
    from runtime.reply.fact_attribution import finalize_reply_messages
    def cited(n):
        return dict(role='assistant', content='[历史消息 ' + json.dumps(dict(
            source=f'reply:old{n}', event_id=f'reply:old{n}:linli', actor='linli')) + ']\n' + '原话' * 300)
    frames = [cited(n) for n in range(3)]
    wrapper = json.dumps({'text': json.dumps({'previous_observations': [
        {'text_ref': f'reply:old{n}:linli', 'evidence_kind': 'character_statement'} for n in range(3)]})})
    messages = (dict(role='system', content='<evidence_summary>' + wrapper + '</evidence_summary>'),
                *frames, dict(role='user', content='现在呢'))
    budget = sum(len(m['content']) for m in messages) - 500
    result = finalize_reply_messages(messages, '本轮规则', max_input_chars=budget)
    assert frames[0] not in result and frames[1] in result and frames[2] in result
    assert result[-1] == messages[-1] and sum(len(m['content']) for m in result) <= budget
    with pytest.raises(ValueError):
        finalize_reply_messages((messages[0], messages[-1]), '本轮规则', max_input_chars=len(messages[-1]['content']))
