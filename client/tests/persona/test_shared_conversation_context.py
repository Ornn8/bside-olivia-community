import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from runtime.personal_chat.presentation import CURRENT


def test_adapter_uses_same_history_across_letter_and_im_modes():
    import local_server
    adapter = object.__new__(local_server.LetterAdapter)
    adapter._now = lambda: datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    adapter.recent_letters = lambda: [
        dict(letter_id='letter', reply_revision=1, letter_status='COMPLETED', created_at=1,
             private_world_occurred_at='2026-09-20T09:00:00+00:00', content='还没吃饭', reply_text='慢慢做'),
        dict(letter_id='qq', reply_revision=1, letter_status='COMPLETED', delivery_status='DELIVERED',
             channel='qq', created_at=2, private_world_occurred_at='2026-09-20T09:01:00+00:00',
             content='刚到家', reply_text='欢迎回来'),
        dict(letter_id='pending', letter_status='COMPLETED', delivery_status='GENERATED', channel='wechat',
             created_at=3, content='消息', reply_text='尚未送达'),
    ]
    outputs = []
    for presentation in (None, {'channel': 'qq'}, {'channel': 'wechat'}):
        token = CURRENT.set(presentation)
        try:
            outputs.append(adapter.recent_letter_fragments('吃什么'))
        finally:
            CURRENT.reset(token)
    assert outputs[0] == outputs[1] == outputs[2]
    packet = json.loads(outputs[0][0].text)
    assert packet['kind'] == 'recent_dialogue'
    assert [r['source_id'] for r in packet['letters']] == ['reply:letter:1', 'reply:qq:1']
    assert packet['letters'][0]['channel'] == 'letter'
    assert '尚未送达' not in str(outputs)


def test_projection_keeps_media_outcome_and_native_sources_for_recall():
    from runtime.reply.fact_attribution import prepare_dialogue_messages
    from runtime.memory.recall_check import _sources
    packet = {'kind': 'recent_dialogue', 'letters': [dict(source_id='reply:voice:1',
        user_letter='唱一段', linli_reply='我试试', media_deliveries=[{'summary': '制作失败'}],
        media_outcome='failed')]}
    messages = ({'role': 'system', 'content': '<untrusted_history>' + json.dumps({
        'text': json.dumps(packet, ensure_ascii=False)}, ensure_ascii=False) + '</untrusted_history>'},
        {'role': 'user', 'content': '刚才唱了吗'})
    projected = prepare_dialogue_messages(messages, max_input_chars=10000)
    assert prepare_dialogue_messages(projected, max_input_chars=10000) == projected
    assert '制作失败' in projected[0]['content']
    sources, present = _sources(projected)
    assert present
    originals = [json.loads(s['text'])[0] for s in sources if s['scope'] == 'historical_exchange']
    assert [(x['speaker'], x['text']) for x in originals] == [('user', '唱一段'), ('linli', '我试试')]
    assert '刚才唱了吗' not in str(originals)


@pytest.mark.parametrize('mode', ['text_letter', 'voice_reply', 'spoken_video', 'singing_video',
                                  'voice_song_video', 'musical_video', 'future_im'])
def test_direct_adapter_modes_preserve_same_context_and_only_change_format(mode):
    from local_server import LetterAdapter
    from llm_gateway import GatewayConfig
    from runtime.reply.reply_context import ReplyMode
    from runtime.memory.memory_port import NullMemoryPort
    adapter = LetterAdapter(GatewayConfig(provider='mock'), memory_port=NullMemoryPort())
    adapter._now = lambda: datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    adapter.recent_letters = lambda: [dict(letter_id='fixture', reply_revision=1, letter_status='COMPLETED',
        created_at=1, private_world_occurred_at='2026-09-20T09:00:00+00:00',
        content='我还没吃饭', reply_text='慢慢做')]
    messages = adapter.reply_context_messages('你刚才说什么', mode=ReplyMode(mode))
    assert messages[-1]['role'] == 'user'
    assert '你刚才说什么' in messages[-1]['content']
    history = [m for m in messages if m['content'].startswith('[历史消息 ')]
    assert [m['role'] for m in history] == ['user', 'assistant']
    assert '我还没吃饭' in history[0]['content']
    assert '慢慢做' in history[1]['content']


def test_song_planner_keeps_all_shared_messages():
    from runtime.media.song_content import _planning_messages
    from llm_gateway import GatewayConfig
    shared = ({'role': 'system', 'content': 'persona'}, {'role': 'user', 'content': 'old user'},
              {'role': 'assistant', 'content': 'old reply'}, {'role': 'system', 'content': 'state boundary'},
              {'role': 'user', 'content': 'current'})
    messages = _planning_messages('current', 60, GatewayConfig(provider='mock'),
        reply_adapter=SimpleNamespace(reply_context_messages=lambda *a, **kw: shared))
    assert messages[:-2] == shared[:-1]
    assert messages[-1] == shared[-1]
    from runtime.media.song_content import _planner_contract
    assert messages[-2] == {'role': 'system', 'content': _planner_contract(60)}


def test_proactive_generation_preserves_native_history(monkeypatch):
    import asyncio
    import local_server
    from runtime.memory import history_selection as recall_check
    shared = ({'role': 'system', 'content': 'persona'}, {'role': 'user', 'content': 'earlier message'},
              {'role': 'assistant', 'content': 'earlier reply'}, {'role': 'system', 'content': 'state boundary'},
              {'role': 'user', 'content': 'query for retrieval'})
    calls = []
    async def complete(messages, **kwargs):
        calls.append(messages)
        return SimpleNamespace(text='new proactive reply')
    async def prepare(messages, *args, **kwargs):
        return (*messages[:-1], {'role':'system', 'content':'late recall evidence'}, messages[-1])
    monkeypatch.setattr(recall_check, 'select_history_messages', prepare)
    adapter = SimpleNamespace(_messages=lambda *a: shared, gateway=SimpleNamespace(
        complete_scoped=complete, timeout_seconds_for_scope=lambda *a, **k: 5))
    monkeypatch.setattr(local_server, 'letters_adapter', adapter)
    monkeypatch.setattr(local_server, 'store', SimpleNamespace(letters=[dict(letter_id='old',
        reply_revision=1, content='old input', reply_text='old reply')]))
    asyncio.run(local_server._proactive_complete(dict(id='opportunity', source_id='reply:old:1'), planning=False))
    assert len(calls) == 1
    assert calls[0][1:4] == shared[1:-1]
    assert calls[0][-3]['content'] == 'late recall evidence'
    assert '现在写这封主动信' in calls[0][-2]['content']
    assert 'query for retrieval' not in str(calls[0])
    assert json.loads(calls[0][-1]['content'])['previous_user_letter'] == 'old input'


def test_small_context_budget_never_presents_skipped_older_turns_as_recent():
    from runtime.reply.conversation_context import conversation_context
    recent, older = conversation_context([dict(letter_id='large', letter_status='COMPLETED',
        content='原文' * 3000, reply_text='回信' * 3000)], query='原文', now=datetime.now(timezone.utc), max_chars=150)
    assert len(recent) + len(older) <= 150
    assert json.loads(recent)['coverage'] == 'omitted_due_to_capacity'
    assert json.loads(recent)['letters'] == []
