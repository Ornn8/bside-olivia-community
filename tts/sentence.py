"""Deterministic sentence-level segmentation for local TTS."""

from __future__ import annotations

import re

from .contracts import TTSValidationError


_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？!?；;.!?])\s+|(?<=[。！？!?；;.!?])(?=[^。！？!?；;.!?\s])")


def split_sentences(text: str, *, max_chars: int = 12000) -> tuple[str, ...]:
    """Split text into non-empty units while retaining punctuation.

    A very long unit is split at whitespace/comma boundaries where possible;
    this keeps the provider's streaming contract sentence-oriented without
    silently dropping text.
    """

    if not isinstance(text, str) or not text.strip():
        raise TTSValidationError("TTS_EMPTY_INPUT", "text is required")
    if len(text) > max_chars:
        raise TTSValidationError("TTS_INPUT_TOO_LONG", "text exceeds the profile limit")

    raw_units = [part.strip() for part in _SENTENCE_BOUNDARY.split(text) if part.strip()]
    if not raw_units:
        raise TTSValidationError("TTS_EMPTY_INPUT", "text is required")

    units: list[str] = []
    for unit in raw_units:
        if len(unit) <= 240:
            units.append(unit)
            continue
        remainder = unit
        while len(remainder) > 240:
            cut = max(
                remainder.rfind(" ", 0, 240),
                remainder.rfind("，", 0, 240),
                remainder.rfind(",", 0, 240),
                remainder.rfind("、", 0, 240),
            )
            if cut < 32:
                cut = 240
            units.append(remainder[:cut].strip())
            remainder = remainder[cut:].strip()
        if remainder:
            units.append(remainder)
    return tuple(units)
