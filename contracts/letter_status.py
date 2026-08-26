"""Pure mapping between internal letter states and the original client wire enum."""

from __future__ import annotations

from enum import IntEnum


class OriginalLetterStatus(IntEnum):
    PENDING = 1
    REPLIED = 4
    FAILED = 5


_INTERNAL_TO_WIRE = {
    "PENDING": OriginalLetterStatus.PENDING,
    "PROCESSING": OriginalLetterStatus.PENDING,
    "COMPLETED": OriginalLetterStatus.REPLIED,
    "REPLIED": OriginalLetterStatus.REPLIED,
    "FAILED": OriginalLetterStatus.FAILED,
    "CANCELED": OriginalLetterStatus.FAILED,
    "CANCELLED": OriginalLetterStatus.FAILED,
}


class LetterStatusError(ValueError):
    code = "LETTER_STATUS_INVALID"


def original_letter_status(value: object) -> int:
    """Return the exact numeric status expected by the original frontend.

    Existing imported records may already contain one of the public numeric
    values. Unknown strings, booleans, arbitrary integers, and unrelated enum
    types are rejected so callers cannot silently publish a misleading state.
    """

    if isinstance(value, OriginalLetterStatus):
        return int(value)
    if type(value) is int:
        try:
            return int(OriginalLetterStatus(value))
        except ValueError as exc:
            raise LetterStatusError("unsupported numeric letter status") from exc
    if not isinstance(value, str):
        raise LetterStatusError("letter status must be text or a known wire value")
    normalized = value.strip().upper()
    try:
        return int(_INTERNAL_TO_WIRE[normalized])
    except KeyError as exc:
        raise LetterStatusError("unsupported internal letter status") from exc


def original_letter_status_or_failed(value: object) -> int:
    """Fail closed for public serialization without raising provider details."""

    try:
        return original_letter_status(value)
    except LetterStatusError:
        return int(OriginalLetterStatus.FAILED)


__all__ = [
    "LetterStatusError",
    "OriginalLetterStatus",
    "original_letter_status",
    "original_letter_status_or_failed",
]
