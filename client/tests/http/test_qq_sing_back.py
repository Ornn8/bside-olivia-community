"""Singing a voice message back, from a QQ chat: the voice he sang or hummed is kept (audio, length, what was heard in
it), shown to the writer as facts, the writer's optional ``sing_back`` field is checked, the order is the existing cloud
cover with the a cappella marker, the mp3 goes back like a cover, and every failure is told once with reason and code.
Covers keep behaving as they did."""
import asyncio
import base64
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
import time
from types import SimpleNamespace
import wave

import pytest

from runtime import incoming_media
from runtime.cloud_service import CloudError
from runtime.media.song_sources import KEEP_SECONDS, MAX_KEPT_VOICES, SongSources, VoiceSources
from runtime.personal_chat import backend, cover_chat, cover_delivery, decision, song_intake
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import _clear_draft
from tests.http.test_chat_jev_decision import server_fixture, pipeline_result
from tests.http.test_personal_chat_decision import envelope
from tests.http.test_qq_cover_chat_wiring import covers
from tests.http.test_qq_cover_delivery import REAL_ORDER, Send, asked_row, run_service, task_failed, world

SUNG = '两只老虎两只老虎跑得快跑得快'
HUM = '啦啦啦啦啦'
BINDING = PersonalMessage('qq', 'b', 'u', '', '').binding_id


