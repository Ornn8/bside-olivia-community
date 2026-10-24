"""A letter reply that carries her song tells the writer the song came with it and when it will be ready."""
import json
from datetime import datetime, timedelta, timezone

from runtime.media.song_notice import ready_day, song_kind, writer_note

SHANGHAI = timezone(timedelta(hours=8))
NIGHT = {'night_batch': {'kinds': ['cover', 'original', 'separate', 'lipsync'], 'start': '03:00', 'end': '07:00',
                         'timezone': 'Asia/Shanghai'}}
COVER = {'material': {'cover_source_id': 'abc123', 'cover_output': 'video'}}


def fact(text):
    head, body = text.split('\n', 2)[1:]
    assert head == '[系统唱歌安排，非用户原话]'
    return json.loads(body)


def at(hour, minute=0):
    return datetime(2026, 10, 24, hour, minute, tzinfo=SHANGHAI)


def test_cover_letter_says_the_song_came_with_it_and_when_it_is_ready():
    value = fact(writer_note(COVER, 'singing_video', NIGHT, now=at(16, 42)))
    assert value['reply_includes'].endswith('翻唱视频')
    assert '不要说没听过' in value['meaning'] and '链接' in value['meaning']
    assert '明天早上7点前' in value['timing'] and '文字会先送到' in value['timing']


def test_audio_cover_and_original_songs():
    audio = {'material': {'cover_source_id': 'abc123'}}
    assert fact(writer_note(audio, 'singing_video', NIGHT, now=at(16)))['reply_includes'].endswith('翻唱音频')
    original = fact(writer_note({}, 'voice_song_video', NIGHT, now=at(16)))
    assert original['reply_includes'] == '林离唱的歌' and '明天' in original['timing']


def test_no_song_no_note_and_no_night_no_timing():
    assert song_kind({}, 'text_letter') is None and writer_note({}, 'text_letter', NIGHT) == ''
    assert 'timing' not in fact(writer_note(COVER, 'singing_video', {}, now=at(16)))
    assert 'timing' not in fact(writer_note(COVER, 'singing_video', {'night_batch': {'kinds': ['lipsync']}}, now=at(16)))


def test_ready_day_follows_the_window_in_shanghai_time():
    night = NIGHT['night_batch']
    assert ready_day(night, at(1)) == '今天'      # before tonight's window
    assert ready_day(night, at(5, 30)) == '今天'  # early enough inside it
    assert ready_day(night, at(6, 30)) == '明天'  # too late for this window
    assert ready_day(night, at(23)) == '明天'
    # 16:42 in Tokyo is 15:42 in Shanghai: still the next morning.
    assert ready_day(night, datetime(2026, 10, 24, 16, 42, tzinfo=timezone(timedelta(hours=9)))) == '明天'
