"""Contact she starts on her own: a voice message on chat channels, never a picture, video or sticker."""
import asyncio
from types import SimpleNamespace

import pytest

from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.presentation import CURRENT
from runtime.personal_chat.service import PersonalChatService
from tests.http.test_chat_jev_proactive import environment, run_contact
from tests.http.test_personal_chat_decision import envelope

TEXT = '读到一句很喜欢的话，想跟你说说'
CANDIDATE = dict(version=1, event_id='wake:today', target_date='2026-10-12', event_at='2026-10-12T08:00:00+08:00',
                 event_kind='wake_up', scene_id='bedroom', certainty='live', detail='刚睡醒', location='卧室')


def media_world(monkeypatch, env):
    """Everything that could attach a picture or a video is switched on and observable."""
    monkeypatch.setenv('OLIVIA_GPU_API_URL', 'https://gpu.invalid')
    monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'synthetic')
    offered = []
    env.server._daily_video_worker = SimpleNamespace(candidates=lambda: offered.append('video') or [dict(CANDIDATE)])
    env.server.video_reply_settings_store = SimpleNamespace(
        image_snapshot=lambda: {'enabled': True}, tier_snapshot=lambda: 'video')
    monkeypatch.setattr('runtime.personal_chat.stickers.choices', lambda *a, **k: offered.append('sticker') or {'linli-29': 'x'})
    return offered


def greedy_body():
    return envelope(text=TEXT, delivery='text', sticker='linli-29',
                    daily_video={'event_id': CANDIDATE['event_id'], 'spoken_text': '早安'})


def watch_presentation(seen):
    def hook(_snapshot):
        seen.append(dict(CURRENT.get()))
    return hook


@pytest.mark.parametrize('legacy', [False, True])
def test_proactive_qq_turn_is_voice_only_and_queues_no_picture_video_or_sticker(monkeypatch, tmp_path, legacy):
    seen = []
    env = environment(monkeypatch, tmp_path, body=greedy_body(), writer_hook=watch_presentation(seen), legacy=legacy)
    offered = media_world(monkeypatch, env)
    run_contact(env)
    row = env.rows[0]
    assert row['delivery_status'] == 'DELIVERED' and row['delivered_format'] == 'audio'
    assert [kind for kind, _ in env.sent] == ['audio'] and env.audio == [TEXT]
    # Nothing was offered to the writer, so nothing could be chosen.
    assert offered == []
    assert seen[0]['daily_video_candidates'] == [] and seen[0]['sticker_choices'] == {}
    assert 'image' not in seen[0]['semantic_kinds'] and 'video_speech' not in seen[0]['semantic_kinds']
    assert '不能承诺本次会附图' in str(env.writes[0])
    # And nothing was queued behind the voice message.
    assert row['daily_video_candidates'] == []
    assert not any(key in row for key in ('sticker_id', 'daily_video_request', 'daily_video_status', 'share_video_event',
                                          'image_status', 'prepared_image', 'image_delivery_status'))
    assert not env.photos and not env.service.photo_tasks
    assert row['requested_format'] == 'voice' and row['delivery_basis'] == 'PROACTIVE_MEDIA_PLAN'


def test_cloud_text_decision_for_a_chat_channel_is_refused_not_sent(monkeypatch, tmp_path):
    env = environment(monkeypatch, tmp_path, medium='text')
    with pytest.raises(RuntimeError, match='JEV_RESPONSE_INVALID'):
        run_contact(env)
    assert len(env.calls) == 1 and not env.writes
    assert not env.sent and not env.audio and not env.photos


def test_chat_contact_asks_the_cloud_for_nothing_but_voice(monkeypatch, tmp_path):
    env = environment(monkeypatch, tmp_path)
    run_contact(env)
    assert env.calls[0]['channel'] == 'qq' and env.calls[0]['available_media'] == ['audio_speech']


def test_without_a_voice_the_cloud_decision_defers_locally_and_nothing_is_sent(monkeypatch, tmp_path):
    env = environment(monkeypatch, tmp_path)
    env.server._voice_reply_configured = lambda _: False
    run_contact(env)
    assert not env.calls and not env.writes and not env.sent and not env.audio and not env.photos
    row = env.rows[0]
    assert row['proactive_decision']['decision']['reason'] == 'no_medium'
    assert row['delivery_status'] == 'SKIPPED' and row['skip_reason'] == 'PROACTIVE_NO_REPLY'


