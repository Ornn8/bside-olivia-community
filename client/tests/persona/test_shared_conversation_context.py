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




def test_projection_keeps_received_and_sent_photo_evidence_with_original_turn():
    from runtime.reply.conversation_context import conversation_context
    from runtime.reply.fact_attribution import prepare_dialogue_messages
    now = datetime(2026, 9, 26, 6, tzinfo=timezone.utc)
    observed = dict(summary='卡通人物捂住脸的表情图片', source='user', sha256='a' * 64,
                    observed_at='2026-09-26T05:00:00+00:00', evidence_kind='visual_observation')
    own = dict(observed, summary='窗边的花盆', source='generated', sha256='b' * 64)
    row = dict(letter_id='photo', reply_revision=1, channel='qq', created_at=1,
               delivery_status='DELIVERED', content='只是网图', reply_text='这是谁？',
               life_received_at='2026-09-26T05:00:00+00:00',
               private_world_occurred_at='2026-09-26T05:01:00+00:00',
               incoming_image_observations=[observed], image_description=own,
               image_delivery_status='DELIVERED', image_delivered_at='2026-09-26T05:02:00+00:00')
    recent, _ = conversation_context([row], query='我醒啦', now=now)
    messages = ({'role': 'system', 'content': '<untrusted_history>' + json.dumps({
        'text': recent}, ensure_ascii=False) + '</untrusted_history>'},
        {'role': 'user', 'content': '我醒啦'})
    projected = prepare_dialogue_messages(messages, max_input_chars=20000)
    assert observed['summary'] in projected[0]['content']
    assert own['summary'] in projected[0]['content']
    evidence = json.loads(projected[0]['content'].split('<untrusted_history>')[1].split('</untrusted_history>')[0])
    images = json.loads(evidence['text'])['letters'][0]['image_observations']
    assert [(i['source'], i['parent_id']) for i in images] == [('user', 'photo'), ('generated', 'photo')]
    assert images[1]['observed_at'] == '2026-09-26T05:02:00+00:00'
    assert projected[-1] == messages[-1]
    assert '2026-09-26T13:00:00+08:00' in projected[1]['content']
    assert '2026-09-26T13:01:00+08:00' in projected[2]['content']
    assert prepare_dialogue_messages(projected, max_input_chars=20000) == projected


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
        reply_adapter=SimpleNamespace(reply_context_messages=lambda *a, **kw: shared),
        as_of=datetime(2026, 9, 27, tzinfo=timezone.utc))
    assert messages[:-2] == shared[:-1]
    assert messages[-1] == shared[-1]
    from runtime.media.song_content import _planner_contract
    assert messages[-2] == {'role': 'system', 'content': _planner_contract(60)}


def test_proactive_generation_preserves_native_history(monkeypatch):
    import asyncio
    import local_server
    from llm_gateway import GatewayConfig
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
    adapter = local_server.LetterAdapter(GatewayConfig(provider='mock', persona_v2_enabled=False))
    adapter.persona_provider = SimpleNamespace(snapshot=lambda: SimpleNamespace(system_prompt='persona'))
    adapter.reply_context_messages = lambda *args, **kwargs: shared
    adapter.gateway = SimpleNamespace(complete_scoped=complete, timeout_seconds_for_scope=lambda *a, **k: 5)
    monkeypatch.setattr(local_server, 'letters_adapter', adapter)
    monkeypatch.setattr(local_server, 'store', SimpleNamespace(letters=[dict(letter_id='old',
        reply_revision=1, content='old input', reply_text='old reply', letter_status='COMPLETED')], personal_chats=[]))
    async def exercise():
        intent = dict(id='opportunity', source_id='reply:old:1')
        turn = await local_server._prepare_proactive_turn(intent, now=datetime.now(timezone.utc))
        await local_server._proactive_complete(intent, planning=False, turn=turn)
    asyncio.run(exercise())
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


def test_long_photo_observations_do_not_erase_latest_correction():
    from runtime.reply.conversation_context import conversation_context
    observations = [dict(summary='visible_' + 'x' * 580 + '_end', source='user', sha256=str(i) * 64,
                         observed_at='2026-09-26T05:00:00+00:00', evidence_kind='visual_observation')
                    for i in range(4)]
    row = dict(letter_id='photos', delivery_status='DELIVERED', channel='qq', created_at=1,
               content='前文' * 900 + '这只是网图，不是我拍的。', reply_text='说明' * 900,
               incoming_image_observations=observations)
    recent, old = conversation_context([row], query='我醒啦', now=datetime.now(timezone.utc))
    packet = json.loads(recent)
    assert len(packet['letters']) == 1
    item = packet['letters'][0]
    assert item['user_letter'].endswith('这只是网图，不是我拍的。')
    assert len(item['image_observations']) == 4
    assert item.get('truncated')  # These long originals were actually shortened.
    assert all(i['summary_truncated'] and i['summary'].startswith('visible_')
               and i['summary'].endswith('_end') for i in item['image_observations'])
    assert len(recent) + len(old) <= 6000
    assert len(row['incoming_image_observations'][0]['summary']) > 500
def test_large_unrelated_history_is_not_repeatedly_segmented():
    import cProfile
    from runtime.reply.recent_correspondence import recent_correspondence
    rows = [dict(letter_id=f'scale{i}', reply_revision=1, letter_status='COMPLETED',
        private_world_occurred_at='2026-10-06T00:00:00+00:00',
        content='普通生活见闻。' * 60, reply_text='我听到了这些近况。' * 60) for i in range(1000)]
    profile = cProfile.Profile()
    value = profile.runcall(recent_correspondence, rows, query='早上好', max_chars=6000)
    assert len(json.loads(value)['letters']) == 2
    segmentation_calls = sum(s.callcount for s in profile.getstats() if getattr(s.code, 'co_name', None) == 'tokens')
    assert segmentation_calls < 20  # Irrelevant rows cannot cause thousands of full-text tokenizations.
