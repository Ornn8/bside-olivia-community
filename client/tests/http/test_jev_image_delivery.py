from runtime.reply.jev_limits import JEV_MAX_INPUT_BYTES
"""Single-image Jev delivery uses the durable photo worker, never a text draft."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from runtime import image_reply, image_understanding
from runtime.personal_chat import backend
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService
from runtime.reply.companion_runtime import delivery_for, prepare_decision
from runtime.reply.character_emotion_context import freeze_expression_context, store_expression_context
from tests.persona.test_jev_pipeline import plan, Port


def image_record():
    return {'plan': plan(kind='image'), 'input_revision': 0}


@pytest.mark.parametrize('unknown', [False, True])
def test_image_is_async_acknowledged_once_and_never_sends_scene_draft(monkeypatch, tmp_path, unknown):
    async def scenario():
        rows, sends, commits = [], [], []
        gate = asyncio.Event()
        server = SimpleNamespace(store=SimpleNamespace(personal_chats=rows), _persist_store_state=lambda: None)
        async def generate(event, row):
            assert row['image_available']
            row.update(companion_decision=image_record(), companion_delivery='image', companion_timing='now')
            return 'INTERNAL SCENE DRAFT'
        async def prepare(server, row, send):
            await gate.wait()
            row.update(image_status='COMPLETED', prepared_image=str(tmp_path/'synthetic.png'))
        async def image_memory(*a): pass
        monkeypatch.setattr(backend, 'prepare_chat_photo', prepare)
        monkeypatch.setattr(image_understanding, 'commit_image_memory', image_memory)
        async def photo(row, send): await backend.deliver_photo(server, row, send)
        async def commit(row): commits.append(row['reply_text'])
        class Send:
            async def __call__(self, text):
                assert text != 'INTERNAL SCENE DRAFT'
                sends.append(('notice', text))
                return 'notice-ack'
            async def image(self, path):
                sends.append(('image', path))
                if unknown: raise TimeoutError()
                return 'image-ack'
        event, send = PersonalMessage('qq', 'b', 'u', '1', '发张照片'), Send()
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('b', 'u')}, photo=photo)
        await service.handle(event, send)
        assert rows[0]['delivery_status'] == 'MEDIA_PENDING'
        assert not service.lock.locked() and not sends
        gate.set()
        await asyncio.gather(*service.photo_tasks.values())
        if service.consumer_tasks: await asyncio.gather(*service.consumer_tasks.values())
        if unknown:
            assert rows[0]['image_delivery_status'] == 'UNKNOWN' and not commits
            with pytest.raises(RuntimeError): await service.handle(event, send)
        else:
            assert rows[0]['delivery_status'] == 'DELIVERED'
            assert rows[0]['reply_text'] == '[已发送图片]'
            assert commits == ['[已发送图片]']
            await service.handle(event, send)
        assert len([s for s in sends if s[0] == 'image']) == 1
    asyncio.run(scenario())


def test_image_plan_and_backend_capability_are_consumable(monkeypatch, tmp_path):
    from tests.http.test_chat_jev_decision import server_fixture, pipeline_result
    monkeypatch.setenv('OLIVIA_GPU_API_URL', 'http://127.0.0.1:9')
    monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'synthetic')
    result = pipeline_result(delivery='image')
    result.companion_decision = image_record()
    server, row, seen, _, audio = server_fixture(monkeypatch, tmp_path, result)
    row['image_available'] = True
    assert asyncio.run(backend.generate(server, PersonalMessage('qq','b','u','1','照片'), row)) == 'Hello'
    assert delivery_for(SimpleNamespace(plan=result.companion_decision['plan']), kinds=seen[0]['semantic_kinds']) == ('now','image')
    assert image_reply.is_companion_image(row) and not audio


@pytest.mark.parametrize('status,expected', [('available', ['pleased']), ('stale', [])])
def test_image_uses_frozen_present_mood_without_needing_new_user_reaction(status, expected):
    now = datetime.now(timezone.utc)
    row = dict(reply_text='刚练顺那一段。')
    emotion = dict(interpretation_only=True, reaction_subject='character', reactions=[], concerns=[],
        current_affect=dict(status=status,label='pleased',as_of=now.isoformat(),
                            reason='练习有了进展',basis={'source_id':'PRIVATE'}))
    store_expression_context(row, freeze_expression_context('test', now, emotion=emotion), row['reply_text'])
    emotion['current_affect']['label'] = 'hurt'
    value = image_reply._photo_reference(row,row['reply_text'])
    assert value['expression_options'] == expected
    assert ('current_affect' in value) == (status == 'available')
    assert 'PRIVATE' not in json.dumps(value)


def test_new_world_fields_reach_planner_without_ids_or_inferred_attendance():
    now = datetime.now(timezone.utc)
    world = dict(stale=False, current=dict(evidence_kind='published_life', location='宿舍', activity='整理书包', source_id='PRIVATE'),
        schedule=dict(phase='class', current_class=dict(title='和声', start='09:00', end='10:40', source_id='PRIVATE')),
        weather=dict(status='fresh', temperature_c=25, observed_at=now.isoformat(), raw='PRIVATE'),
        meals=[dict(date='2026-09-28',slot='lunch',food='面条',status='planned',stale=True,source_id='PRIVATE')],
        character_development=dict(items=[dict(label='摄影', stage='growing',source_id='PRIVATE')]))
    row = dict(reply_text='书包放在桌边')
    store_expression_context(row, freeze_expression_context('test',now,world=world), row['reply_text'])
    value = image_reply._photo_reference(row,row['reply_text'])
    assert value['world_activity']['activity']=='整理书包'
    assert value['course_plan']['attendance_confirmed'] is False
    assert value['meal_records'][0]['status']=='planned' and value['meal_records'][0]['stale']
    assert value['weather_observation']['temperature_c']==25
    assert value['preference_changes'][0]['label']=='摄影'
    assert 'PRIVATE' not in json.dumps(value)
    world['stale']=True
    world['weather']['status']='stale'
    store_expression_context(row,freeze_expression_context('test',now,world=world),row['reply_text'])
    value=image_reply._photo_reference(row,row['reply_text'])
    assert 'world_activity' not in value and 'weather_observation' not in value


@pytest.mark.parametrize('confirmed', [True, False])
def test_jev_sees_image_ack_separate_from_original_chat(confirmed):
    from runtime.reply.conversation_context import conversation_context
    from runtime.reply.fact_attribution import prepare_dialogue_messages
    row = dict(letter_id='older',channel='qq',delivery_status='DELIVERED',created_at=1,
               content='发张照片',reply_text='我拍一张',image_delivery_status='DELIVERED' if confirmed else 'UNKNOWN')
    packet,_=conversation_context([row],query='你睡了吗',now=datetime.now(timezone.utc))
    messages=prepare_dialogue_messages((dict(role='system',content='<untrusted_history>'+json.dumps({'text':packet})+'</untrusted_history>'),
        dict(role='user',content='你睡了吗')),max_input_chars=40000)
    port=Port()
    asyncio.run(prepare_decision(port,messages,'你睡了吗',source_id='current',input_revision=0,
        as_of=datetime.now(timezone.utc),kinds=['text']))
    wire=port.turns[0].input['messages']
    assert wire[-1]['text']=='你睡了吗'
    assert ('application_delivery_record' in wire[-2]['text']) is confirmed
    if confirmed: assert json.loads(wire[-2]['text'])['original_chat_text']=='我拍一张'


def test_primary_image_uses_existing_planner_and_receipt(tmp_path, monkeypatch):
    from tests.http.test_image_reply_recovery import setup
    async def scenario():
        server,row,calls=setup(tmp_path,monkeypatch,failure='none')
        row.update(companion_decision=image_record(),companion_delivery='image',reply_text='桌上的茶杯')
        await image_reply.prepare(server,row,'发张照片',row['reply_text'],channel='qq')
        assert row['image_status']=='COMPLETED' and len(calls['submits'])==1
        await image_reply.prepare(server,row,'发张照片',row['reply_text'],channel='qq')
        assert len(calls['submits'])==1
    asyncio.run(scenario())


def test_decision_input_leaves_out_oldest_turns_instead_of_failing(monkeypatch):
    """A long QQ burst exceeded the decide request cap and failed the reply before writing."""
    from runtime.reply import companion_runtime
    turns = [dict(source=f'reply:old{i}:1', event_id=f'reply:old{i}:1:user', role='user', text=f'第{i}条消息' + '很长的内容' * 400)
             for i in range(12)]
    monkeypatch.setattr(companion_runtime, '_decision_context', lambda messages, required=(): [dict(r) for r in turns])
    port = Port()
    asyncio.run(prepare_decision(port, [], '现在呢', source_id='current', input_revision=0,
                                 as_of=datetime.now(timezone.utc), kinds=['text']))
    wire = port.turns[0].input['messages']
    assert wire[-1]['text'] == '现在呢'
    assert len(json.dumps({'input': port.turns[0].input}, ensure_ascii=False).encode()) <= JEV_MAX_INPUT_BYTES
    kept = [m['text'][:5] for m in wire[:-1]]
    assert kept and kept[-1].startswith('第11条')  # newest turns stay, oldest go first


@pytest.mark.parametrize('photo_ok', [True, False])
def test_a_qq_photo_turn_says_its_line_then_sends_the_photo(monkeypatch, tmp_path, photo_ok):
    # 2.3.9 (owner 2026-10-14: 只发一张图不说话干巴巴的): the reply is a spoken line, the photo follows it.
    async def scenario():
        rows, sends, commits = [], [], []
        server = SimpleNamespace(store=SimpleNamespace(personal_chats=rows), _persist_store_state=lambda: None)
        async def generate(event, row):
            row.update(companion_decision=image_record(), companion_delivery='image', companion_timing='now',
                       photo_with_words=True)
            return '好呀，刚洗完头发，给你拍一张'
        async def prepare(server, row, send):
            if photo_ok:
                row.update(image_status='COMPLETED', prepared_image=str(tmp_path / 'synthetic.png'))
            else:
                row.update(image_status='FAILED', image_error_code='GPU_REQUEST_FAILED')
        async def image_memory(*a): pass
        monkeypatch.setattr(backend, 'prepare_chat_photo', prepare)
        monkeypatch.setattr(image_understanding, 'commit_image_memory', image_memory)
        async def photo(row, send): await backend.deliver_photo(server, row, send)
        async def commit(row): commits.append(row['reply_text'])
        class Send:
            async def __call__(self, text):
                sends.append(('text', text))
                return 'text-ack'
            async def image(self, path):
                sends.append(('image', path))
                return 'image-ack'
        event, send = PersonalMessage('qq', 'b', 'u', '1', '发张照片'), Send()
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('b', 'u')}, photo=photo)
        await service.handle(event, send)
        if service.photo_tasks:
            await asyncio.gather(*service.photo_tasks.values())
        if service.consumer_tasks:
            await asyncio.gather(*service.consumer_tasks.values())
        assert sends[0] == ('text', '好呀，刚洗完头发，给你拍一张')
        assert rows[0]['delivery_status'] == 'DELIVERED'       # the line went out; a lost photo never undoes it
        if photo_ok:
            assert [kind for kind, _ in sends] == ['text', 'image']
        else:
            assert 'image' not in [kind for kind, _ in sends]
            assert len(sends) == 2 and sends[1][0] == 'text'   # the photo failure notice
        assert commits
    asyncio.run(scenario())


def test_the_qq_photo_line_is_written_as_her_own_words():
    from runtime.reply.companion_runtime import decision_instruction
    decision = SimpleNamespace(source_ids=(), plan={}, writer_projection=lambda: {'timing': 'now'})
    voice = decision_instruction(decision, delivery='qq_image_voice')
    text = decision_instruction(decision, delivery='qq_image')
    scene = decision_instruction(decision, delivery='image')
    assert '正文是你此刻对用户说的话' in voice and 'delivery=voice' in voice
    assert '正文是你此刻对用户说的话' in text and 'delivery 使用 text' in text and 'delivery=voice' not in text
    assert '仅作为照片规划的内部描述' in scene and '仅作为照片规划的内部描述' not in voice
