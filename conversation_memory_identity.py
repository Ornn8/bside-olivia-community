"""One canonical identity boundary for local conversation memory."""

from __future__ import annotations

import re


_USER_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


class ConversationMemoryIdentityError(ValueError):
    pass


def normalize_conversation_memory_user_id(value: object) -> str:
    """Return the sole user scope used by config, retrieval, delivery, and audit."""

    if not isinstance(value, str):
        raise ConversationMemoryIdentityError("conversation memory user_id is invalid")
    normalized = value.strip().casefold()
    if not _USER_ID_RE.fullmatch(normalized):
        raise ConversationMemoryIdentityError("conversation memory user_id is invalid")
    return normalized


__all__ = [
    "ConversationMemoryIdentityError",
    "normalize_conversation_memory_user_id",
]
