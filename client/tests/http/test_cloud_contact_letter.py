"""A contact the cloud asks for, carried out as a letter through the real proactive letter pipeline."""
import pytest

from tests.http.test_expression_context_routes import run_isolated

SETUP = r'''
import asyncio, json, os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import local_server as server
from persona_assembly import UntrustedFragment
from runtime import cloud_contact, contact_context, proactive_config
from tests.persona.test_reply_pipeline import ROOT, _configured_v2_pipeline
_, _, bridge, _ = _configured_v2_pipeline(ROOT / 'linli_character/persona_release_v2.json')
server.letters_adapter = bridge.adapter
server.store.letters[:] = []
server.store.personal_chats[:] = []
server._commit_private_world_letter = lambda row: False
server._schedule_daily_life_exchange = lambda row: None
server._conversation_memory_ready_for_reply = lambda: True
server.daily_life_runtime = SimpleNamespace(store=SimpleNamespace(exchange_state=lambda: {}))
server.video_reply_settings_store = SimpleNamespace(image_snapshot=lambda: {'enabled': False})
server.PORT = 8123
calls = []
def life(content, *, recent_fragments=None, now=None):
    return (UntrustedFragment('linli.daily-life', json.dumps({'kind': 'character_life_reference', 'stale': False,
        'current': {'location': '琴房', 'activity': '慢练', 'source_id': 'day:x', 'evidence_kind': 'published_life'}},
        ensure_ascii=False)),)
async def emotion(content, *, now):
    return {'reaction_subject': 'character', 'interpretation_only': True, 'reactions': []}
server.letters_adapter.daily_life_fragments = life
server.letters_adapter.prepare_character_emotion = emotion
failing = []
async def complete(messages, *, request_id, scope):
    calls.append(dict(messages=tuple(dict(m) for m in messages), request_id=request_id, contact=contact_context.current()))
    if failing:
        raise RuntimeError('LLM_UNAVAILABLE')
    if request_id.endswith(':title'):
        return SimpleNamespace(text='{"title":"「模型取的标题」"}')
    return SimpleNamespace(text='今晚想和你说两句。\n[[signature:林离]]')
server.letters_adapter.gateway = SimpleNamespace(complete_scoped=complete, timeout_seconds_for_scope=lambda *a, **kw: 10)
ROOT_DIR = server._state_root()
BEIJING = timezone(timedelta(hours=8))
NOW = int(datetime(2026, 10, 13, 20, tzinfo=BEIJING).timestamp())
acks = []
async def post(payload):
    acks.append(payload)
@contextmanager
def slot():
    yield True
def fields(number, **extra):
    return {'id': 'contact:00000000-0000-4000-8000-%012d' % number, 'kind': 'contact', 'at': NOW - 600,
            'deliver_after': NOW - 60, 'expires': NOW + 3600, 'purpose': 'recall', 'channel': 'letter',
            'brief': '提醒她今晚月亮很亮，不要催她回复', 'qq_format': 'text', **extra}
async def carry(number, **extra):
    """The whole path: recorded, picked, carried out under the contact's context, reported."""
    item = fields(number, **extra)
    cloud_contact.record(ROOT_DIR, [cloud_contact.parse_event(item)], NOW)
    async def deliver(channel, event):
        assert channel == 'letter'
        return await server._deliver_contact_letter(event)
    await cloud_contact.tick(ROOT_DIR, NOW, qq_ready=lambda: False, preferred=lambda: 'letter', deliver=deliver, slot=slot, post=post)
    await cloud_contact.tick(ROOT_DIR, NOW + 30, qq_ready=lambda: False, preferred=lambda: 'letter', deliver=deliver, slot=slot, post=post)
    return item['id']
def letters_of(contact_id):
    return [row for row in server.store.letters if row.get('contact_id') == contact_id]
'''


