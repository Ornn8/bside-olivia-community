"""Normalize owner-only text events before any model, memory or world access."""
from dataclasses import dataclass
import hashlib
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


def owner_message(channel: str, payload: Mapping, *, account_id: str, owner_id: str) -> PersonalMessage | None:
    """Reject all groups, foreign senders, own echoes and incomplete events."""
    if not account_id or not owner_id:
        raise ValueError("PERSONAL_CHAT_OWNER_REQUIRED")
    if not isinstance(payload, Mapping):
        return None
    if channel == "qq":
        if (payload.get("post_type") != "message" or payload.get("message_type") != "private"
                or str(payload.get("self_id", "")) != account_id
                or str(payload.get("user_id", "")) != owner_id or owner_id == account_id):
            return None
        segments = payload.get("message")
        if not isinstance(segments, list) or any(not isinstance(s, dict) or s.get("type") not in {"text", "image", "face"}
            or not isinstance(s.get("data"), dict) for s in segments):
            return None
        if any(s['type'] == 'text' and not isinstance(s['data'].get('text'), str) for s in segments):
            return None
        pictures = [s['data'] for s in segments if s['type'] == 'image']
        if len(pictures) > 4 or any(not isinstance(p.get('url'), str)
                                   or not p['url'].startswith('https://') or len(p['url']) > 8192 for p in pictures):
            return None
        # QQ faces are not downloadable pictures. Preserve the surrounding text
        # without guessing an emotion from an opaque platform-specific face ID.
        text = "".join(s['data']['text'] if s['type'] == 'text' else
                       '[图片]' if s['type'] == 'image' else '[QQ表情]' for s in segments)
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
        input_kind=('image' if channel == 'qq' and pictures else
                    'voice' if channel == 'wechat' and any(i['type'] == 3 for i in items) else 'text'), sent_at=sent_at,
        images=tuple((str(message_id), p['url']) for p in pictures) if channel == 'qq' else ())



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
                           'image' if any(e.images for e in events) else 'voice' if any(e.input_kind == 'voice' for e in events) else 'text',
                           first.sent_at, tuple(dict.fromkeys(image for event in events for image in event.images)))
