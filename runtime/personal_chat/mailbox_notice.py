"""One-time mailbox awareness notice carried by an ordinary IM reply.

QQ/Weixin remain the immediate conversation surface.  If Olivia has already
published an unread proactive mailbox letter, the next user-initiated IM reply
may mention that the letter exists.  This is not a request to answer the letter
and never creates a standalone proactive IM send.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Iterable


NOTICE_TEXT = "对了，我之前往信箱里放了封信，你有空的时候再看看就好。"


def _stamp(row: dict) -> float:
    for key in ("published_at", "created_at"):
        value = row.get(key)
        if type(value) in (int, float):
            return float(value)
    return 0.0


def _eligible(row: dict) -> bool:
    return (
        isinstance(row, dict)
        and row.get("origin") == "proactive"
        and row.get("letter_status") == "COMPLETED"
        and not bool(row.get("is_read", 0))
        and not row.get("im_notice_delivered_at")
        and not row.get("superseded_by")
        and row.get("proactive_kind") != "contact_invitation"
        and isinstance(row.get("letter_id"), str)
        and bool(row.get("letter_id"))
    )


def pending_letter(rows: Iterable[dict]) -> dict | None:
    """Return the newest unread proactive letter that has not been mentioned in IM.

    The contact-invitation letter is excluded: once personal chat is configured,
    reminding the user about that setup invitation is stale and confusing.
    """

    return max((row for row in rows if _eligible(row)), key=_stamp, default=None)


def attach_notice(letters: Iterable[dict], chat_row: dict, text: str) -> str:
    """Append a non-coercive mailbox hint to one normal inbound-message reply."""

    if chat_row.get("origin") == "proactive":
        return text
    rows = list(letters)
    existing = chat_row.get("mailbox_notice_letter_id")
    if isinstance(existing, str) and existing:
        letter = next((row for row in rows if row.get("letter_id") == existing), None)
        if letter is not None and _eligible(letter):
            return text.rstrip() + "\n\n" + NOTICE_TEXT
        chat_row.pop("mailbox_notice_letter_id", None)
    letter = pending_letter(rows)
    if letter is None:
        return text
    chat_row["mailbox_notice_letter_id"] = letter["letter_id"]
    return text.rstrip() + "\n\n" + NOTICE_TEXT


def commit_notice(
    letters: Iterable[dict],
    chat_row: dict,
    persist: Callable[[], None],
    *,
    now: float | None = None,
) -> bool:
    """Mark the source letter only after the IM reply is confirmed delivered.

    The marker is owner-wide, not channel-specific, so a reminder sent on QQ is
    not repeated later on Weixin (and vice versa).
    """

    if chat_row.get("delivery_status") != "DELIVERED":
        return False
    letter_id = chat_row.get("mailbox_notice_letter_id")
    if not isinstance(letter_id, str) or not letter_id:
        return False
    letter = next(
        (
            item
            for item in letters
            if isinstance(item, dict) and item.get("letter_id") == letter_id
        ),
        None,
    )
    if letter is None or bool(letter.get("is_read", 0)) or letter.get("im_notice_delivered_at"):
        return False

    stamp = (
        float(now)
        if now is not None
        else datetime.now(timezone.utc).timestamp()
    )
    previous = {
        key: letter.get(key)
        for key in ("im_notice_delivered_at", "im_notice_channel", "im_notice_exchange_id")
    }
    existed = {key: key in letter for key in previous}
    letter["im_notice_delivered_at"] = stamp
    letter["im_notice_channel"] = chat_row.get("channel")
    letter["im_notice_exchange_id"] = chat_row.get("letter_id")
    try:
        persist()
    except BaseException:
        for key, value in previous.items():
            if existed[key]:
                letter[key] = value
            else:
                letter.pop(key, None)
        raise
    return True


__all__ = ["NOTICE_TEXT", "attach_notice", "commit_notice", "pending_letter"]
