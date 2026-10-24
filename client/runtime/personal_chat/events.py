"""Normalize owner-only text events before any model, memory or world access."""
from dataclasses import dataclass
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Mapping


@dataclass(frozen=True)
class PersonalMessage:
    channel: str
    account_id: str
    owner_id: str
    message_id: str
    text: str
    parts: tuple[tuple[str, str], ...] = ()
    input_kind: str = 'text'
    sent_at: str | None = None
    images: tuple[tuple[str, str], ...] = ()
    media: tuple[tuple[str, str, str, str], ...] = ()

    @property
    def sources(self):
        return self.parts or ((self.message_id, self.text),)

    @property
    def exchange_id(self) -> str:
        source = "\0".join((self.channel, self.account_id, self.owner_id, self.message_id))
        return "im-" + hashlib.sha256(source.encode()).hexdigest()

    @property
    def binding_id(self):
        return hashlib.sha256('\0'.join((self.channel,self.account_id,self.owner_id)).encode()).hexdigest()


# What the other QQ segment kinds read as (2026-10-24). A share card keeps its title and link, a sticker its caption;
# a kind this list does not know still arrives as a marker instead of losing the whole message.
_MARKERS = {'image': '[图片]', 'record': '[语音]', 'video': '[视频]', 'file': '[文件]', 'forward': '[聊天记录]',
            'node': '[聊天记录]', 'contact': '[名片]', 'location': '[位置]',
            'music': '[音乐分享]', 'shake': '[窗口抖动]', 'poke': '[戳一戳]', 'xml': '[分享卡片]', 'markdown': '[消息]'}
_SILENT = {'reply', 'at'}


def _face(data):
    from .qq_faces import FACE_NAMES
    name = FACE_NAMES.get(str(data.get('id')))
    return f'[表情:{name}]' if name else '[QQ表情]'


def _sticker(data):
    summary = data.get('summary')
    caption = summary.strip().strip('[]').strip() if isinstance(summary, str) else ''
    return f'[表情包:{caption[:20]}]' if caption else '[表情包]'


def _card(data):
    """A shared link or mini-program card: its source, title and https link, never its raw JSON."""
    try:
        card = json.loads(data['data']) if isinstance(data.get('data'), str) and len(data['data']) <= 65536 else None
    except ValueError:
        card = None
    if not isinstance(card, dict):
        return '[分享卡片]'
    meta = card.get('meta') if isinstance(card.get('meta'), dict) else {}
    detail = next((value for value in meta.values() if isinstance(value, dict)), {})
    mini = 'qqdocurl' in detail
    words = [value.strip()[:120] for value in (detail.get('title') if mini else detail.get('tag'),
                                               detail.get('desc') if mini else detail.get('title'))
             if isinstance(value, str) and value.strip()]
    if not words and isinstance(card.get('prompt'), str) and card['prompt'].strip():
        words = [card['prompt'].strip()[:120]]
    text = '[分享卡片：' + ' · '.join(words) + ']' if words else '[分享卡片]'
    url = detail.get('qqdocurl') or detail.get('jumpUrl')
    if isinstance(url, str) and url.startswith('https://') and len(url) <= 500 and not any(c.isspace() for c in url):
        text += ' ' + url
    return text


def _segment_text(segment):
    kind, data = segment['type'], segment['data']
    if kind == 'text':
        return data['text']
    if kind in _SILENT:
        return ''
    if kind == 'face':
        return _face(data)
    if kind == 'mface':
        return _sticker(data)
    if kind == 'json':
        return _card(data)
    if kind in ('dice', 'rps'):
        from .qq_games import marker
        return marker(kind, data)
    return _MARKERS.get(kind, '[QQ消息]')


