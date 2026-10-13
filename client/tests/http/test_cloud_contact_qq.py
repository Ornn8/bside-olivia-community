"""A contact the cloud asks for, carried out on QQ through the same service that sends her other messages."""
import asyncio
from types import SimpleNamespace

import pytest

from runtime import cloud_contact, proactive_config
from runtime.cloud_contact import Outcome
from runtime.personal_chat import backend
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.presentation import CURRENT
from tests.http.test_chat_jev_proactive import environment
from tests.http.test_cloud_contact import ID, NOW, event
from tests.http.test_personal_chat_decision import envelope

TEXT = '今晚的月亮很亮，想先告诉你'
CANDIDATE = dict(version=1, event_id='wake:today', target_date='2026-10-12', event_at='2026-10-12T08:00:00+08:00',
                 event_kind='wake_up', scene_id='bedroom', certainty='live', detail='刚睡醒', location='卧室')


@pytest.fixture(autouse=True)
def built_in_config(monkeypatch):
    monkeypatch.setattr(cloud_contact, 'MEDIA_WAIT_SECONDS', 0)  # An attachment that is not done is reported at once.
    proactive_config.configure(None)
    yield
    proactive_config.configure(None)


def contact(**fields):
    return cloud_contact.parse_event(event(channel='qq', **fields))


def world(monkeypatch, tmp_path, **options):
    """The real writer pipeline with no cloud decision for what she starts herself."""
    seen = []
    def hook(_snapshot):
        seen.append(dict(CURRENT.get()))
    env = environment(monkeypatch, tmp_path, legacy=True, writer_hook=hook, **options)
    env.seen = seen
    env.server.PORT = 8123
    env.server._media_root = lambda: tmp_path / 'media'
    return env


def carry_out(env, event_fields, *, attempt=1):
    parsed = {**event_fields, 'attempts': attempt}
    runtime = {'service': env.service, 'qq_reachable': lambda: True}
    initiative = SimpleNamespace(target=(PersonalMessage('qq', 'bot', 'owner', 'earlier', 'hi'), env.sender))
    async def scenario():
        outcome = await backend._contact_qq(env.server, runtime, initiative, parsed)
        await asyncio.sleep(0)
        return outcome
    return asyncio.run(scenario())


def test_the_brief_reaches_the_writer_and_the_message_goes_out_as_the_cloud_asked(monkeypatch, tmp_path):
    env = world(monkeypatch, tmp_path, body=envelope(text=TEXT, delivery='voice', sticker='linli-29'))
    outcome = carry_out(env, contact(brief='告诉她月亮很亮，别催她回复', qq_format='text'))
    assert outcome == Outcome('delivered', None, True)
    row = env.rows[0]
    assert row['origin'] == 'proactive' and row['contact_id'] == ID and row['contact_purpose'] == 'recall'
    assert row['contact_format'] == 'text' and row['letter_id'].startswith('im-')
    assert row['delivery_status'] == 'DELIVERED' and row['delivered_format'] == 'text'
    assert env.sent == [('text', TEXT)] and env.audio == []
    shown = env.seen[0]['contact']
    assert shown['brief'] == '告诉她月亮很亮，别催她回复' and shown['format'] == 'text' and shown['attachments'] == []
    assert shown['instruction'] == cloud_contact.WRITER_RULE
    assert '告诉她月亮很亮' in str(env.writes[0]) and '不得skip' in str(env.writes[0])
    assert 'sticker_id' not in row  # Nothing she would add herself.


def test_a_voice_contact_is_spoken_with_the_same_pipeline_as_her_own_voice(monkeypatch, tmp_path):
    env = world(monkeypatch, tmp_path, body=envelope(text=TEXT, delivery='text'))
    assert carry_out(env, contact(qq_format='voice')).status == 'delivered'
    assert env.sent[0][0] == 'audio' and env.audio == [TEXT]
    assert env.rows[0]['delivered_format'] == 'audio' and env.rows[0]['delivery_basis'] == 'CLOUD_CONTACT'
    assert env.seen[0]['contact']['format'] == 'voice'


