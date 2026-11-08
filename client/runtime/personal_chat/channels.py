"""Which chat channels exist and what each can carry.

`qq` and `app` (the phone app) are the full instant-message channels: voice, photos, stickers,
songs, gestures. `wechat` only exists in old stored rows. The reply pipeline asks these helpers
instead of comparing a channel name with 'qq'.
"""

from contextlib import contextmanager
from contextvars import ContextVar

RICH = frozenset({'qq', 'app'})
CHATS = frozenset({'qq', 'wechat', 'app'})


def rich(channel) -> bool:
    return channel in RICH


def proactive_channel(channel, configured) -> bool:
    """Her own contact follows the QQ rules and goes to the phone app only when the user has no QQ channel."""
    return channel == 'qq' or (channel == 'app' and 'qq' not in configured)


# What the item being sent is, when the file alone does not say (a sticker, the words behind a voice bubble).
# Only the phone app reads it; QQ sends the bare file.
MEDIA_HINT = ContextVar('chat_media_hint', default=None)


@contextmanager
def media_hint(**fields):
    token = MEDIA_HINT.set(fields)
    try:
        yield
    finally:
        MEDIA_HINT.reset(token)
