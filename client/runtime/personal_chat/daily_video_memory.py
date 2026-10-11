"""A delivered daily video enters conversation memory from facts already known.

The record is built from the event, the place, her spoken line and the caption that
was actually sent. No model or vision call ever looks at the clip. It rides on a
persisted chat row, like a delivered photo, and the memory outbox derives one
source per video event from it.
"""
from datetime import datetime, timedelta, timezone
import hashlib

KEY = 'daily_video_memories'
LOCAL = timezone(timedelta(hours=8))
_LABELS = {'housework': '做家务', 'walk': '散步', 'bath_finished': '洗完澡后的样子', 'shopping': '购物',
           'meal': '吃饭', 'wake_up': '刚睡醒的样子', 'moment': '此刻所在的日常'}
BOUNDARY = ('这是已经发给对方的视频；不证明对方已观看，也不产生新的生活事件；'
            '画面里除上述文字外的细节没有识别过，回忆时不要凭空补充。')


def letter_id(event_id):
    return 'daily-video-' + hashlib.sha256(event_id.encode()).hexdigest()[:32]


def build(request, *, facts, caption, delivered_at):
    """One record per video event: that she sent a short video of herself, where, and what she said."""
    facts = facts or {}
    place = request.get('place') or facts.get('place') or {}
    where = '、'.join(dict.fromkeys(name for name in (place.get('name'), facts.get('location')) if name))
    detail = facts.get('note') or facts.get('activity') or ''
    shot = datetime.fromisoformat(request['event_at']).astimezone(LOCAL)
    lines = ['林离已通过QQ给用户发出一段她自己出镜的短视频（自拍，十几秒以内）。',
             f"内容类型：{_LABELS[request['event_kind']]}。拍摄于{shot.month}月{shot.day}日{shot:%H:%M}。"]
    if where:
        lines.append('她拍摄时在：' + where + '。')
    if detail:
        lines.append('当时的事：' + detail)
    lines.append('她在视频里说：“' + request['spoken_text'] + '”')
    if caption:
        lines.append('随视频一起发出的文字：' + caption)
    lines.append(BOUNDARY)
    return dict(event_id=request['event_id'], delivered_at=delivered_at.isoformat(), text='\n'.join(lines))


def attach(rows, binding_id, record):
    """Put the record on the newest delivered chat row of this owner; never twice for one event."""
    if any(isinstance(item, dict) and item.get('event_id') == record['event_id']
           for row in rows for item in (row.get(KEY) if isinstance(row.get(KEY), list) else ())):
        return 'EXISTS'
    own = [row for row in rows if row.get('channel') == 'qq' and row.get('binding_id') == binding_id]
    anchor = next((row for row in reversed(own) if row.get('delivery_status') == 'DELIVERED'),
                  next(iter(reversed(own)), None))
    if anchor is None:
        return 'NO_CONVERSATION'
    anchor.setdefault(KEY, []).append(record)
    return 'ATTACHED'


def memory_rows(row):
    """Additional outbox deliveries; never rewrite the original text source."""
    records = row.get(KEY)
    seen = set()
    for item in records if isinstance(records, list) else ():
        try:
            event_id, text, stamp = item['event_id'], item['text'], datetime.fromisoformat(item['delivered_at'])
            if (not isinstance(event_id, str) or not event_id or event_id in seen or not isinstance(text, str)
                    or not text.strip() or stamp.utcoffset() is None):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        seen.add(event_id)
        yield dict(letter_id=letter_id(event_id), reply_revision=1, content='', reply_text=text,
                   letter_status='COMPLETED', origin='proactive', created_at=stamp.timestamp(),
                   private_world_occurred_at=item['delivered_at'])