def test_without_the_cloud_decision_a_missing_voice_is_no_contact_not_a_text(monkeypatch, tmp_path):
    env = environment(monkeypatch, tmp_path, legacy=True)
    env.server._voice_reply_configured = lambda _: False
    run_contact(env)
    assert not env.writes and not env.sent and not env.audio and not env.photos
    assert env.rows[0]['delivery_status'] == 'SKIPPED' and env.rows[0]['skip_reason'] == 'PROACTIVE_NO_REPLY'


def test_without_the_cloud_decision_the_writer_cannot_choose_text(monkeypatch, tmp_path):
    env = environment(monkeypatch, tmp_path, body=envelope(text=TEXT, delivery='text'), legacy=True)
    run_contact(env)
    assert [kind for kind, _ in env.sent] == ['audio'] and env.audio == [TEXT]


def test_auxiliary_recovery_text_is_not_sent_as_her_own_contact(monkeypatch, tmp_path):
    env = environment(monkeypatch, tmp_path)
    from runtime.personal_chat import backend
    original = backend.persist_chat
    async def degrade(server):
        for row in server.store.personal_chats:
            if row.get('origin') == 'proactive' and row.get('proactive_decision'):
                row['degraded_stages'] = ['recall']
        await original(server)
    monkeypatch.setattr(backend, 'persist_chat', degrade)
    run_contact(env)
    assert len(env.writes) == 1 and not env.sent and not env.audio
    assert env.rows[0]['delivery_status'] == 'SKIPPED'


def test_a_lost_voice_upload_is_never_sent_as_the_text_of_her_own_contact():
    async def scenario():
        rows, sent = [], []
        async def generate(event, row):
            row.update(origin='proactive', prepared_audio='synthetic.wav', requested_format='voice')
            return TEXT
        class Sender:
            async def __call__(self, text): sent.append(('text', text))
            async def audio(self, path): sent.append(('audio', path))
            async def prepare_audio(self, path): raise RuntimeError('synthetic upload failure')
        async def commit(row): raise AssertionError('a skipped contact cannot commit')
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('bot', 'owner')})
        await service.proactive(PersonalMessage('qq', 'bot', 'owner', 'check', ''), Sender())
        assert sent == []
        assert rows[0]['delivery_status'] == 'SKIPPED' and rows[0]['skip_reason'] == 'PROACTIVE_NO_REPLY'
    asyncio.run(scenario())


def test_only_contact_she_starts_is_unprompted_never_a_reply():
    from runtime.reply.proactive_runtime import unprompted
    assert unprompted({'origin': 'proactive'}) is True
    assert unprompted({}) is False and unprompted({'origin': 'user', 'channel': 'qq'}) is False


def test_a_reply_to_the_owner_is_offered_every_medium_and_may_fall_back_to_text(monkeypatch, tmp_path):
    seen = []
    env = environment(monkeypatch, tmp_path, legacy=True, writer_hook=watch_presentation(seen),
                      body=envelope(text='在呢', delivery='text'))
    offered = media_world(monkeypatch, env)
    async def unavailable(*args, **kwargs):
        raise RuntimeError('synthetic voice failure')
    monkeypatch.setattr('runtime.personal_chat.backend.prepare_chat_audio', unavailable)
    async def scenario():
        await env.service.handle(PersonalMessage('qq', 'bot', 'owner', 'm1', '在吗'), env.sender)
        await asyncio.gather(*env.service.consumer_tasks.values())
    asyncio.run(scenario())
    assert offered == ['video', 'sticker']
    assert seen[0]['daily_video_candidates'] == [CANDIDATE] and seen[0]['sticker_choices'] == {'linli-29': 'x'}
    assert {'image', 'video_speech'} <= set(seen[0]['semantic_kinds'])
    assert env.rows[0]['delivery_status'] == 'DELIVERED'