def wav_bytes(seconds):
    output = io.BytesIO()
    with wave.open(output, 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x01\x00' * int(8000 * seconds))
    return output.getvalue()


def server_in(tmp_path):
    return SimpleNamespace(_local_data_root=lambda: tmp_path, _persist_store_state=lambda: None)


def keep(server, tmp_path, name, *, seconds=8, heard=SUNG, row=None, channel='qq'):
    """One voice message arriving as ``name``; returns the turn's row."""
    row = {'letter_id': 'turn-' + name, 'channel': 'qq'} if row is None else row
    recording = tmp_path / (name + '.bin')
    recording.write_bytes(wav_bytes(seconds))
    event = PersonalMessage(channel, 'b', 'u', name, '[语音]')
    asyncio.run(song_intake.keep_voice(server, event, row, recording, name, heard))
    return row


def offered_for(server, row, checked=True):
    return asyncio.run(cover_chat.voices_offered(server, row, checked=checked))


# --- keeping the voice ----------------------------------------------------------------------------------------

def test_a_voice_he_sent_is_kept_with_its_length_and_what_was_heard(tmp_path):
    server = server_in(tmp_path)
    row = keep(server, tmp_path, 'a', seconds=8)
    [kept] = VoiceSources(tmp_path).unused()
    assert row['voice_arrivals'] == [{'source': kept['id'], 'seconds': 8.0}]
    assert kept['seconds'] == 8.0 and kept['heard'] == SUNG and kept['words'] is True and kept['origin'] == 'voice'
    assert (tmp_path / 'cover-inputs' / kept['id'] / 'source.wav').read_bytes() == wav_bytes(8)
    assert (tmp_path / 'cover-inputs' / 'chat-voices.json').is_file()
    assert SongSources(tmp_path).unused() == []                  # a voice is not a song to cover


def test_the_same_message_is_kept_once(tmp_path):
    server = server_in(tmp_path)
    row = keep(server, tmp_path, 'a')
    keep(server, tmp_path, 'a', row=row)
    assert len(VoiceSources(tmp_path).unused()) == 1 and len(row['voice_arrivals']) == 1


@pytest.mark.parametrize('seconds', [301, 400])
def test_a_voice_longer_than_five_minutes_is_never_kept(tmp_path, seconds):
    row = keep(server_in(tmp_path), tmp_path, 'long', seconds=seconds)
    assert 'voice_arrivals' not in row and VoiceSources(tmp_path).unused() == []
    assert not (tmp_path / 'cover-inputs').exists()


def test_a_minute_exactly_is_kept(tmp_path):
    keep(server_in(tmp_path), tmp_path, 'a', seconds=60)
    assert VoiceSources(tmp_path).unused()[0]['seconds'] == 60.0


def test_only_the_last_five_unused_voices_stay_and_old_ones_go_after_three_days(tmp_path):
    server = server_in(tmp_path)
    for index in range(MAX_KEPT_VOICES + 2):
        keep(server, tmp_path, f'v{index}', seconds=4, heard=SUNG + str(index))
    kept = VoiceSources(tmp_path).unused(limit=20)
    assert len(kept) == MAX_KEPT_VOICES
    assert {row['heard'] for row in kept} == {SUNG + str(index) for index in range(2, 7)}     # the newest five
    folders = {path.parent.name for path in (tmp_path / 'cover-inputs').glob('*/source.wav')}
    assert folders == {row['id'] for row in kept}                                             # the others' audio is gone
    store = VoiceSources(tmp_path)
    old = store.unused(limit=1)[0]
    assert store.cleanup(now=time.time() + KEEP_SECONDS + 5) and store.unused(limit=20, now=time.time()) == []
    assert not (tmp_path / 'cover-inputs' / old['id']).exists()


def test_a_voice_on_another_channel_in_a_proactive_turn_or_with_the_feature_off_is_not_kept(tmp_path, monkeypatch):
    server = server_in(tmp_path)
    keep(server, tmp_path, 'other', channel='other')
    keep(server, tmp_path, 'proactive', row={'letter_id': 'p', 'channel': 'qq', 'origin': 'proactive'})
    keep(server, tmp_path, 'contact', row={'letter_id': 'c', 'channel': 'qq', 'contact_id': 'x'})
    monkeypatch.setattr(cover_chat, 'ENABLED', False)
    keep(server, tmp_path, 'off')
    assert VoiceSources(tmp_path).unused() == [] and not (tmp_path / 'cover-inputs').exists()


def test_a_recording_that_cannot_be_read_is_skipped_and_the_turn_goes_on(tmp_path):
    server = server_in(tmp_path)
    recording = tmp_path / 'bad.bin'
    recording.write_bytes(b'not audio at all')
    row = {'letter_id': 't', 'channel': 'qq'}
    asyncio.run(song_intake.keep_voice(server, PersonalMessage('qq', 'b', 'u', 'x', '[语音]'), row, recording, 'bad', SUNG))
    assert 'voice_arrivals' not in row and VoiceSources(tmp_path).unused() == []
    assert not (tmp_path / 'cover-inputs').exists()


def test_a_voice_arriving_through_the_media_step_is_observed_then_kept(tmp_path, monkeypatch):
    async def resolve(kind, reference):
        assert kind == 'audio' and reference == 'a' * 32
        return {'base64': base64.b64encode(wav_bytes(6)).decode()}

    async def describe(server, path, kind, name, operation_id):
        return {'kind': 'audio', 'summary': SUNG, 'source': 'user', 'sha256': 'x', 'evidence_kind': 'media_observation',
                'observed_at': datetime.now(timezone.utc).isoformat()}
    monkeypatch.setattr(incoming_media, 'describe_media', describe)
    server = SimpleNamespace(_state_root=lambda: tmp_path, _local_data_root=lambda: tmp_path, _persist_store_state=lambda: None)
    event = PersonalMessage('qq', 'b', 'u', '1', '[语音]', media=(('1', 'audio', 'a' * 32, ''),))
    row = {'letter_id': 'turn', 'channel': 'qq', 'incoming_media': [list(part) for part in event.media]}
    token = incoming_media.MEDIA_RESOLVER.set(resolve)
    try:
        asyncio.run(incoming_media.understand_incoming(server, event, row))
        asyncio.run(incoming_media.understand_incoming(server, event, row))     # a retried turn keeps nothing twice
    finally:
        incoming_media.MEDIA_RESOLVER.reset(token)
    [kept] = VoiceSources(tmp_path).unused()
    assert kept['heard'] == SUNG and kept['seconds'] == 6.0 and row['voice_arrivals'][0]['source'] == kept['id']
    assert len(row['voice_arrivals']) == 1 and row['incoming_media_status'] == 'COMPLETED'


def test_a_voice_that_could_not_be_observed_is_not_kept(tmp_path, monkeypatch):
    async def resolve(kind, reference):
        return {'base64': base64.b64encode(wav_bytes(6)).decode()}

    async def describe(*args):
        raise TimeoutError()
    monkeypatch.setattr(incoming_media, 'describe_media', describe)
    server = SimpleNamespace(_state_root=lambda: tmp_path, _local_data_root=lambda: tmp_path, _persist_store_state=lambda: None)
    event = PersonalMessage('qq', 'b', 'u', '1', '[语音]', media=(('1', 'audio', 'a' * 32, ''),))
    row = {'letter_id': 'turn', 'channel': 'qq', 'incoming_media': [list(part) for part in event.media]}
    token = incoming_media.MEDIA_RESOLVER.set(resolve)
    try:
        asyncio.run(incoming_media.understand_incoming(server, event, row))
    finally:
        incoming_media.MEDIA_RESOLVER.reset(token)
    assert VoiceSources(tmp_path).unused() == [] and row['incoming_media_failed'] == 1


@pytest.mark.parametrize('heard,expected', [
    (SUNG, True), ('你好，蓝色笔记本十七本。', True), ('two tigers two tigers running fast', True),
    (HUM, False), ('嗯嗯嗯嗯嗯嗯', False), ('la la la la la', False), ('（哼唱）', False), ('[humming melody]', False),
    ('', False), (None, False), ('。。。', False)])
def test_what_was_heard_counts_as_words_by_its_shape_not_by_a_word_list(heard, expected):
    assert cover_chat.has_words(heard) is expected


# --- what the writer is shown ---------------------------------------------------------------------------------

def test_this_turns_voice_comes_first_and_only_recent_unused_ones_follow(tmp_path):
    server = server_in(tmp_path)
    keep(server, tmp_path, 'old', heard='很久以前唱的一段歌')
    store = VoiceSources(tmp_path)
    rows = json.loads((tmp_path / 'cover-inputs' / 'chat-voices.json').read_text(encoding='utf-8'))
    rows[0]['created_at'] = time.time() - cover_chat.VOICE_RECENT_SECONDS - 60
    (tmp_path / 'cover-inputs' / 'chat-voices.json').write_text(json.dumps(rows), encoding='utf-8')
    keep(server, tmp_path, 'recent', heard='刚才唱的两句歌')
    row = keep(server, tmp_path, 'now', heard=HUM, seconds=5)
    offered = offered_for(server, row)
    assert [(item['arrived'], item['heard']) for item in offered] == [('this_turn', HUM), ('earlier', '刚才唱的两句歌')]
    this_turn = offered[0]
    assert this_turn['seconds'] == 5 and this_turn['words'] is False and len(this_turn['id']) == 8
    assert this_turn['voice'].startswith(this_turn['id']) and store.get(this_turn['voice'])
    assert row['sing_back_voices'] == offered and offered_for(server, row, checked=False) == offered   # frozen on the row


def test_a_voice_already_sung_back_or_one_the_cloud_cannot_take_is_not_offered(tmp_path):
    server = server_in(tmp_path)
    row = keep(server, tmp_path, 'a')
    VoiceSources(tmp_path).mark_used(row['voice_arrivals'][0]['source'])
    assert offered_for(server, row) == []
    fresh = keep(server, tmp_path, 'b')
    assert offered_for(server, fresh, checked=False) == []


def test_at_most_three_voices_are_offered(tmp_path):
    server = server_in(tmp_path)
    for index in range(4):
        row = keep(server, tmp_path, f'v{index}', heard=SUNG + str(index))
    assert len(offered_for(server, row)) == cover_chat.VOICE_OFFER_LIMIT


def test_the_note_has_facts_only_and_says_when_there_are_no_real_words(tmp_path):
    server = server_in(tmp_path)
    row = keep(server, tmp_path, 'hum', heard=HUM, seconds=7)
    offered_for(server, row)
    note = cover_chat.voice_context(row)
    assert note.startswith('\n[系统语音，非用户原话]\n')
    body = json.loads(note.split('\n', 2)[2])
    assert body['voices'] == [{'id': row['sing_back_voices'][0]['id'], 'seconds': 7, 'heard': HUM, 'real_words': False,
                               'arrived': 'this_turn'}]
    assert '不是用户原话' in body['meaning'] and 'lyrics' in body['meaning'] and '3秒到5分钟' in body['meaning']
    assert cover_chat.voice_context({}) == '' and cover_chat.voice_context({'sing_back_voices': []}) == ''


def test_the_guidance_teaches_by_meaning_and_travels_only_with_a_voice(tmp_path):
    row = keep(server_in(tmp_path), tmp_path, 'a')
    offered_for(server_in(tmp_path), row)
    body = json.loads(cover_chat.voice_context(row).split('\n', 2)[2])
    guidance = body['sing_back']
    assert 'what=' in guidance and 'not_for=' in guidance and 'examples=' in guidance
    assert '婉拒' in guidance and 'cover' in guidance and '原创' in guidance and 'lyrics' in guidance
    assert 'sing_back' in decision.OPTIONAL_FIELDS and 'sing_back' not in decision.NEUTRAL_METADATA
    assert 'sing_back' not in decision.INSTRUCTION         # no turn without a voice message carries any of it
    assert cover_chat.voice_context({'sing_back_voices': []}) == ''


# --- the writer's field ---------------------------------------------------------------------------------------

def offered_pair():
    """A voice with words and a hum, as the writer was shown them."""
    return [{'id': 'aaaa1111', 'voice': 'a' * 8 + '1' * 8 + '0' * 16, 'seconds': 8, 'heard': SUNG, 'words': True, 'arrived': 'this_turn'},
            {'id': 'bbbb2222', 'voice': 'b' * 8 + '2' * 8 + '0' * 16, 'seconds': 6, 'heard': HUM, 'words': False, 'arrived': 'this_turn'}]


def decode(sing_back=None, voices=None, **options):
    extra = {} if sing_back is None else {'sing_back': sing_back}
    return decision.decode(envelope(**extra), user='[语音]', now=time.time(),
                           sing_back_voices=offered_pair() if voices is None else voices, **options)


def test_an_offered_voice_becomes_a_request_and_the_field_is_removed():
    result = decode({'voice': 'aaaa1111'})
    assert result['sing_back_request'] == {'source': 'a' * 8 + '1' * 8 + '0' * 16, 'title': '', 'sing_back': True, 'lyrics': ''}
    assert 'sing_back' not in result and 'dropped_cover' not in result and 'cover_request' not in result


def test_a_hum_keeps_the_lyrics_she_wrote_as_plain_lines():
    result = decode({'voice': 'bbbb2222', 'lyrics': '  今晚的月亮  \r\n\n[副歌] 陪我慢慢唱~\n**风**轻轻地吹  '})
    assert result['sing_back_request']['lyrics'] == '今晚的月亮\n副歌 陪我慢慢唱\n风 轻轻地吹'


def test_lyrics_for_a_recording_that_had_words_are_dropped_but_the_request_stays():
    result = decode({'voice': 'aaaa1111', 'lyrics': '我自己写的几句词'})
    assert result['sing_back_request']['lyrics'] == '' and 'dropped_cover' not in result


@pytest.mark.parametrize('field,reason', [
    ({'voice': 'cccc3333'}, 'SING_BACK_VOICE_NOT_OFFERED'),
    ({'voice': 'aaaa1111' + '0' * 24}, 'SING_BACK_VOICE_NOT_OFFERED'),          # only the short id the writer was shown
    ({'voice': 7}, 'SING_BACK_REQUEST_SHAPE'),
    ({'lyrics': '啦啦'}, 'SING_BACK_REQUEST_SHAPE'),
    ({'voice': 'bbbb2222', 'extra': 1}, 'SING_BACK_REQUEST_SHAPE'),
    ('bbbb2222', 'SING_BACK_REQUEST_SHAPE'),
    ({'voice': 'bbbb2222', 'lyrics': 12}, 'SING_BACK_LYRICS_INVALID'),
    ({'voice': 'bbbb2222', 'lyrics': '词' * 401}, 'SING_BACK_LYRICS_TOO_LONG')])
def test_an_invalid_field_is_dropped_and_the_reply_still_goes_out(field, reason):
    result = decode(field)
    assert result['text'] == '好呀'                       # the reply itself stands
    assert 'sing_back_request' not in result and result['dropped_cover'] == reason


def test_lyrics_up_to_the_limit_are_accepted():
    result = decode({'voice': 'bbbb2222', 'lyrics': '词' * cover_chat.LYRICS_LIMIT})
    assert len(result['sing_back_request']['lyrics']) == cover_chat.LYRICS_LIMIT


def test_nothing_offered_a_proactive_turn_or_a_skipped_reply_drops_the_field():
    assert decode({'voice': 'bbbb2222'}, voices=())['dropped_cover'] == 'SING_BACK_VOICE_NOT_OFFERED'
    result = decision.decode(envelope(sing_back={'voice': 'bbbb2222'}), user='', now=time.time(),
                             sing_back_voices=offered_pair(), proactive=True)
    assert 'sing_back_request' not in result and result['dropped_cover'] == 'SING_BACK_CONTEXT'


def test_one_order_per_reply_a_cover_wins_over_a_sing_back():
    cover = {'source': 'abcd1234', 'evidence': '你翻唱一下'}
    songs = [{'id': 'abcd1234', 'source': 'abcd1234' + '0' * 24, 'title': '晴天'}]
    result = decision.decode(envelope(cover=cover, sing_back={'voice': 'bbbb2222'}), user='你翻唱一下', now=time.time(),
                             cover_sources=songs, sing_back_voices=offered_pair())
    assert result['cover_request']['title'] == '晴天' and 'sing_back_request' not in result
    assert result['dropped_cover'] == 'SING_BACK_WITH_COVER'


def test_a_cover_cannot_name_a_voice_and_a_sing_back_cannot_name_a_song():
    result = decision.decode(envelope(cover={'source': 'bbbb2222', 'evidence': '你翻唱一下'}), user='你翻唱一下', now=time.time(),
                             cover_sources=(), sing_back_voices=offered_pair())
    assert 'cover_request' not in result and result['dropped_cover'] == 'COVER_SOURCE_NOT_OFFERED'
    songs = [{'id': 'abcd1234', 'source': 'abcd1234' + '0' * 24, 'title': '晴天'}]
    result = decision.decode(envelope(sing_back={'voice': 'abcd1234'}), user='[语音]', now=time.time(), cover_sources=songs)
    assert 'sing_back_request' not in result and result['dropped_cover'] == 'SING_BACK_VOICE_NOT_OFFERED'


def test_a_reply_without_the_field_and_a_cover_as_before_are_unchanged():
    plain = decode()
    assert not {'sing_back_request', 'dropped_cover', 'cover_request'} & set(plain)
    songs = [{'id': 'abcd1234', 'source': 'abcd1234' + '0' * 24, 'title': '晴天'}]
    cover = decision.decode(envelope(cover={'source': 'abcd1234', 'evidence': '你翻唱一下'}), user='你翻唱一下', now=time.time(),
                            cover_sources=songs)
    assert cover['cover_request'] == {'source': 'abcd1234' + '0' * 24, 'title': '晴天', 'evidence': '你翻唱一下'}
    assert 'sing_back_request' not in cover


# --- inside a QQ turn ------------------------------------------------------------------------------------------

def voice_event(text='[语音]'):
    return PersonalMessage('qq', 'b', 'u', '1', text, media=(('1', 'audio', 'a' * 32, ''),))


def voice_turn(monkeypatch, covers, heard, seconds, answer):
    """A whole turn whose message is a voice: media read and kept, the writer shown the voice and answering with ``answer``."""
    server, row, seen, _, _ = server_fixture(monkeypatch, covers.tmp, pipeline_result(text='{}'))
    server._local_data_root = lambda: covers.tmp
    row['letter_id'] = 'turn-1'
    event = voice_event()
    row['incoming_media'] = [list(part) for part in event.media]

    async def resolve(kind, reference):
        return {'base64': base64.b64encode(wav_bytes(seconds)).decode()}

    async def describe(server, path, kind, name, operation_id):
        return {'kind': 'audio', 'summary': heard, 'source': 'user', 'sha256': 'x', 'evidence_kind': 'media_observation',
                'observed_at': datetime.now(timezone.utc).isoformat()}
    monkeypatch.setattr(incoming_media, 'describe_media', describe)

    async def run(request, context):
        from runtime.personal_chat.presentation import CURRENT
        shown = CURRENT.get()
        seen.append(deepcopy(shown))
        return pipeline_result(text=envelope(text='好呀，我也唱给你听。', sing_back=answer(shown['sing_back_voices'])))
    server.reply_pipeline.run = run

    async def scenario():
        token = incoming_media.MEDIA_RESOLVER.set(resolve)
        try:
            return await backend.generate(server, event, row)
        finally:
            incoming_media.MEDIA_RESOLVER.reset(token)
    return asyncio.run(scenario()), row, seen


def test_a_sung_voice_reaches_the_writer_and_her_request_is_kept_on_the_row(monkeypatch, covers):
    text, row, seen = voice_turn(monkeypatch, covers, SUNG, 9, lambda voices: {'voice': voices[0]['id']})
    shown = seen[0]
    [offered] = shown['sing_back_voices']
    assert offered['arrived'] == 'this_turn' and offered['seconds'] == 9 and offered['words'] is True
    assert '系统语音' in shown['incoming_observation_context'] and SUNG in shown['incoming_observation_context']
    assert shown['cover_sources'] == [] and '系统歌曲' not in shown['incoming_observation_context']
    assert text == '好呀，我也唱给你听。'
    assert row['cover_request'] == {'source': offered['voice'], 'title': '', 'sing_back': True, 'lyrics': ''}
    assert row['cover_status'] == 'PENDING' and 'cover_request_dropped' not in row


def test_a_hum_keeps_the_lyrics_she_wrote_through_the_whole_turn(monkeypatch, covers):
    text, row, seen = voice_turn(monkeypatch, covers, HUM, 6,
                                 lambda voices: {'voice': voices[0]['id'], 'lyrics': '今晚的月亮\n陪我慢慢唱'})
    assert seen[0]['sing_back_voices'][0]['words'] is False
    assert row['cover_request']['lyrics'] == '今晚的月亮\n陪我慢慢唱'


def test_a_voice_that_is_only_talk_can_be_left_alone_and_nothing_is_ordered(monkeypatch, covers):
    server, row, seen, _, _ = server_fixture(monkeypatch, covers.tmp, pipeline_result(text=envelope(text='在呀，刚吃完饭。')))
    server._local_data_root = lambda: covers.tmp
    text = asyncio.run(backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', '你在干嘛呀'), row))
    assert text == '在呀，刚吃完饭。' and 'cover_request' not in row and seen[0]['sing_back_voices'] == []
    assert '系统语音' not in seen[0]['incoming_observation_context']


def test_a_refused_field_is_noted_for_the_next_turn_like_a_refused_cover(monkeypatch, covers):
    text, row, seen = voice_turn(monkeypatch, covers, SUNG, 9, lambda voices: {'voice': 'ffffffff'})
    assert text == '好呀，我也唱给你听。' and 'cover_request' not in row
    assert row['cover_request_dropped'] == 'SING_BACK_VOICE_NOT_OFFERED'
    later = {'letter_id': 'turn-2', 'channel': 'qq', 'binding_id': BINDING}
    server = SimpleNamespace(store=SimpleNamespace(personal_chats=[dict(row, binding_id=BINDING, created_at=time.time()), later]))
    [order] = cover_chat.in_flight(server, later)
    assert order == {'what': '清唱', 'state': '没有成功下单', 'code': 'SING_BACK_VOICE_NOT_OFFERED'}
    later['voice_arrivals'] = [{'source': 'x'}]                     # a new voice this turn: judged afresh
    assert cover_chat.in_flight(server, later) == []


def test_with_the_feature_off_no_voice_is_kept_or_shown(monkeypatch, covers):
    monkeypatch.setattr(cover_chat, 'ENABLED', False)
    server, row, seen, _, _ = server_fixture(monkeypatch, covers.tmp, pipeline_result())
    server._local_data_root = lambda: covers.tmp
    asyncio.run(backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', '你好'), row))
    assert seen[0]['sing_back_voices'] == [] and '系统语音' not in seen[0]['incoming_observation_context']


def test_a_new_draft_forgets_the_voices_shown_but_not_the_ones_that_arrived():
    row = {'content': 'new', 'sing_back_voices': [{}], 'cover_request': {'source': 'x', 'sing_back': True},
           'cover_status': 'PENDING', 'voice_arrivals': [{'source': 'a'}]}
    _clear_draft(row)
    assert row == {'content': 'new', 'voice_arrivals': [{'source': 'a'}]}


# --- the order and the delivery -------------------------------------------------------------------------------

class Cloud:
    """The cloud call every cover uses, recorded."""

    def __init__(self):
        self.calls = []

    def generate(self, kind, data, output, *, environment=None, assets=None):
        self.calls.append(dict(kind=kind, data=deepcopy(data), output=output, assets={k: v.read_bytes() for k, v in (assets or {}).items()}))
        output.write_bytes(b'RIFFsung')
        return {'remote_task_id': 't1'}


@pytest.fixture
def cloud(world, monkeypatch):
    recorder = Cloud()
    monkeypatch.setattr('runtime.remote_pipeline.generate', recorder.generate)
    return recorder


def sang_row(world, letter_id='turn-1', *, seconds=8, heard=SUNG, lyrics=None):
    """A turn in which he sent a voice and she answered with a sing_back field, decoded as the backend does."""
    server = world.server
    row = {'letter_id': letter_id, 'channel': 'qq', 'delivery_status': 'DELIVERED', 'letter_status': 'COMPLETED',
           'binding_id': BINDING}
    keep(server, world.tmp, letter_id, seconds=seconds, heard=heard, row=row)
    offered = offered_for(server, row)
    field = {'voice': offered[0]['id'], **({} if lyrics is None else {'lyrics': lyrics})}
    written = decision.decode(envelope(text='好呀，我唱给你听。', sing_back=field), user='[语音]', now=time.time(),
                              sing_back_voices=offered)
    row.update(cover_request=written['sing_back_request'], cover_status=cover_chat.PENDING)
    world.rows.append(row)
    return row


def test_a_voice_with_words_is_ordered_as_the_usual_cloud_cover_with_the_marker_and_no_lyrics(world, cloud):
    row = sang_row(world)
    send = Send()
    run_service(world, row, send)
    [call] = cloud.calls
    assert call['kind'] == 'cover' and call['data']['lyrics'] == '' and call['data']['language'] == 'unknown'
    assert call['assets'] == {'source_asset': wav_bytes(8)}                       # the kept recording itself
    options = call['data']['cover_options']
    assert options['engine'] == 'yue2' and options['arrangement'] == cover_chat.SING_BACK_ARRANGEMENT
    assert options['arrangement'] == '~~sb:1~~ a cappella, solo female vocal, no instruments'
    assert set(options) == {'audio_cover_strength', 'cover_noise_strength', 'engine', 'arrangement'}   # no new key
    assert row['cover_status'] == 'DELIVERED' and row['cover_finished'] is True


def test_a_hum_is_ordered_with_her_lyrics(world, cloud):
    row = sang_row(world, heard=HUM, seconds=6, lyrics='今晚的月亮\n陪我慢慢唱')
    run_service(world, row, Send())
    [call] = cloud.calls
    assert call['data']['lyrics'] == '今晚的月亮\n陪我慢慢唱'
    assert call['data']['cover_options']['arrangement'] == cover_chat.SING_BACK_ARRANGEMENT


def test_the_result_goes_back_as_an_mp3_is_filed_remembered_and_the_voice_is_used(world, cloud):
    row = sang_row(world)
    voice = row['cover_request']['source']
    send = Send()
    run_service(world, row, send)
    [(name, data)] = send.files
    assert name.startswith('清唱 ') and name.endswith('.mp3') and data == b'ID3RIFFsung'
    assert send.texts == ['跟着你唱的，我也给你唱了一段，发给你了。']
    assert world.library == [(name[:-4], b'RIFFsung')]                            # added to the music library like a cover
    assert VoiceSources(world.tmp).get(voice)['used'] is True
    assert not cover_delivery.work_folder(world.tmp, row).exists()
    [memory] = list(cover_chat.memory_rows(row))
    assert memory['letter_id'].startswith('sing-back-') and memory['reply_text'] == send.texts[0]
    assert memory['letter_status'] == 'COMPLETED' and '清唱' in memory['content']
    run_service(world, row, send, rounds=2)                                        # a restart sends and orders nothing more
    assert len(send.files) == 1 and len(send.texts) == 1 and len(cloud.calls) == 1
    assert offered_for(world.server, {'letter_id': 'later', 'channel': 'qq'}) == []   # not offered again


def test_while_one_is_made_no_other_cover_or_sing_back_is_ordered(world, cloud):
    first = asked_row(world, 'turn-1')
    first['cover_status'] = 'MAKING'
    first['delivery_status'] = 'PENDING'
    second = sang_row(world, 'turn-2')
    send = Send()
    run_service(world, second, send)
    assert second['cover_status'] == 'FAILED' and second['cover_error_code'] == 'COVER_IN_PROGRESS'
    assert 'COVER_IN_PROGRESS' in send.texts[0] and cloud.calls == [] and not send.files
    assert VoiceSources(world.tmp).get(second['cover_request']['source'])['used'] is False     # she can try again
    third = asked_row(world, 'turn-3')
    second['cover_status'] = 'MAKING'
    second['delivery_status'] = 'PENDING'
    first['cover_status'] = 'FAILED'
    send = Send()
    run_service(world, third, send)
    assert third['cover_error_code'] == 'COVER_IN_PROGRESS' and not send.files


def test_what_is_being_made_is_listed_for_the_writer_as_a_sing_back(world):
    row = sang_row(world)
    row['cover_status'] = 'MAKING'
    row['cover_title'] = '清唱 11-08 21-35'
    other = {'letter_id': 'turn-2', 'channel': 'qq', 'binding_id': BINDING}
    assert cover_chat.in_flight(world.server, other) == [{'what': '清唱', 'title': '清唱 11-08 21-35', 'state': '制作中'}]


@pytest.mark.parametrize('seconds,code,words', [(2, 'SINGBACK_TOO_SHORT', '不到 3 秒'), (2.9, 'SINGBACK_TOO_SHORT', '再唱长一点')])
def test_a_recording_shorter_than_three_seconds_is_told_before_anything_is_ordered(world, cloud, seconds, code, words):
    row = sang_row(world, seconds=seconds)
    send = Send()
    run_service(world, row, send, rounds=2)
    assert row['cover_status'] == 'FAILED' and row['cover_error_code'] == code
    [message] = send.texts
    assert words in message and f'错误码 {code}' in message and message.startswith('这次没能接着你唱')
    assert cloud.calls == [] and not send.files and VoiceSources(world.tmp).get(row['cover_request']['source'])['used'] is False


def test_a_voice_that_has_vanished_is_told_with_its_code(world, cloud):
    row = sang_row(world)
    VoiceSources(world.tmp).remove(row['cover_request']['source'])
    send = Send()
    run_service(world, row, send)
    assert row['cover_status'] == 'FAILED' and len(send.texts) == 1
    assert 'SINGBACK_AUDIO_UNREADABLE' in send.texts[0] and '这段语音打不开' in send.texts[0] and cloud.calls == []


# --- failures: reason and code, once --------------------------------------------------------------------------

def failing_cloud(monkeypatch, error):
    def generate(kind, data, output, **kwargs):
        raise error
    monkeypatch.setattr('runtime.remote_pipeline.generate', generate)


@pytest.mark.parametrize('code,words', [
    ('SINGBACK_AUDIO_UNREADABLE', '这段语音打不开'),
    ('SINGBACK_TOO_SHORT', '不到 3 秒'),
    ('SINGBACK_TOO_LONG', '太长了'),
    ('SINGBACK_SILENT', '几乎没有声音'),
    ('SINGBACK_NO_MELODY', '没听出稳定的旋律'),
    ('SINGBACK_TOO_FEW_NOTES', '听出来的音太少'),
    ('SINGBACK_TOO_MANY_NOTES', '音太密了'),
    ('SINGBACK_NEEDS_LYRICS', '没有歌词'),
    ('SINGBACK_SEPARATE_FAILED', '提取清唱'),
    ('YUE2_EARLY_STOP', '唱到一半停了'),
    ('YUE2_TOO_SHORT', '唱出来的太短'),
    ('GPU_QUEUE_FULL', '正忙'),
    ('GPU_LANE_BUSY', '一直忙着'),
    ('GPU_TASK_TIMEOUT', '做了很久'),
    ('SINGBACK_SOMETHING_NEW', '处理不了这段语音')])
def test_a_failed_order_is_told_once_with_its_plain_reason_and_the_code(world, monkeypatch, code, words):
    failing_cloud(monkeypatch, task_failed(code) if not code.startswith('GPU_') else CloudError(code, 429))
    row = sang_row(world)
    send = Send()
    run_service(world, row, send, rounds=3)
    assert row['cover_status'] == 'FAILED' and row['cover_error_code'] == code
    [message] = send.texts
    assert message.startswith('这次没能接着你唱：') and words in message and f'错误码 {code}' in message
    assert not send.files and world.library == []
    assert VoiceSources(world.tmp).get(row['cover_request']['source'])['used'] is False


def test_an_unknown_failure_is_a_generic_sentence_with_its_code(world, monkeypatch):
    failing_cloud(monkeypatch, RuntimeError('boom'))
    row = sang_row(world)
    send = Send()
    run_service(world, row, send)
    assert send.texts == ['这次没能接着你唱：中间出了点问题（错误码 COVER_FAILED）。']


def test_the_failure_words_follow_the_interface_language(monkeypatch):
    from runtime import ui_language
    monkeypatch.setattr(ui_language, 'current', lambda: ui_language.ENGLISH)
    for code in ('SINGBACK_TOO_SHORT', 'SINGBACK_NEEDS_LYRICS', 'SINGBACK_NO_MELODY', 'YUE2_EARLY_STOP', 'SINGBACK_NEW_ONE'):
        error = CloudError('GPU_TASK_FAILED', 502)
        error.detail = code
        words = cover_chat.failure_words(error, sing_back=True)
        assert not any('一' <= ch <= '鿿' for ch in words) and f'error code {code}' in words
    assert not any('一' <= ch <= '鿿' for ch in cover_chat.sing_back_completion_text())
    assert not any('一' <= ch <= '鿿' for ch in cover_chat.sing_back_title({'created_at': 1_700_000_000}))


# --- covers behave as before ---------------------------------------------------------------------------------

def test_a_cover_keeps_its_wording_for_the_same_codes():
    from runtime.personal_chat.cover_chat import failure_words
    error = CloudError('GPU_TASK_FAILED', 502)
    error.detail = 'YUE2_EARLY_STOP'
    assert failure_words(error) == '这次翻唱没能做成：唱歌那边处理不了这首歌（错误码 YUE2_EARLY_STOP）。'
    error.detail = 'YUE2_TOO_SHORT'
    assert failure_words(error) == '这次翻唱没能做成：唱歌那边处理不了这首歌（错误码 YUE2_TOO_SHORT）。'


def test_a_cover_is_still_ordered_without_the_marker_and_without_lyrics(cloud, tmp_path):
    source = tmp_path / 'song.wav'
    source.write_bytes(b'RIFFsong')
    REAL_ORDER(source, tmp_path / 'one.wav', '慢一点的爵士版')
    REAL_ORDER(source, tmp_path / 'two.wav')
    styled, plain = cloud.calls
    assert styled['data']['lyrics'] == '' and styled['data']['cover_options']['arrangement'] == '慢一点的爵士版'
    assert plain['data']['lyrics'] == '' and 'arrangement' not in plain['data']['cover_options']
    assert styled['data']['cover_options']['engine'] == plain['data']['cover_options']['engine'] == 'yue2'
    assert plain['assets'] == {'source_asset': b'RIFFsong'}


def test_a_cover_row_is_not_taken_for_a_sing_back(world):
    row = asked_row(world)
    assert not cover_chat.is_sing_back(row) and cover_chat.is_sing_back(sang_row(world, 'turn-2'))
    assert cover_delivery.song_name(row, '晴天') == '晴天（翻唱）'
