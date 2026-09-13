"""Normalize owner-only text events before any model, memory or world access."""
from dataclasses import dataclass
import hashlib
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
        if not isinstance(segments, list) or any(not isinstance(s, dict) or s.get("type") != "text"
            or not isinstance(s.get("data"), dict) or not isinstance(s["data"].get("text"), str) for s in segments):
            return None
        text = "".join(s.get("data", {}).get("text", "") for s in segments if isinstance(s, dict))
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
    return PersonalMessage(channel, account_id, owner_id, str(message_id), text,
        input_kind='voice' if channel == 'wechat' and any(i['type'] == 3 for i in items) else 'text')


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
                           'voice' if any(e.input_kind == 'voice' for e in events) else 'text')