def photo_row(**values):
    return {'letter_id': 'x', 'channel': 'qq', 'delivery_status': 'DELIVERED', 'reply_mode': 'future_im',
            'image_reply_settings': {'enabled': True}, 'reply_text': '早', 'content': '', **values}


def test_no_photo_step_runs_for_her_own_contact_but_replies_keep_theirs():
    from runtime.image_reply import secondary_photo_allowed
    assert secondary_photo_allowed(photo_row(origin='proactive')) is False
    assert secondary_photo_allowed(photo_row(origin='proactive', proactive_decision=None)) is False
    assert secondary_photo_allowed(photo_row()) is True

    async def scenario(row):
        started = []
        async def photo(row, send): started.append(row['letter_id'])
        class Sender:
            async def image(self, path): raise AssertionError('no picture is sent here')
        service = PersonalChatService([row], lambda: None, None, None, {'qq': ('bot', 'owner')}, photo=photo)
        service._schedule_photo(row, Sender())
        await asyncio.gather(*service.photo_tasks.values())
        return started
    assert asyncio.run(scenario(photo_row(origin='proactive'))) == []
    assert asyncio.run(scenario(photo_row())) == ['x']


def test_image_preparation_marks_her_own_contact_skipped_instead_of_generating(monkeypatch):
    from runtime import image_reply
    saved = []
    server = SimpleNamespace(_persist_store_state=lambda: saved.append(True), media_tasks=set())
    monkeypatch.setattr(image_reply, '_prepare_once', lambda *a, **k: (_ for _ in ()).throw(AssertionError('generated')))
    row = photo_row(origin='proactive')
    async def scenario():
        await image_reply.prepare(server, row, '', '早', channel='qq')
        letter = {**row, 'channel': 'letter', 'reply_mode': 'text', 'content': ''}
        letter.pop('image_status')
        image_reply.schedule(server, letter)  # The letter path: a task would be created if it were allowed.
        assert not server.media_tasks and 'image_status' not in letter
    asyncio.run(scenario())
    assert row['image_status'] == 'SKIPPED' and saved



def test_a_proactive_letter_is_plain_text_with_no_media_even_with_every_media_option_on(tmp_path):
    from tests.http.test_expression_context_routes import run_isolated
    from tests.http.test_proactive_expression_context import SETUP
    run_isolated(tmp_path, SETUP + r'''
server.video_reply_settings_store = SimpleNamespace(
    image_snapshot=lambda: {'enabled': True, 'resolution': '1K'}, tier_snapshot=lambda: 'video')
async def no_media(*args, **kwargs):
    raise AssertionError('A letter she starts on her own is never rendered as media.')
server._render_media_job = no_media
scheduled = []
import runtime.image_reply as image_reply
real_schedule = image_reply.schedule
image_reply.schedule = lambda owner, row: scheduled.append(row['letter_id']) or real_schedule(owner, row)
async def main():
    turn = await server._prepare_proactive_turn(intent, now=NOW)
    # Even a plan asking for a voice letter is written and published as plain text.
    await server._publish_proactive(intent, {'decision': 'send', 'format': 'voice', 'title': '问候'}, turn=turn)
    letter = server.store.letters[-1]
    assert letter['origin'] == 'proactive' and letter['letter_status'] == 'COMPLETED'
    assert letter['reply_mode'] == 'text' and letter['media_status'] == 'NOT_REQUESTED'
    assert letter['reply_video_enabled'] is False and letter['reply_text'] == '慢慢来就好。'
    assert not any(key in letter for key in ('reply_audio_url', 'media_deliveries', 'image_status',
                                             'reply_sticker_id', 'prepared_image', 'image_reply_settings'))
    wire = '\n'.join(m['content'] for m in calls[-1][0])
    assert '按平常文字信写' in wire and '简短语音' not in wire
    assert scheduled == []
    # Neither the photo scheduler nor the startup recovery pass may attach a picture to it later.
    letter['image_reply_settings'] = {'enabled': True}
    for completed in server.store.letters:
        if completed.get('letter_status') == 'COMPLETED':
            image_reply.schedule(server, completed)
    assert not server.media_tasks and 'image_status' not in letter
asyncio.run(main())
''')