def test_a_letter_is_written_from_the_brief_and_length_and_titled_from_its_own_text(tmp_path):
    run_isolated(tmp_path, SETUP + r'''
async def main():
    contact_id = await carry(1, letter_length={'min': 200, 'max': 400})
    assert [call['request_id'].rsplit(':', 1)[-1] for call in calls] == ['body', 'title']
    assert all(call['contact'] == contact_id for call in calls)  # Every call ran in the contact's context.
    body, title = [' '.join(m['content'] for m in call['messages']) for call in calls]
    # The title is asked of the finished letter alone: no brief, no context, a small request.
    assert [m['content'] for m in calls[1]['messages']] == [server.LETTER_TITLE_RULE, '今晚想和你说两句。']
    assert '提醒她今晚月亮很亮' not in title and len(title) < len(body) / 5
    assert cloud_contact.LETTER_RULE in body and '提醒她今晚月亮很亮，不要催她回复' in body
    assert '字数要求：正文200到400字。' in body
    assert '判断林离是否有具体' not in body and '没有自然的具体话题就defer' not in body   # Not her own decision to write.
    assert 'cloud' not in contact_id and 'opportunity' in body   # The frozen view still frames the turn.
    [letter] = letters_of(contact_id)
    assert letter['origin'] == 'proactive' and letter['proactive_kind'] == 'contact' and letter['proactive_candidate_id'] == contact_id
    assert letter['contact_purpose'] == 'recall' and letter['contact_media'] == []
    assert letter['title'] == '模型取的标题' and letter['reply_text'] == '今晚想和你说两句。' and letter['reply_signature'] == '林离'
    assert letter['letter_status'] == 'COMPLETED' and letter['is_read'] == 0 and letter['reply_mode'] == 'text'
    assert cloud_contact.handled(ROOT_DIR)[contact_id]['status'] == 'delivered' and cloud_contact.pending(ROOT_DIR) == []
    assert acks == [{'id': contact_id, 'status': 'delivered', 'channel': 'letter', 'error_code': None}]
    assert contact_context.current() is None and not getattr(server, '_jev_contact_channel', None)
asyncio.run(main())
''')


def test_a_title_the_cloud_gave_is_used_and_the_model_is_not_asked_for_one(tmp_path):
    run_isolated(tmp_path, SETUP + r'''
async def main():
    contact_id = await carry(2, title='今晚的月亮')
    assert [call['request_id'].rsplit(':', 1)[-1] for call in calls] == ['body']
    [letter] = letters_of(contact_id)
    assert letter['title'] == '今晚的月亮'
    # Without a length the instruction carries none.
    assert '字数要求' not in ' '.join(m['content'] for m in calls[0]['messages'])
asyncio.run(main())
''')


def test_the_titles_the_model_cannot_give_fall_back_to_the_usual_one(tmp_path):
    run_isolated(tmp_path, SETUP + r'''
original = complete
async def odd(messages, *, request_id, scope):
    if request_id.endswith(':title'):
        calls.append(dict(request_id=request_id))
        return SimpleNamespace(text='not json at all')
    return await original(messages, request_id=request_id, scope=scope)
server.letters_adapter.gateway = SimpleNamespace(complete_scoped=odd, timeout_seconds_for_scope=lambda *a, **kw: 10)
async def main():
    contact_id = await carry(3)
    [letter] = letters_of(contact_id)
    assert letter['title'] == cloud_contact.DEFAULT_TITLE and letter['letter_status'] == 'COMPLETED'
asyncio.run(main())
''')


def test_a_fixed_text_is_published_as_it_is_and_no_model_is_ever_called(tmp_path):
    run_isolated(tmp_path, SETUP + r'''
async def forbidden(*args, **kwargs):
    raise AssertionError('a fixed text needs no generation')
server._prepare_proactive_turn = forbidden
server._proactive_complete = forbidden
server._conversation_memory_ready_for_reply = lambda: False   # Not even memory has to be ready.
body = '欢迎你来。\n这里有一封信给你。\n\n林离'
async def main():
    contact_id = await carry(4, brief=None, body=body)
    assert calls == []
    [letter] = letters_of(contact_id)
    assert letter['reply_text'] == body and letter['reply_signature'] is None
    assert letter['title'] == cloud_contact.DEFAULT_TITLE and letter['letter_status'] == 'COMPLETED'
    assert letter['origin'] == 'proactive' and letter['contact_purpose'] == 'recall'
    contact_id = await carry(5, brief='ignored when a text is given', body='第二封。', title='欢迎')
    [letter] = letters_of(contact_id)
    assert calls == [] and letter['title'] == '欢迎' and letter['reply_text'] == '第二封。'
    assert [ack['status'] for ack in acks] == ['delivered', 'delivered']
asyncio.run(main())
''')


def test_a_fixed_text_is_filed_like_any_letter_she_sent(tmp_path):
    run_isolated(tmp_path, SETUP + r'''
filed = []
server._commit_private_world_letter = lambda row: filed.append(('world', row['letter_id'])) or False
server._schedule_daily_life_exchange = lambda row: filed.append(('life', row['letter_id']))
async def main():
    contact_id = await carry(6, brief=None, body='欢迎你来。')
    [letter] = letters_of(contact_id)
    assert [kind for kind, _ in filed] == ['world', 'life'] and {key for _, key in filed} == {letter['letter_id']}
    assert letter['daily_life_status'] == 'PENDING' and letter['private_world_status'] is not None
asyncio.run(main())
''')


