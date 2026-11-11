"""A voice message of his longer than a minute (2026-11-11: a 76 s sung voice message was refused whole by the hearing step,
swallowed as "unrecognized", and was not kept either, so she said she could not hear it). It is now cut at quiet places
into pieces under a minute and heard piece by piece, kept whole up to five minutes, and sung back whole; only when the
whole recording is refused as too long is the clearest passage sung, and he is told which part."""
import array
import asyncio
import io
import json
from types import SimpleNamespace
import wave

import pytest

from runtime import incoming_media, voice_clip
from runtime.media.song_sources import VoiceSources
from runtime.personal_chat import cover_chat, cover_delivery
from runtime.personal_chat.events import PersonalMessage
from tests.http.test_qq_cover_delivery import Send, run_service, task_failed, world  # noqa: F401 (world is a fixture)
from tests.http.test_qq_sing_back import Cloud, cloud, offered_for, sang_row  # noqa: F401 (cloud is a fixture)

RATE = 8000
TONE = array.array('h', [int(9000 * (1 if (i // 10) % 2 == 0 else -1)) for i in range(20)])      # 400 Hz square wave
AIR = array.array('h', [3, -3] * 10)                                                              # quiet, not silent


def sung_bytes(seconds, *, phrase=6.0, rest=0.8, rate=RATE) -> bytes:
    """A mono WAV of ``seconds``: tone phrases of ``phrase`` s with ``rest`` s of air between them."""
    samples = array.array('h')
    cycle = int((phrase + rest) * rate)
    one = int(phrase * rate) // 20 * 20
    unit = (TONE * (one // 20)) + (AIR * ((cycle - one) // 20))
    total = int(seconds * rate)
    while len(samples) < total:
        samples.extend(unit)
    del samples[total:]
    out = io.BytesIO()
    with wave.open(out, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(samples.tobytes())
    return out.getvalue()


def sung(path, seconds, **options):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(sung_bytes(seconds, **options))
    return path


def seconds_of(raw):
    with wave.open(io.BytesIO(raw)) as audio:
        return audio.getnframes() / audio.getframerate()


# --- the pieces it is heard in -------------------------------------------------------------------------------------

def test_a_long_recording_is_cut_into_pieces_under_a_minute_in_the_quiet_between_phrases(tmp_path):
    pytest.importorskip('numpy')
    total, pieces = voice_clip.parts(sung(tmp_path / 'a.wav', 76.4, phrase=6.0, rest=0.8))
    assert round(total, 1) == 76.4 and len(pieces) == 2
    assert pieces[0][0] == 0.0 and pieces[-1][1] == pytest.approx(76.4, abs=0.01)
    assert all(a == b for (_x, a, _r), (b, _y, _s) in zip(pieces, pieces[1:])), 'no gap and no overlap'
    assert all(seconds_of(raw) < 58 for _a, _b, raw in pieces)
    for _a, cut, _raw in pieces[:-1]:
        assert (cut % 6.8) >= 5.9, f'cut at {cut} is inside a phrase'          # in the rest between two phrases
    assert len(voice_clip.parts(sung(tmp_path / 'b.wav', 40))[1]) == 1         # a minute or less is one piece
    total, pieces = voice_clip.parts(sung(tmp_path / 'c.wav', 300))
    assert len(pieces) == 7 and all(seconds_of(raw) < 58 for _a, _b, raw in pieces)
    total, pieces = voice_clip.parts(sung(tmp_path / 'd.wav', 400), cap=300)
    assert round(total) == 400 and pieces[-1][1] == pytest.approx(300, abs=0.01)


def test_without_numpy_the_pieces_are_cut_at_equal_distances_and_the_passage_is_the_first_stretch(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_clip, '_numpy', lambda: None)
    path = sung(tmp_path / 'a.wav', 76.4)
    total, pieces = voice_clip.parts(path)
    assert len(pieces) == 2 and all(seconds_of(raw) < 58 for _a, _b, raw in pieces)
    assert voice_clip.passage(path, 58.0) == (0.0, 58.0)
    assert voice_clip.passage(sung(tmp_path / 'b.wav', 40), 58.0) == (0.0, 40.0)
    with pytest.raises(ValueError, match='MEDIA_DURATION_INVALID'):
        voice_clip.parts(tmp_path / 'missing.wav')
    cut = voice_clip.cut(path, 10, 20, tmp_path / 'out' / 'p.wav')
    assert cut.is_file() and not list((tmp_path / 'out').glob('*.part')) and seconds_of(cut.read_bytes()) == pytest.approx(10, abs=0.05)


def test_the_passage_is_the_clearest_stretch_of_whole_phrases_when_it_does_not_fit(tmp_path):
    np = pytest.importorskip('numpy')
    path = sung(tmp_path / 'c.wav', 76.4)
    raw, rate, channels, width = voice_clip.read(path)
    samples = np.frombuffer(raw, dtype='<i2').copy()
    samples[:25 * rate] = (samples[:25 * rate] * 0.0003).astype('<i2')     # the first 25 s are mostly air
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(samples.tobytes())
    start, end = voice_clip.passage(path, 58.0)
    assert start >= 17 and end == pytest.approx(76.4, abs=0.5) and end - start <= 58.0


# --- hearing it ----------------------------------------------------------------------------------------------------

def hear(tmp_path, monkeypatch, *, seconds=76.4, observe=None, describe=None, row=None):
    """His turn with one voice message of ``seconds``; the hearing service is ``observe`` (called once per piece)."""
    data = sung_bytes(seconds)
    calls, acquired = [], []

    async def resolve(kind, reference):
        return {'base64': 'AAAA'}

    async def acquire(result, target, max_bytes=incoming_media.MAX_BYTES):
        acquired.append(max_bytes)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def default(server, kind, mime, raw, operation_id):
        calls.append((operation_id, seconds_of(raw)))
        return {'kind': kind, 'summary': f'[唱歌声]第{len(calls)}段的歌词', 'source': 'user', 'evidence_kind': 'media_observation'}
    monkeypatch.setattr(incoming_media, 'acquire', acquire)
    monkeypatch.setattr(incoming_media, 'observe_bytes', observe or default)
    if describe:
        monkeypatch.setattr(incoming_media, 'describe_media', describe)
    server = SimpleNamespace(_local_data_root=lambda: tmp_path, _state_root=lambda: tmp_path, _persist_store_state=lambda: None)
    event = PersonalMessage('qq', 'b', 'u', 'v1', '[语音]', media=(('v1', 'audio', 'ref-1', ''),))
    row = row if row is not None else {'letter_id': 'turn-1', 'channel': 'qq'}
    token = incoming_media.MEDIA_RESOLVER.set(resolve)
    try:
        asyncio.run(incoming_media.understand_incoming(server, event, row))
    finally:
        incoming_media.MEDIA_RESOLVER.reset(token)
    return row, calls, acquired


def context_of(row):
    return json.loads(incoming_media.incoming_context(row).split('\n', 2)[2])


def test_a_76_second_sung_voice_is_heard_in_pieces_and_she_is_told_how_long_and_what_was_heard(tmp_path, monkeypatch):
    row, calls, acquired = hear(tmp_path, monkeypatch)
    assert row['incoming_media_failed'] == 0 and row['incoming_media_status'] == 'COMPLETED'
    assert acquired == [incoming_media.AUDIO_MAX_BYTES] and incoming_media.AUDIO_MAX_BYTES >= 14 * 1024 * 1024
    assert sorted(name.rsplit(':', 1)[1] for name, _s in calls) == ['part0', 'part1'] and all(s < 58 for _n, s in calls)
    [item] = row['incoming_media_observations']
    assert item['kind'] == 'audio' and item['seconds'] == 76.4 and item['parts'] == 2 and item['missed'] == 0
    assert item['summary'].startswith('（这条语音一共76秒，比一分钟长，分2段听的')
    assert '第1段（0到' in item['summary'] and '[唱歌声]第1段的歌词' in item['summary'] and '[唱歌声]第2段的歌词' in item['summary']
    context = context_of(row)
    assert context['unrecognized_count'] == 0 and 'unrecognized_why' not in context
    assert context['media'][0]['summary'] == item['summary'] and '76秒' in context['media'][0]['summary']
    # the whole recording is kept as a voice she may sing back: its length and the words heard (no headings)
    [voice] = VoiceSources(tmp_path).unused()
    assert voice['seconds'] == pytest.approx(76.4, abs=0.01) and voice['heard'] == '[唱歌声]第1段的歌词 [唱歌声]第2段的歌词'
    assert row['voice_arrivals'][0]['source'] == voice['id']


def test_a_retried_turn_keeps_the_long_voice_once(tmp_path, monkeypatch):
    row, _calls, _ = hear(tmp_path, monkeypatch)
    row2, calls2, _ = hear(tmp_path, monkeypatch, row=row)
    assert calls2 == [] and len(VoiceSources(tmp_path).unused()) == 1 and len(row2['voice_arrivals']) == 1


def test_a_piece_that_could_not_be_heard_is_named_and_the_others_still_count(tmp_path, monkeypatch):
    async def observe(server, kind, mime, raw, operation_id):
        if operation_id.endswith('part1'):
            raise RuntimeError('MEDIA_NATIVE_UNAVAILABLE')
        return {'kind': kind, 'summary': '你爱我还是她', 'source': 'user', 'evidence_kind': 'media_observation'}
    row, _calls, _ = hear(tmp_path, monkeypatch, observe=observe)
    [item] = row['incoming_media_observations']
    assert row['incoming_media_failed'] == 0 and item['missed'] == 1
    assert '第1段' in item['summary'] and '你爱我还是她' in item['summary']
    assert '有1段没听清，已标出' in item['summary'] and '：这一段没听清' in item['summary']
    assert VoiceSources(tmp_path).unused()[0]['heard'] == '你爱我还是她'


def test_when_no_piece_can_be_heard_the_writer_gets_the_reason_code_and_the_voice_is_still_kept(tmp_path, monkeypatch):
    async def observe(server, kind, mime, raw, operation_id):
        raise RuntimeError('MEDIA_NATIVE_UNAVAILABLE')
    row, _calls, _ = hear(tmp_path, monkeypatch, observe=observe)
    assert row['incoming_media_failed'] == 1 and row['incoming_media_status'] == 'PARTIAL'
    assert row['incoming_media_observations'] == [] and row['incoming_media_failed_codes'] == ['MEDIA_NATIVE_UNAVAILABLE']
    context = context_of(row)
    assert context['unrecognized_count'] == 1 and context['unrecognized_why'] == ['MEDIA_NATIVE_UNAVAILABLE']


def test_a_voice_of_a_minute_or_less_is_heard_in_one_call_as_before(tmp_path, monkeypatch):
    seen = []

    async def describe(server, path, kind, name, operation_id):
        seen.append(operation_id)
        return {'kind': 'audio', 'summary': '两只老虎两只老虎', 'source': 'user', 'sha256': 'x',
                'evidence_kind': 'media_observation', 'observed_at': '2026-11-11T00:00:00+00:00'}
    row, pieces, acquired = hear(tmp_path, monkeypatch, seconds=12, describe=describe)
    assert len(seen) == 1 and pieces == [] and row['incoming_media_failed'] == 0
    assert 'seconds' not in row['incoming_media_observations'][0] and 'unrecognized_why' not in context_of(row)
    assert VoiceSources(tmp_path).unused()[0]['heard'] == '两只老虎两只老虎'


def test_speech_that_cannot_be_heard_still_says_why_with_a_code_only(tmp_path, monkeypatch):
    async def describe(server, path, kind, name, operation_id):
        raise ValueError('MEDIA_RESPONSE_INVALID: https://secret.example/x')
    row, _calls, _ = hear(tmp_path, monkeypatch, seconds=12, describe=describe)
    assert row['incoming_media_failed'] == 1 and row['incoming_media_failed_codes'] == ['MEDIA_RESPONSE_INVALID']
    assert 'secret.example' not in incoming_media.incoming_context(row)


def test_a_voice_over_five_minutes_says_how_much_was_heard_and_is_not_kept(tmp_path, monkeypatch):
    row, calls, _ = hear(tmp_path, monkeypatch, seconds=330)
    [item] = row['incoming_media_observations']
    assert len(calls) == 7 and item['seconds'] == 330.0 and '只听了前300秒，后面的没有听' in item['summary']
    assert len(item['summary']) <= 6000 and VoiceSources(tmp_path).unused() == []


def test_a_voice_of_exactly_five_minutes_is_kept(tmp_path, monkeypatch):
    hear(tmp_path, monkeypatch, seconds=300)
    assert VoiceSources(tmp_path).unused()[0]['seconds'] == pytest.approx(300.0, abs=0.01)


# --- the chat connection ---------------------------------------------------------------------------------------------

def test_the_qq_connection_takes_a_five_minute_voice_message():
    from runtime.personal_chat import qq
    import inspect
    assert 'max_msg_size=32 * 1024 * 1024' in inspect.getsource(qq)


def test_the_new_module_is_in_the_update_patch():
    from installer import full_patch
    assert 'runtime/voice_clip.py' in full_patch.PAYLOAD_EXTRA_FILES
    assert 'runtime/voice_clip.py' in full_patch.PAYLOAD_REQUIRED_RELATIVE_FILES


# --- singing it back ---------------------------------------------------------------------------------------------------

def long_row(world, seconds=76.4, **options):
    row = sang_row(world, seconds=seconds, **options)
    return row


class RefusingCloud(Cloud):
    """The cloud call; the first order of a recording over a minute is refused as too long, like a failed whole-clip route."""

    def __init__(self, refuse=True):
        super().__init__()
        self.refuse = refuse

    def generate(self, kind, data, output, *, environment=None, assets=None):
        raw = assets['source_asset'].read_bytes()
        seconds = seconds_of(raw)
        self.calls.append(dict(kind=kind, data=data, seconds=seconds, assets={'source_asset': raw}))
        if self.refuse and seconds > 60.5:
            output.with_name(output.stem + '-remote-order.private.json').write_text('{}', encoding='utf-8')   # its receipt
            raise task_failed('SINGBACK_TOO_LONG')
        assert not output.with_name(output.stem + '-remote-order.private.json').exists(), 'the refused task is polled again'
        output.write_bytes(b'RIFFsung')
        return {'remote_task_id': 't1'}


def test_a_long_voice_is_listed_for_the_writer_with_its_length_and_without_a_limit_note(world, cloud):
    row = long_row(world)
    [voice] = row['sing_back_voices']
    assert voice['seconds'] == 76
    assert '3秒到5分钟' in cover_chat.VOICE_MEANING and '一分钟' in cover_chat.VOICE_MEANING
    note = cover_chat.voice_context(row)
    assert '"seconds": 76' in note


def test_singing_back_a_long_voice_sends_the_whole_recording(world, cloud):
    row = long_row(world)
    send = Send()
    run_service(world, row, send)
    [call] = cloud.calls
    assert seconds_of(call['assets']['source_asset']) == pytest.approx(76.4, abs=0.01)       # nothing cut
    assert call['data']['cover_options']['arrangement'] == cover_chat.SING_BACK_ARRANGEMENT
    assert row['cover_status'] == 'DELIVERED' and 'passage' not in row['cover_request']
    [(name, data)] = send.files
    assert name.startswith('清唱 ') and '第' not in name
    assert send.texts == ['跟着你唱的，我也给你唱了一段，发给你了。']


def test_if_the_whole_recording_is_refused_as_too_long_its_clearest_passage_is_sung_and_he_is_told_which(world, monkeypatch):
    recorder = RefusingCloud()
    monkeypatch.setattr('runtime.remote_pipeline.generate', recorder.generate)
    row = long_row(world)
    send = Send()
    run_service(world, row, send)
    assert [round(call['seconds'], 1) for call in recorder.calls] == [76.4, recorder.calls[1]['seconds']]
    start, end = row['cover_request']['passage']
    assert 0 <= start < end <= 76.4 and end - start <= 58.01
    assert recorder.calls[1]['seconds'] == pytest.approx(end - start, abs=0.05)
    assert row['cover_status'] == 'DELIVERED' and row['cover_finished'] is True
    [(name, _data)] = send.files
    assert f'（第 {start:g} 到 {end:g} 秒）' in name
    [text] = send.texts
    assert f'第 {start:g} 到 {end:g} 秒' in text and '太长' in text and 'cover_error_code' not in row
    # the kept recording itself is not touched, and a restart orders nothing more
    kept = VoiceSources(world.tmp).get(row['cover_request']['source'])
    assert kept['seconds'] == pytest.approx(76.4, abs=0.01) and kept['used'] is True
    run_service(world, row, send, rounds=2)
    assert len(recorder.calls) == 2 and len(send.files) == 1


def test_any_other_refusal_of_the_whole_recording_is_not_turned_into_a_passage(world, monkeypatch):
    calls = []

    def generate(kind, data, output, **kwargs):
        calls.append(1)
        raise task_failed('SINGBACK_NO_MELODY')
    monkeypatch.setattr('runtime.remote_pipeline.generate', generate)
    row = long_row(world)
    send = Send()
    run_service(world, row, send, rounds=3)
    assert len(calls) == 1 and row['cover_status'] == 'FAILED' and 'passage' not in row['cover_request']
    assert 'SINGBACK_NO_MELODY' in send.texts[0]


def test_a_recording_of_a_minute_or_less_refused_as_too_long_is_not_cut(world, monkeypatch):
    calls = []

    def generate(kind, data, output, **kwargs):
        calls.append(1)
        raise task_failed('SINGBACK_TOO_LONG')
    monkeypatch.setattr('runtime.remote_pipeline.generate', generate)
    row = long_row(world, seconds=50)
    send = Send()
    run_service(world, row, send, rounds=3)
    assert len(calls) == 1 and row['cover_status'] == 'FAILED' and 'passage' not in row['cover_request']
    assert 'SINGBACK_TOO_LONG' in send.texts[0] and '太长' in send.texts[0]


def test_a_recording_longer_than_a_voice_can_be_kept_is_ordered_as_its_clearest_passage(world, monkeypatch):
    recorder = RefusingCloud(refuse=False)
    monkeypatch.setattr('runtime.remote_pipeline.generate', recorder.generate)
    row = long_row(world, seconds=70)
    # an older entry longer than five minutes (it can no longer be kept, but could be there): the length is what the index says
    store = VoiceSources(world.tmp)
    voice = row['cover_request']['source']
    rows = json.loads(store.index.read_text(encoding='utf-8'))
    rows[0]['seconds'] = 320.0
    store.index.write_text(json.dumps(rows), encoding='utf-8')
    send = Send()
    run_service(world, row, send)
    [call] = recorder.calls
    start, end = row['cover_request']['passage']
    assert call['seconds'] == pytest.approx(end - start, abs=0.05) and end - start <= 58.01
    assert row['cover_status'] == 'DELIVERED' and VoiceSources(world.tmp).get(voice)['used'] is True


def test_the_words_follow_the_interface_language(monkeypatch):
    from runtime import ui_language
    monkeypatch.setattr(ui_language, 'current', lambda: ui_language.ENGLISH)
    title = cover_chat.sing_back_title({'created_at': 1_700_000_000}, [10.0, 60.5])
    text = cover_chat.sing_back_completion_text([10.0, 60.5])
    assert '10' in title and '60.5' in title and '10' in text and '60.5' in text
    assert not any('一' <= ch <= '鿿' for ch in title + text)
    assert not any('一' <= ch <= '鿿' for ch in cover_chat._reason('SINGBACK_TOO_LONG', True))
