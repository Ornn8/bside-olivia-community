"""Source-grounded wire contract for the original Olivia mailbox.

The supported 0.0.9.627 client maps camelCase response fields into its
Collection view.  This module keeps that public contract separate from local
runtime state and preserves optional snake_case aliases only for existing local
callers.  It contains no original client assets or source excerpts.
"""

from __future__ import annotations

from enum import IntEnum
import re
import time
from typing import Iterable, Mapping
from urllib.parse import urlsplit


class OriginalClientLetterStatus(IntEnum):
    PENDING = 1
    AUDITING = 2
    LLM_PROCESSING = 3
    REPLIED = 4
    FAILED = 5


class OriginalClientAuditStatus(IntEnum):
    PENDING = 1
    PASSED = 2
    REJECTED = 3


class OriginalClientReplyType(IntEnum):
    NONE = 0
    TEXT = 1
    SPEECH = 2
    MIX_PLAY = 3
    MIX_SVS = 4


_INTERNAL_LETTER_STATUS = {
    "PENDING": OriginalClientLetterStatus.PENDING,
    "AUDITING": OriginalClientLetterStatus.AUDITING,
    "PROCESSING": OriginalClientLetterStatus.LLM_PROCESSING,
    "LLM_PROCESSING": OriginalClientLetterStatus.LLM_PROCESSING,
    "COMPLETED": OriginalClientLetterStatus.REPLIED,
    "REPLIED": OriginalClientLetterStatus.REPLIED,
    "FAILED": OriginalClientLetterStatus.FAILED,
    "CANCELED": OriginalClientLetterStatus.FAILED,
    "CANCELLED": OriginalClientLetterStatus.FAILED,
}
_INTERNAL_AUDIT_STATUS = {
    "PENDING": OriginalClientAuditStatus.PENDING,
    "PASSED": OriginalClientAuditStatus.PASSED,
    "REJECTED": OriginalClientAuditStatus.REJECTED,
}
_MEDIA_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.mp4$")
_VIDEO_MODES = frozenset({"spoken_video", "musical_video", "video"})