def _poke(payload, *, account_id, owner_id):
    """He poked her in their private chat: a turn of its own."""
    if (payload.get('post_type') != 'notice' or payload.get('notice_type') != 'notify' or payload.get('sub_type') != 'poke'
            or payload.get('group_id') or str(payload.get('self_id', '')) != account_id
            or str(payload.get('user_id', '')) != owner_id or str(payload.get('target_id', '')) != account_id
            or str(payload.get('sender_id', owner_id)) != owner_id  # NapCat: user_id is the chat, sender_id who poked
            or owner_id == account_id):
        return None
    raw_time = payload.get('time')
    if type(raw_time) not in (int, float) or raw_time <= 0:
        return None
    try:
        sent_at = datetime.fromtimestamp(raw_time, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None
    return PersonalMessage('qq', account_id, owner_id, f'poke-{int(raw_time)}', '[他戳了戳你]', sent_at=sent_at)


def owner_message(channel: str, payload: Mapping, *, account_id: str, owner_id: str) -> PersonalMessage | None:
    """Reject all groups, foreign senders, own echoes and incomplete events."""
    if not account_id or not owner_id:
        raise ValueError("PERSONAL_CHAT_OWNER_REQUIRED")
    if not isinstance(payload, Mapping):
        return None
    if channel == "qq":
        if payload.get('post_type') == 'notice':
            return _poke(payload, account_id=account_id, owner_id=owner_id)
        if (payload.get("post_type") != "message" or payload.get("message_type") != "private"
                or str(payload.get("self_id", "")) != account_id
                or str(payload.get("user_id", "")) != owner_id or owner_id == account_id):
            return None
        segments = payload.get("message")
        if not isinstance(segments, list) or any(not isinstance(s, dict) or not isinstance(s.get("type"), str)
            or not isinstance(s.get("data"), dict) for s in segments):
            return None
        if any(s['type'] == 'text' and not isinstance(s['data'].get('text'), str) for s in segments):
            return None
        pictures = [s['data'] for s in segments if s['type'] == 'image']
        if len(pictures) > 4 or any(not isinstance(p.get('url'), str)
                                   or not p['url'].startswith('https://') or len(p['url']) > 8192 for p in pictures):
            return None
        attachments = []
        for segment in segments:
            if segment['type'] not in {'record', 'video', 'file'}:
                continue
            data = segment['data']
            reference = data.get('file_id') or data.get('file')
            # Only NapCat resource IDs, never paths/URLs/base64 supplied in events.
            if not isinstance(reference, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,256}', reference):
                return None
            name = data.get('name', '') if segment['type'] == 'file' else ''
            if not isinstance(name, str) or len(name) > 255:
                return None
            attachments.append(('audio' if segment['type'] == 'record' else segment['type'], reference, name))
        if len(attachments) + len(pictures) > 4:
            return None
        # A face is named from the bundled catalog; an ID it does not hold stays [QQ表情], never a guess.
        text = "".join(_segment_text(s) for s in segments)
        message_id = payload.get("message_id")
    elif channel == "wechat":
        if (payload.get("group_id") or payload.get("message_type") != 1
                or payload.get("message_state") != 2
                or payload.get("from_user_id") != owner_id
                or payload.get("to_user_id") != account_id):
            return None
        items = payload.get("item_list")
        if not isinstance(items, list) or any(not isinstance(i, dict) or i.get("type") not in {1,3}
            or not isinstance(i.get('text_item' if i.get('type') == 1 else 'voice_item'), dict)
            or not isinstance(i['text_item' if i.get('type') == 1 else 'voice_item'].get('text'), str) for i in items):
            return None
        text = '\n'.join(i['text_item' if i['type'] == 1 else 'voice_item']['text'] for i in items)
        message_id = payload.get("message_id")
    else:
        raise ValueError("PERSONAL_CHAT_CHANNEL_INVALID")
    if not isinstance(text, str) or not text.strip() or len(text) > 10000 or message_id is None:
        return None
    raw_time = payload.get('time') if channel == 'qq' else payload.get('create_time_ms')
    sent_at = None
    if type(raw_time) in (int, float) and raw_time > 0:
        try:
            sent_at = datetime.fromtimestamp(raw_time / (1000 if channel == 'wechat' else 1), timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    return PersonalMessage(channel, account_id, owner_id, str(message_id), text,
        input_kind=(('voice' if attachments[0][0] == 'audio' else attachments[0][0]) if channel == 'qq' and attachments else
                    'image' if channel == 'qq' and pictures else
                    'voice' if channel == 'wechat' and any(i['type'] == 3 for i in items) else 'text'), sent_at=sent_at,
        images=tuple((str(message_id), p['url']) for p in pictures) if channel == 'qq' else (),
        media=tuple((str(message_id), *item) for item in attachments) if channel == 'qq' else ())



def mergeable_by_sent_time(previous: PersonalMessage, current: PersonalMessage, max_gap_seconds: float) -> bool:
    """Do not collapse delayed platform backlog into one live conversation turn.

    Arrival time is only a fallback when the transport does not provide a
    trustworthy send timestamp. When both timestamps exist, they must be
    chronological and within the configured burst window.
    """
    if not isinstance(previous, PersonalMessage) or not isinstance(current, PersonalMessage):
        raise TypeError("PERSONAL_CHAT_MESSAGE_REQUIRED")
    if max_gap_seconds <= 0:
        return False
    if previous.sent_at is None or current.sent_at is None:
        return True
    try:
        before = datetime.fromisoformat(previous.sent_at.replace("Z", "+00:00"))
        after = datetime.fromisoformat(current.sent_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if before.tzinfo is None or after.tzinfo is None:
        return False
    gap = (after - before).total_seconds()
    return 0 <= gap <= max_gap_seconds


def combine(events):
    first = events[0]
    sources = {}
    for event in events:
        if (event.channel, event.account_id, event.owner_id) != (first.channel, first.account_id, first.owner_id):
            raise ValueError('PERSONAL_CHAT_MERGE_OWNER_MISMATCH')
        for key, text in event.sources:
            if key in sources and sources[key] != text:
                raise ValueError('PERSONAL_CHAT_ID_CONFLICT')
            sources[key] = text
    text = '\n'.join(sources.values())
    if len(text) > 10000:
        raise ValueError('PERSONAL_CHAT_BATCH_TOO_LARGE')
    return PersonalMessage(first.channel, first.account_id, first.owner_id, next(iter(sources)), text, tuple(sources.items()),
                           next(e.input_kind for e in events if e.media) if any(e.media for e in events) else 'image' if any(e.images for e in events) else 'voice' if any(e.input_kind == 'voice' for e in events) else 'text',
                           first.sent_at, tuple(dict.fromkeys(image for event in events for image in event.images)),
                           tuple(dict.fromkeys(item for event in events for item in event.media)))