def test_a_contact_that_was_already_published_is_not_written_again(tmp_path):
    run_isolated(tmp_path, SETUP + r'''
async def main():
    contact_id = await carry(7)
    assert len(letters_of(contact_id)) == 1 and len(calls) == 2
    event = {**cloud_contact.parse_event(fields(7)), 'attempts': 2}
    outcome = await server._deliver_contact_letter(event)   # A restart before the report was written.
    assert outcome.status == 'delivered' and len(letters_of(contact_id)) == 1 and len(calls) == 2
asyncio.run(main())
''')


def test_a_busy_pipeline_or_a_failing_model_leaves_the_contact_for_later(tmp_path):
    run_isolated(tmp_path, SETUP + r'''
async def main():
    event = {**cloud_contact.parse_event(fields(8)), 'attempts': 1}
    server._conversation_memory_ready_for_reply = lambda: False
    assert (await server._deliver_contact_letter(event)).status == 'busy' and calls == []
    server._conversation_memory_ready_for_reply = lambda: True
    server._history_memory_admin_gate.acquire()
    try:
        assert (await server._deliver_contact_letter(event)).status == 'busy'
    finally:
        server._history_memory_admin_gate.release()
    failing.append(True)
    contact_id = await carry(9)
    [entry] = [e for e in cloud_contact.pending(ROOT_DIR) if e['id'] == contact_id]
    assert entry['attempts'] == 1 and entry['retry_after'] > NOW and letters_of(contact_id) == []
    assert not server._proactive_busy and all(row.get('letter_status') != 'PROCESSING' for row in server.store.letters)
    failing.clear()
    await cloud_contact.tick(ROOT_DIR, NOW + 1000, qq_ready=lambda: False, preferred=lambda: 'letter',
                             deliver=lambda channel, e: server._deliver_contact_letter(e), slot=slot, post=post)
    assert len(letters_of(contact_id)) == 1 and cloud_contact.handled(ROOT_DIR)[contact_id]['status'] == 'delivered'
asyncio.run(main())
''')


PICTURE = {'type': 'photo', 'asset_url': 'https://bside-moon.cn/assets/olivia-intro.jpg', 'bytes': 255817,
           'sha256': 'c3da242abd46793dfcca1f96eb3a09a377f964371cd4d6fd4e4d69d2feb76038'}


def test_a_fixed_picture_appears_on_the_letter_like_a_generated_reply_photo(tmp_path):
    run_isolated(tmp_path, SETUP + r'''
PICTURE = ''' + repr(PICTURE) + r'''
saved = []
async def fetch(asset, media_root):
    assert asset['asset_url'] == PICTURE['asset_url'] and asset['sha256'] == PICTURE['sha256'] and asset['bytes'] == PICTURE['bytes']
    from PIL import Image
    path = Path(media_root) / ('photo-' + asset['sha256'][:32] + '.png')
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (6, 6)).save(path, format='PNG')
    saved.append(path)
    return path
cloud_contact.fetch_photo = fetch
async def main():
    contact_id = await carry(10, brief=None, body='欢迎你来。', media=[PICTURE])
    [letter] = letters_of(contact_id)
    name = 'photo-' + PICTURE['sha256'][:32] + '.png'
    assert letter['image_status'] == 'COMPLETED' and letter['prepared_image'] == str(saved[0])
    assert letter['reply_image_url'] == 'http://127.0.0.1:8123/toy/media/' + name and letter['image_render_mode'] == 'native'
    assert not any(key in letter for key in ('image_plan', 'image_server_request', 'image_generation_binding'))
    assert acks[-1] == {'id': contact_id, 'status': 'delivered', 'channel': 'letter', 'error_code': None}
    # The letter detail shows it the way it shows a generated photo, though the owner's photo switch is off.
    from original_client_letter_contract import serialize_letter_detail, serialize_letter_list
    detail = serialize_letter_detail(letter)
    assert letter['image_reply_settings'] == {'enabled': False}
    assert detail['imageStatus'] == 'COMPLETED' and detail['imageRequestId'] == letter['letter_id']
    assert detail['replyImageUrl'] == letter['reply_image_url'] and detail['imageRenderMode'] == 'native'
    import re
    assert re.fullmatch(r'photo-[a-f0-9]{32}\.png', name)   # The name the photo acknowledgement route accepts.
    assert serialize_letter_list([letter], remaining_today=1, scope='local')['total'] == 1
    # The same letter before the picture is there is waiting for it rather than showing none.
    waiting = {key: value for key, value in letter.items() if key not in ('image_status', 'prepared_image', 'reply_image_url')}
    assert serialize_letter_detail(waiting)['imageStatus'] == 'PLANNING' and 'replyImageUrl' not in serialize_letter_detail(waiting)
asyncio.run(main())
''')