def test_a_voice_contact_falls_back_to_text_and_says_so_when_there_is_no_voice(monkeypatch, tmp_path):
    env = world(monkeypatch, tmp_path, body=envelope(text=TEXT, delivery='voice'))
    env.server._voice_reply_configured = lambda _: False
    outcome = carry_out(env, contact(qq_format='voice'))
    assert outcome.status == 'delivered' and outcome.error_code == 'VOICE_FALLBACK_TEXT'
    assert env.sent == [('text', TEXT)] and env.audio == []


def test_a_voice_that_could_not_be_made_is_sent_as_text_not_dropped(monkeypatch, tmp_path):
    env = world(monkeypatch, tmp_path, body=envelope(text=TEXT, delivery='voice'))
    async def failing(server, text, path, **kwargs):
        raise RuntimeError('PERSONAL_CHAT_TTS_UNAVAILABLE')
    monkeypatch.setattr(backend, 'prepare_chat_audio', failing)
    outcome = carry_out(env, contact(qq_format='voice'))
    assert outcome.status == 'delivered' and outcome.error_code == 'VOICE_FALLBACK_TEXT'
    assert env.sent == [('text', TEXT)] and env.rows[0]['delivery_basis'] == 'VOICE_RENDER_FAILED'


def test_a_fixed_text_is_sent_as_it_is_without_asking_any_model(monkeypatch, tmp_path):
    env = world(monkeypatch, tmp_path)
    body = '欢迎你来。这里很安静，慢慢看就好。'
    outcome = carry_out(env, contact(brief=None, body=body, qq_format='voice', media=[]))
    assert outcome.status == 'delivered' and outcome.error_code is None
    assert env.writes == [] and env.calls == [] and env.audio == []  # The format voice is ignored for a fixed text.
    # It is split the way every reply is: each sentence on its own line, in one message.
    assert env.sent == [('text', '欢迎你来\n这里很安静，慢慢看就好')]
    row = env.rows[0]
    assert row['reply_text'] == '欢迎你来\n这里很安静，慢慢看就好' and row['contact_body'] == body  # Stored as it was shown.
    assert row['delivered_format'] == 'text'
    assert env.committed == [row['letter_id']]  # Filed like any message she sent.


def test_a_generated_photo_needs_the_owners_switch_and_its_absence_is_reported(monkeypatch, tmp_path):
    env = world(monkeypatch, tmp_path, body=envelope(text=TEXT))
    env.server.video_reply_settings_store = SimpleNamespace(image_snapshot=lambda: {'enabled': False})
    env.service.prepare_photo = lambda row, send: backend.prepare_chat_photo(env.server, row, send)
    outcome = carry_out(env, contact(media=[{'type': 'photo', 'brief': '窗外的月亮'}]))
    row = env.rows[0]
    assert outcome == Outcome('delivered', 'MEDIA_PHOTO_SKIPPED', True)
    assert row['image_status'] == 'SKIPPED' and env.sent == [('text', TEXT)]
    assert row['contact_media'] == [{'type': 'photo', 'brief': '窗外的月亮'}]


def test_a_photo_the_cloud_named_is_scheduled_and_one_it_did_not_name_never_is(monkeypatch, tmp_path):
    env = world(monkeypatch, tmp_path, body=envelope(text=TEXT))
    carry_out(env, contact())
    assert env.photos == [] and not env.service.photo_tasks and 'image' not in env.seen[0]['semantic_kinds']
    env = world(monkeypatch, tmp_path / 'with', body=envelope(text=TEXT))
    env.server.video_reply_settings_store = SimpleNamespace(image_snapshot=lambda: {'enabled': True})
    async def scenario():
        outcome_task = asyncio.create_task(backend._contact_qq(
            env.server, {'service': env.service, 'qq_reachable': lambda: True},
            SimpleNamespace(target=(PersonalMessage('qq', 'bot', 'owner', 'earlier', 'hi'), env.sender)),
            {**contact(media=[{'type': 'photo', 'brief': '窗外的月亮'}]), 'attempts': 1}))
        for _ in range(50):
            await asyncio.sleep(0)
        row = env.rows[0]
        row.update(image_status='COMPLETED', image_delivery_status='DELIVERED')
        return await outcome_task
    outcome = asyncio.run(scenario())
    assert outcome.status == 'delivered' and outcome.error_code is None
    assert env.photos == [env.rows[0]['letter_id']]  # The photo step ran after the text, under the existing photo task.
    assert 'image' not in env.seen[0]['semantic_kinds']  # No picture is planned by the writer; the cloud's brief is.