class OriginalClientContractError(ValueError):
    """Stable contract error used only by pure serializers."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _now_value(now: float | None) -> float:
    value = time.time() if now is None else now
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OriginalClientContractError("ORIGINAL_CLIENT_NOW_INVALID")
    return float(value)


def _published(letter: Mapping[str, object], *, now: float | None) -> bool:
    deadline = letter.get("reply_not_before", 0.0)
    if deadline in (None, ""):
        deadline = 0.0
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return False
    return float(deadline) <= _now_value(now)


def _letter_status(value: object, *, published: bool) -> int:
    if not published:
        return int(OriginalClientLetterStatus.PENDING)
    if isinstance(value, OriginalClientLetterStatus):
        return int(value)
    if type(value) is int:
        try:
            return int(OriginalClientLetterStatus(value))
        except ValueError:
            return int(OriginalClientLetterStatus.FAILED)
    normalized = str(value or "").strip().upper()
    return int(_INTERNAL_LETTER_STATUS.get(normalized, OriginalClientLetterStatus.FAILED))


def _audit_status(value: object) -> int:
    if isinstance(value, OriginalClientAuditStatus):
        return int(value)
    if type(value) is int:
        try:
            return int(OriginalClientAuditStatus(value))
        except ValueError:
            return int(OriginalClientAuditStatus.PASSED)
    normalized = str(value or "").strip().upper()
    return int(_INTERNAL_AUDIT_STATUS.get(normalized, OriginalClientAuditStatus.PASSED))


def _exact_reply_mode(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"spoken_video", "musical_video", "text_letter"}:
        return normalized
    if normalized == "video":
        return "musical_video"
    return "text_letter"


def _safe_local_media_url(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    if port is None or not 1 <= port <= 65535:
        return ""
    prefix = "/toy/media/"
    if not parsed.path.startswith(prefix):
        return ""
    name = parsed.path[len(prefix) :]
    return value if _MEDIA_NAME.fullmatch(name) else ""


def _reply_projection(
    letter: Mapping[str, object],
    *,
    published: bool,
) -> tuple[int, str, str]:
    reply_text = str(letter.get("reply_text") or "") if published else ""
    if not reply_text:
        return int(OriginalClientReplyType.NONE), "", ""

    mode = _exact_reply_mode(letter.get("reply_mode"))
    media_status = str(letter.get("media_status") or "").strip().upper()
    media_url = _safe_local_media_url(letter.get("reply_video_url"))
    if mode not in _VIDEO_MODES or media_status != "COMPLETED" or not media_url:
        # Canonical text remains usable while media is pending or unavailable.
        return int(OriginalClientReplyType.TEXT), reply_text, ""
    if mode == "spoken_video":
        return int(OriginalClientReplyType.SPEECH), reply_text, media_url
    # The local musical-video mode contains generated singing, so the closest
    # original-client semantic is MIX_SVS rather than instrumental MIX_PLAY.
    return int(OriginalClientReplyType.MIX_SVS), reply_text, media_url


def _required_identifier(letter: Mapping[str, object]) -> str:
    value = letter.get("letter_id", letter.get("letterId"))
    if not isinstance(value, str) or not value or len(value) > 256:
        raise OriginalClientContractError("ORIGINAL_CLIENT_LETTER_ID_INVALID")
    return value


def _timestamp(value: object, *, default: int) -> object:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float, str)) and value not in (None, ""):
        return value
    return default


def _material(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    stamp = value.get("stampId", value.get("stamp_id"))
    paper = value.get("paperId", value.get("paper_id"))
    if isinstance(stamp, (str, int)) and not isinstance(stamp, bool):
        result["stampId"] = stamp
    if isinstance(paper, (str, int)) and not isinstance(paper, bool):
        result["paperId"] = paper
    return result


def _summary(letter: Mapping[str, object]) -> str:
    value = (
        letter.get("summary")
        or letter.get("content")
        or letter.get("reply_text")
        or ""
    )
    return str(value)[:50]


def serialize_letter_summary(
    letter: Mapping[str, object],
    *,
    now: float | None = None,
    include_legacy_aliases: bool = False,
) -> dict[str, object]:
    current_time = int(_now_value(now))
    published = _published(letter, now=now)
    reply_type, _reply_text, _media_url = _reply_projection(letter, published=published)
    letter_id = _required_identifier(letter)
    status = _letter_status(letter.get("letter_status", letter.get("letterStatus")), published=published)
    audit_status = _audit_status(letter.get("audit_status", letter.get("auditStatus")))
    created_at = _timestamp(letter.get("created_at", letter.get("createdAt")), default=current_time)
    payload: dict[str, object] = {
        "letterId": letter_id,
        "isRead": 1 if letter.get("is_read", letter.get("isRead", 1)) else 0,
        "letterStatus": status,
        "auditStatus": audit_status,
        "summary": _summary(letter),
        "createdAt": created_at,
        "replyType": reply_type,
    }
    replied_at = letter.get("replied_at", letter.get("repliedAt"))
    if replied_at not in (None, ""):
        payload["repliedAt"] = replied_at
    if include_legacy_aliases:
        exact_mode = _exact_reply_mode(letter.get("reply_mode"))
        payload.update(
            {
                "letter_id": letter_id,
                "is_read": payload["isRead"],
                "letter_status": status,
                "audit_status": audit_status,
                "created_at": created_at,
                "reply_type": reply_type,
                "reply_mode": "text" if exact_mode == "text_letter" else "video",
                "reply_mode_exact": exact_mode,
                "triage": letter.get("triage", {"status": "unavailable"}),
            }
        )
        if "repliedAt" in payload:
            payload["replied_at"] = payload["repliedAt"]
    return payload


def serialize_letter_detail(
    letter: Mapping[str, object],
    *,
    now: float | None = None,
    scope: str = "current",
    include_legacy_aliases: bool = False,
) -> dict[str, object]:
    payload = serialize_letter_summary(
        letter,
        now=now,
        include_legacy_aliases=include_legacy_aliases,
    )
    published = _published(letter, now=now)
    reply_type, reply_text, media_url = _reply_projection(letter, published=published)
    material = _material(letter.get("material", {}))
    payload.update(
        {
            "material": material,
            "content": str(letter.get("content") or ""),
            "replyText": reply_text,
            "replyType": reply_type,
            "replyVideoUrl": media_url,
        }
    )
    if include_legacy_aliases:
        payload.update(
            {
                "reply_text": reply_text,
                "reply_content": reply_text,
                "reply_type": reply_type,
                "reply_video_url": media_url,
                "media_status": letter.get("media_status", "NOT_REQUESTED"),
                "media_error_code": letter.get("media_error_code"),
                "scope": scope,
                "read_only": scope == "legacy",
            }
        )
    return payload


def serialize_letter_list(
    letters: Iterable[Mapping[str, object]],
    *,
    remaining_today: int,
    scope: str,
    now: float | None = None,
    include_legacy_aliases: bool = False,
) -> dict[str, object]:
    if type(remaining_today) is not int or remaining_today < 0:
        raise OriginalClientContractError("ORIGINAL_CLIENT_REMAINING_INVALID")
    serialized = [
        serialize_letter_summary(
            letter,
            now=now,
            include_legacy_aliases=include_legacy_aliases,
        )
        for letter in letters
    ]
    payload: dict[str, object] = {
        "list": serialized,
        "total": len(serialized),
        "hasMore": False,
        "nextCursor": 0,
        "remainingToday": remaining_today,
    }
    if include_legacy_aliases:
        payload.update(
            {
                "has_more": False,
                "next_cursor": 0,
                "remaining_today": remaining_today,
                "scope": scope,
                "source": "read-only-legacy" if scope == "legacy" else "local-memory",
                "read_only": scope == "legacy",
            }
        )
    return payload


def serialize_unread_count(
    unread_count: int,
    *,
    scope: str,
    include_legacy_aliases: bool = False,
) -> dict[str, object]:
    if type(unread_count) is not int or unread_count < 0:
        raise OriginalClientContractError("ORIGINAL_CLIENT_UNREAD_INVALID")
    payload: dict[str, object] = {"unreadCount": unread_count}
    if include_legacy_aliases:
        payload.update(
            {
                "unread_count": unread_count,
                "scope": scope,
                "read_only": scope == "legacy",
            }
        )
    return payload


__all__ = [
    "OriginalClientAuditStatus",
    "OriginalClientContractError",
    "OriginalClientLetterStatus",
    "OriginalClientReplyType",
    "serialize_letter_detail",
    "serialize_letter_list",
    "serialize_letter_summary",
    "serialize_unread_count",
]