def test_a_picture_that_does_not_match_is_left_out_and_the_letter_stands(tmp_path):
    run_isolated(tmp_path, SETUP + r'''
PICTURE = ''' + repr(PICTURE) + r'''
async def fetch(asset, media_root):
    raise ValueError('CONTACT_ASSET_MISMATCH')
cloud_contact.fetch_photo = fetch
async def main():
    contact_id = await carry(11, brief=None, body='欢迎你来。', media=[PICTURE])
    [letter] = letters_of(contact_id)
    assert letter['letter_status'] == 'COMPLETED' and letter['reply_text'] == '欢迎你来。'
    assert letter['image_status'] == 'SKIPPED' and 'prepared_image' not in letter and 'reply_image_url' not in letter
    assert acks[-1] == {'id': contact_id, 'status': 'delivered', 'channel': 'letter', 'error_code': 'CONTACT_ASSET_MISMATCH'}
    from original_client_letter_contract import serialize_letter_detail
    assert serialize_letter_detail(letter)['imageStatus'] == 'SKIPPED' and 'replyImageUrl' not in serialize_letter_detail(letter)
asyncio.run(main())
''')


def test_letters_carry_pictures_only_and_the_cloud_is_told_what_was_left_out(tmp_path):
    run_isolated(tmp_path, SETUP + r'''
async def main():
    contact_id = await carry(12, brief=None, body='欢迎你来。', media=[{'type': 'video'}, {'type': 'voice'}, {'type': 'video'}])
    [letter] = letters_of(contact_id)
    assert [item['type'] for item in letter['contact_media']] == ['video', 'voice']
    assert acks[-1]['error_code'] == 'MEDIA_DUPLICATE_SKIPPED,MEDIA_VIDEO_UNSUPPORTED,MEDIA_VOICE_UNSUPPORTED'
    assert 'image_status' not in letter and letter['letter_status'] == 'COMPLETED'
asyncio.run(main())
''')


def test_a_generated_photo_needs_the_owners_switch_and_follows_the_clouds_guidance(tmp_path):
    run_isolated(tmp_path, SETUP + r'''
from runtime import image_reply
prepared = []
async def prepare(srv, row, content, text, **kwargs):
    prepared.append((row['letter_id'], image_reply.contact_guidance(row), image_reply.secondary_photo_allowed(row)))
    row['image_status'] = 'COMPLETED'
image_reply.prepare = prepare
async def main():
    photo = {'type': 'photo', 'brief': '窗外的月亮'}
    contact_id = await carry(13, brief=None, body='欢迎你来。', media=[photo])   # Photos are switched off.
    assert prepared == [] and acks[-1]['error_code'] == 'MEDIA_PHOTO_SKIPPED'
    server.video_reply_settings_store = SimpleNamespace(image_snapshot=lambda: {'enabled': True})
    contact_id = await carry(14, brief=None, body='第二封。', media=[photo])
    [letter] = letters_of(contact_id)
    assert prepared == [(letter['letter_id'], '窗外的月亮', True)] and letter['image_reply_settings'] == {'enabled': True}
    assert acks[-1]['error_code'] is None and letter['image_status'] == 'COMPLETED'
asyncio.run(main())
''')


def test_the_clouds_rules_do_not_touch_a_contact_letter(tmp_path):
    run_isolated(tmp_path, SETUP + r'''
proactive_config.adopt({'version': 'v', 'rules': {'max_per_day': 0, 'min_gap_minutes': 1440, 'quiet_hours': ['00:00', '23:59']}})
async def main():
    contact_id = await carry(15, quiet_hours=False)
    [letter] = letters_of(contact_id)
    assert letter['letter_status'] == 'COMPLETED'
    assert server._proactive_gate_reason() in {'attempt_limit', 'quiet_hours'}   # Her own letters are held back by them.
asyncio.run(main())
''')
