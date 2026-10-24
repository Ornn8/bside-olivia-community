"""What she knows when a letter reply will carry a song she sings.

A cover letter routes to singing_video and the writer was told nothing about it: she saw only 「请翻唱《…》」, said
she had never heard the song and asked for a link, while her cover already waited for the night batch (2026-10-24,
owner: 「林离不知道晚上统一跑生成，用户看了会很懵逼」). The writer now reads, after the letter text, that the song came
with the letter (covers), and, when the service records songs only at night, by when it will be ready.
"""
import json
from datetime import datetime, time, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8))


def song_kind(letter, mode):
    """'cover' or 'original' when this reply carries a song she sings, else None."""
    material = letter.get('material') or {}
    if isinstance(material, dict) and material.get('cover_source_id'):
        return 'cover'
    if mode in {'singing_video', 'voice_song_video', 'musical_video'}:
        return 'original'
    return None


def _clock(value, default):
    try:
        hour, minute = (int(part) for part in str(value).split(':'))
        return time(hour, minute)
    except (TypeError, ValueError):
        return default


def ready_day(night, now):
    """'今天' or '明天': the morning by which a song ordered now is recorded, in Shanghai time."""
    start = _clock(night.get('start'), time(3))
    end = _clock(night.get('end'), time(7))
    local = now.astimezone(SHANGHAI)
    # An order placed before tonight's window, or early enough inside it, is recorded this morning.
    latest = (datetime.combine(local.date(), end, SHANGHAI) - timedelta(hours=1)).time()
    return '今天' if local.time() < start or local.time() < latest else '明天'


def writer_note(letter, mode, capabilities, *, now=None):
    """The fact block appended to the letter for the writer, or '' when the reply carries no song."""
    kind = song_kind(letter, mode)
    if kind is None:
        return ''
    material = letter.get('material') or {}
    fact = {}
    if kind == 'cover':
        video = material.get('cover_output') == 'video'
        fact['reply_includes'] = '林离照着对方附上的原曲唱的翻唱' + ('视频' if video else '音频')
        fact['meaning'] = ('对方随信附上了这首歌的原曲音频，录制时照着它唱，这封回信会附上你唱的翻唱。'
                           '不要说没听过、不会唱，也不要让对方发链接或描述旋律；写信时你没有听到原曲，'
                           '不描述或评价它听起来怎样；也不要说已经唱好或已经发出。')
    else:
        fact['reply_includes'] = '林离唱的歌'
        fact['meaning'] = '这封回信会附上你唱的歌；不要说已经唱好或已经发出。'
    night = (capabilities or {}).get('night_batch')
    if isinstance(night, dict) and kind in (night.get('kinds') or ()):
        day = ready_day(night, now or datetime.now(SHANGHAI))
        end = _clock(night.get('end'), time(7))
        fact['timing'] = (f'歌每天在凌晨统一录，这首一般会在{day}早上{end.hour}点前录好，录好后自动送到对方这里；'
                          '这封信的文字会先送到。可以自然地告诉对方要等到那时，不用解释录制安排的原因。')
    return '\n[系统唱歌安排，非用户原话]\n' + json.dumps(fact, ensure_ascii=False)
