"""What Olivia writes for the user to read or hear follows the interface language.

The prompts, the persona and the memories stay in Chinese. When the interface is English, the model calls whose
text the user reads or hears get one short directive that asks for natural English; calls that only feed the
program (memory, world, routing, judgments) are left alone. Chinese mode adds nothing, so those requests stay
exactly as they were.

* ``USER_REQUEST_PREFIXES`` / ``USER_SCOPES`` are the explicit list of the user-facing calls, keyed on the request
  id the gateway already carries (see ``llm_gateway._wire_request_id``). An id that is not listed is internal.
* ``with_directive()`` is used by the gateway for those calls; ``for_user_text()`` is used where a call shares its
  id with an internal one (the quality rewrite shares ``quality-`` with the reviewer).
* ``scaled()`` converts a length limit written for Chinese characters into the same amount of English content.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from runtime import ui_language

# Request ids of the calls whose text reaches the user. Matching is by prefix, never by substring, so
# "history-select:letter-reply:..." (a recall step for a letter) stays internal.
USER_REQUEST_PREFIXES = (
    "letter-reply:",  # letter replies (the writer)
    "personal-chat:",  # QQ / WeChat replies and proactive chat, incl. spoken stories and piano notes
    "proactive:",  # letters she starts on her own: the plan (title) and the body
    "diary:",  # one day's diary entry
    "diary-revise:",  # its second pass
    "diary-memoir:",  # a month's memoir
    "diary-memoir-revise:",
    "daily-video-author:",  # words for a short selfie clip
    "daily-video-share:",
)
# Calls that carry no readable request id are recognised by their scope instead.
USER_SCOPES = frozenset({"song_content"})  # original song lyrics

# English needs about four times the characters (spaces included) of the same Chinese text.
LENGTH_FACTOR = 4

DIRECTIVE = (
    "Language: the user reads and hears Olivia in English. Write everything the user will read or hear "
    "(letters, chat messages, spoken lines, diary entries, titles, captions, song lyrics) in natural, fluent "
    "English, with the warmth and voice she already has. Keep her personality, relationship, memories and facts "
    "exactly as they are; the Chinese material in this prompt is reference only, so never translate it word for "
    "word. Keep the user's own words, when quoted, in the language the user wrote them. A length given in "
    "Chinese characters means the same amount of content in English (about 0.6 words per character), unless the "
    "prompt counts English letters too; then follow that count exactly. The output format does not change: "
    "JSON keys, enum values, tags, markers and ids stay exactly as specified, and only the wording a person "
    "reads is in English."
)


def writes_for_user(request_id: str | None, scope: Any = None) -> bool:
    """True for the model calls whose text the user reads or hears."""

    if isinstance(request_id, str) and request_id.startswith(USER_REQUEST_PREFIXES):
        return True
    return getattr(scope, "value", scope) in USER_SCOPES


def directive_message() -> dict[str, str]:
    return {"role": "system", "content": DIRECTIVE}


def for_user_text(messages: Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]:
    """``messages`` with the English directive in front of the last user message; unchanged in Chinese.

    The directive sits where the writer's other per-turn notes already sit, so the long stable prefix of the
    prompt is untouched. Asking twice adds nothing.
    """

    if not ui_language.is_english():
        return messages
    if any(message.get("content") == DIRECTIVE for message in messages):
        return messages
    items = list(messages)
    position = next((i for i in range(len(items) - 1, -1, -1) if items[i].get("role") == "user"), len(items))
    items.insert(position, directive_message())
    return tuple(items) if isinstance(messages, tuple) else items


def with_directive(
    messages: Sequence[Mapping[str, Any]], request_id: str | None, scope: Any = None
) -> Sequence[Mapping[str, Any]]:
    """The gateway's hook: the directive for a user-facing call in English, otherwise ``messages`` itself."""

    if not ui_language.is_english() or not writes_for_user(request_id, scope):
        return messages
    return for_user_text(messages)


def scaled(limit: int) -> int:
    """A character limit sized for Chinese, sized for English text when the interface is English."""

    return limit * LENGTH_FACTOR if ui_language.is_english() else limit


__all__ = [
    "DIRECTIVE", "LENGTH_FACTOR", "USER_REQUEST_PREFIXES", "USER_SCOPES",
    "directive_message", "for_user_text", "scaled", "with_directive", "writes_for_user",
]