def test_the_photo_guidance_and_the_request_for_one_reach_the_photo_planner():
    from runtime import image_reply
    row = {'contact_id': ID, 'contact_media': [{'type': 'photo', 'brief': '窗外的月亮'}]}
    assert image_reply.secondary_photo_allowed(row) and image_reply.contact_guidance(row) == '窗外的月亮'
    assert image_reply.contact_guidance({**row, 'contact_media': [{'type': 'photo', 'brief': None}]}) is None
    assert not image_reply.secondary_photo_allowed({'contact_id': ID, 'contact_media': [{'type': 'voice', 'brief': None}]})
    assert not image_reply.secondary_photo_allowed({'contact_id': ID, 'contact_media': []})
    assert not image_reply.secondary_photo_allowed({**row, 'degraded_stages': ['writer']})


def fixed_photo(monkeypatch, tmp_path, data, **lie):
    """Serve a picture to the contact's download step; return the media entry that announces it."""
    import hashlib
    import aiohttp
    from tests.http.test_cloud_contact import Download
    class Session:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False
        def get(self, url, **kwargs):
            return Download(data)
    monkeypatch.setattr(aiohttp, 'ClientSession', Session)
    return {'type': 'photo', 'asset_url': 'https://bside-moon.cn/assets/olivia-intro.jpg',
            'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), **lie}


def test_a_fixed_picture_is_checked_and_sent_after_the_text_without_any_generation(monkeypatch, tmp_path):
    from tests.http.test_cloud_contact import png
    env = world(monkeypatch, tmp_path, body=envelope(text=TEXT))
    env.server.video_reply_settings_store = SimpleNamespace(image_snapshot=lambda: {'enabled': False})  # Photos switched off.
    entry = fixed_photo(monkeypatch, tmp_path, png())
    env.service.prepare_photo = lambda row, send: backend.prepare_chat_photo(env.server, row, send)
    delivered = []
    async def photo(row, send):
        delivered.append((row['image_status'], row['prepared_image']))
    env.service.photo = photo
    outcome = carry_out(env, contact(media=[entry]))
    asyncio.run(asyncio.sleep(0))
    assert outcome.status == 'delivered' and outcome.error_code is None
    row = env.rows[0]
    assert row['image_status'] == 'COMPLETED' and row['contact_asset'] is True
    assert row['reply_image_url'] == f'http://127.0.0.1:8123/toy/media/{row["prepared_image"].rsplit(chr(92), 1)[-1].rsplit("/", 1)[-1]}'
    assert delivered and delivered[0][0] == 'COMPLETED'
    assert env.calls == [] and 'image_plan' not in row and 'image_server_request' not in row  # Nothing was planned or ordered.


def test_a_fixed_picture_that_does_not_match_is_left_out_and_the_text_stands(monkeypatch, tmp_path):
    from tests.http.test_cloud_contact import png
    env = world(monkeypatch, tmp_path, body=envelope(text=TEXT))
    entry = fixed_photo(monkeypatch, tmp_path, png(), sha256='c' * 64)
    env.service.prepare_photo = lambda row, send: backend.prepare_chat_photo(env.server, row, send)
    delivered = []
    async def photo(row, send):
        delivered.append(row['image_status'])
    env.service.photo = photo
    outcome = carry_out(env, contact(media=[entry]))
    assert outcome.status == 'delivered' and outcome.error_code == 'CONTACT_ASSET_MISMATCH'
    row = env.rows[0]
    assert row['delivery_status'] == 'DELIVERED' and env.sent == [('text', TEXT)]
    assert row['image_status'] == 'SKIPPED' and 'prepared_image' not in row
    assert not list((tmp_path / 'media').glob('photo-*.png')) if (tmp_path / 'media').exists() else True


def test_a_voice_attachment_follows_the_text_when_the_text_is_a_text(monkeypatch, tmp_path):
    env = world(monkeypatch, tmp_path, body=envelope(text=TEXT))
    sent = []
    async def default_voice(row, send):
        row['default_voice_status'] = 'DELIVERED'
        sent.append(row['reply_text'])
    env.service.default_voice = default_voice
    outcome = carry_out(env, contact(qq_format='text', media=[{'type': 'voice', 'brief': None}]))
    assert outcome == Outcome('delivered', None, True)
    assert env.sent == [('text', TEXT)] and sent == [TEXT]
    # A voice that was asked for and did not come out is reported, the text is not.
    env = world(monkeypatch, tmp_path / 'again', body=envelope(text=TEXT))
    async def broken(row, send):
        row['default_voice_status'] = 'FAILED'
    env.service.default_voice = broken
    outcome = carry_out(env, contact(qq_format='text', media=[{'type': 'voice', 'brief': None}]))
    assert outcome.status == 'delivered' and outcome.error_code == 'MEDIA_VOICE_FAILED'


def test_a_fixed_text_can_follow_with_a_voice_message_of_the_same_words(monkeypatch, tmp_path):
    env = world(monkeypatch, tmp_path)
    spoken = []
    async def default_voice(row, send):
        row['default_voice_status'] = 'DELIVERED'
        spoken.append(row['reply_text'])
    env.service.default_voice = default_voice
    outcome = carry_out(env, contact(brief=None, body='欢迎你来。', media=[{'type': 'voice', 'brief': None}]))
    assert outcome == Outcome('delivered', None, True) and env.writes == []
    assert env.sent == [('text', '欢迎你来')] and spoken == ['欢迎你来']


def test_a_clip_is_offered_only_when_the_cloud_asked_for_one(monkeypatch, tmp_path):
    from runtime.personal_chat import daily_video
    monkeypatch.setattr(daily_video, 'tier_allows_video', lambda store: True)
    for wanted in (False, True):
        env = world(monkeypatch, tmp_path / str(wanted), body=envelope(text=TEXT))
        env.server._daily_video_worker = SimpleNamespace(candidates=lambda: [dict(CANDIDATE)])
        env.server.video_reply_settings_store = SimpleNamespace(image_snapshot=lambda: {'enabled': True}, tier_snapshot=lambda: 'video')
        outcome = carry_out(env, contact(media=[{'type': 'video', 'brief': None}] if wanted else []))
        assert [c['event_id'] for c in env.seen[0]['daily_video_candidates']] == (['wake:today'] if wanted else [])
        assert outcome.status == 'delivered'
        assert outcome.error_code == ('MEDIA_VIDEO_UNAVAILABLE' if wanted else None)  # The writer chose none.


def test_a_clip_the_writer_chose_is_queued_and_not_reported_as_missing(monkeypatch, tmp_path):
    from runtime.personal_chat import daily_video
    monkeypatch.setattr(daily_video, 'tier_allows_video', lambda store: True)
    body = envelope(text=TEXT, daily_video={'event_id': 'wake:today', 'spoken_text': '早安',
                                           'share_text': '给你看看我刚睡醒', 'staging': {
                                               'image_direction': 'soft morning light', 'video_direction': 'blinks slowly'}})
    env = world(monkeypatch, tmp_path, body=body)
    env.server._daily_video_worker = SimpleNamespace(candidates=lambda: [dict(CANDIDATE)])
    env.server.video_reply_settings_store = SimpleNamespace(image_snapshot=lambda: {'enabled': True}, tier_snapshot=lambda: 'video')
    outcome = carry_out(env, contact(media=[{'type': 'video', 'brief': None}]))
    assert outcome == Outcome('delivered', None, True) and env.rows[0]['daily_video_status'] == 'PENDING_ACK'


def test_a_contact_that_could_not_begin_is_not_counted_as_tried(monkeypatch, tmp_path):
    env = world(monkeypatch, tmp_path, body=envelope(text=TEXT))
    runtime = {'service': env.service, 'qq_reachable': lambda: False}
    initiative = SimpleNamespace(target=(PersonalMessage('qq', 'bot', 'owner', 'earlier', 'hi'), env.sender))
    assert asyncio.run(backend._contact_qq(env.server, runtime, initiative, {**contact(), 'attempts': 1})).status == 'busy'
    initiative.target = None
    runtime['qq_reachable'] = lambda: True
    assert asyncio.run(backend._contact_qq(env.server, runtime, initiative, {**contact(), 'attempts': 1})).status == 'busy'
    assert env.rows == [] and env.sent == []


def test_the_owner_writing_meanwhile_leaves_the_contact_for_later(monkeypatch, tmp_path):
    env = world(monkeypatch, tmp_path, body=envelope(text=TEXT))
    runtime = {'service': env.service, 'qq_reachable': lambda: True}
    initiative = SimpleNamespace(target=(PersonalMessage('qq', 'bot', 'owner', 'earlier', 'hi'), env.sender))
    original = env.service.generate
    async def generate(event, row):
        result = await original(event, row)
        env.service.user_revision += 1  # The owner wrote while it was being prepared.
        return result
    env.service.generate = generate
    assert asyncio.run(backend._contact_qq(env.server, runtime, initiative, {**contact(), 'attempts': 1})).status == 'busy'
    assert env.sent == []


def test_a_contact_already_on_its_way_is_never_sent_again(monkeypatch, tmp_path):
    env = world(monkeypatch, tmp_path, body=envelope(text=TEXT))
    first = carry_out(env, contact())
    again = carry_out(env, contact(), attempt=2)
    assert first.status == again.status == 'delivered' and len(env.rows) == 1 and len(env.sent) == 1
    env.rows[0]['delivery_status'] = 'DELIVERY_UNCONFIRMED'
    assert carry_out(env, contact(), attempt=3) == Outcome('failed', 'DELIVERY_UNCONFIRMED', False)
    assert len(env.rows) == 1 and len(env.sent) == 1


def test_a_writer_that_declines_is_a_retry_not_a_message(monkeypatch, tmp_path):
    env = world(monkeypatch, tmp_path, body=envelope(text='', skip=True))
    outcome = carry_out(env, contact())
    assert outcome == Outcome('retry', 'CONTACT_WRITER_SKIPPED', True) and env.sent == []


def test_a_failed_generation_is_reported_with_its_code(monkeypatch, tmp_path):
    env = world(monkeypatch, tmp_path, body='this is not a decision')
    with pytest.raises(Exception):
        carry_out(env, contact())
    assert env.rows[0]['delivery_status'] == 'FAILED' and env.sent == []


def test_her_own_voice_rules_still_hold_for_what_she_starts_herself(monkeypatch, tmp_path):
    """The same service without a contact: voice only, no picture."""
    from runtime.reply.proactive_runtime import unprompted
    assert unprompted({'origin': 'proactive'}) and not unprompted({'origin': 'proactive', 'contact_id': ID})
    assert not unprompted({'origin': 'user'})
    env = world(monkeypatch, tmp_path, body=envelope(text=TEXT, delivery='text'))
    async def scenario():
        await env.service.proactive(PersonalMessage('qq', 'bot', 'owner', 'own', ''), env.sender, eligible=lambda: True)
    asyncio.run(scenario())
    assert env.sent[0][0] == 'audio' and 'contact' not in env.seen[0]


def test_the_cloud_can_let_her_own_qq_message_be_text(monkeypatch, tmp_path):
    proactive_config.adopt({'version': 'v', 'rules': {'initiative_media': {'qq': ['text']}}})
    env = world(monkeypatch, tmp_path, body=envelope(text=TEXT, delivery='voice'))
    asyncio.run(env.service.proactive(PersonalMessage('qq', 'bot', 'owner', 'own', ''), env.sender, eligible=lambda: True))
    assert env.sent == [('text', TEXT)] and env.audio == [] and env.rows[0]['delivery_status'] == 'DELIVERED'
    proactive_config.adopt({'version': 'v', 'rules': {'initiative_media': {'qq': ['audio_speech', 'text']}}})
    env = world(monkeypatch, tmp_path / 'both', body=envelope(text=TEXT, delivery='text'))
    asyncio.run(env.service.proactive(PersonalMessage('qq', 'bot', 'owner', 'own', ''), env.sender, eligible=lambda: True))
    assert env.sent == [('text', TEXT)]  # Both allowed: her own choice stands.
