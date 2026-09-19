"""Offline, reproducible streaming-ASR metrics."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from .contracts import AsrEvent, assert_monotonic_timestamps


def normalize_transcript(text: str) -> str:
    """Normalize case, compatibility forms, punctuation, and whitespace."""

    text = unicodedata.normalize("NFKC", text).casefold()
    kept: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if category.startswith("P") or category.startswith("S"):
            kept.append(" ")
        else:
            kept.append(char)
    return re.sub(r"\s+", " ", "".join(kept)).strip()


def _tokens(text: str) -> list[str]:
    normalized = normalize_transcript(text)
    if not normalized:
        return []
    if any(char.isspace() for char in normalized):
        return normalized.split()
    return list(normalized)


def edit_distance(reference: Iterable[str], hypothesis: Iterable[str]) -> int:
    ref = list(reference)
    hyp = list(hypothesis)
    row = list(range(len(hyp) + 1))
    for i, ref_token in enumerate(ref, 1):
        next_row = [i]
        for j, hyp_token in enumerate(hyp, 1):
            next_row.append(
                min(
                    next_row[-1] + 1,
                    row[j] + 1,
                    row[j - 1] + (ref_token != hyp_token),
                )
            )
        row = next_row
    return row[-1]


def wer(reference: str, hypothesis: str) -> float:
    ref = _tokens(reference)
    return edit_distance(ref, _tokens(hypothesis)) / len(ref) if ref else float("nan")


def cer(reference: str, hypothesis: str) -> float:
    ref = list(normalize_transcript(reference))
    return edit_distance(ref, list(normalize_transcript(hypothesis))) / len(ref) if ref else float("nan")


def _prefix_stability(partial: str, final: str) -> float:
    partial_text = normalize_transcript(partial)
    final_text = normalize_transcript(final)
    if not partial_text:
        return 1.0 if not final_text else 0.0
    common = 0
    for left, right in zip(partial_text, final_text):
        if left != right:
            break
        common += 1
    return common / len(partial_text)


def measure_events(events: Iterable[AsrEvent]) -> dict[str, Any]:
    event_list = list(events)
    assert_monotonic_timestamps(event_list)
    session_timestamp = next((event.timestamp_ms for event in event_list if event.type == "session"), 0.0)
    partials = [event for event in event_list if event.type == "partial"]
    finals = [event for event in event_list if event.type == "final"]
    latest_partial = partials[-1] if partials else None
    final = finals[-1] if finals else None
    stability = None
    if final is not None:
        stability = _prefix_stability(latest_partial.text, final.text) if latest_partial else None
    return {
        "event_count": len(event_list),
        "partial_count": len(partials),
        "final_count": len(finals),
        "first_partial_latency_ms": (
            round(partials[0].timestamp_ms - session_timestamp, 3) if partials else None
        ),
        "first_final_latency_ms": round(finals[0].timestamp_ms - session_timestamp, 3) if finals else None,
        "partial_to_final_stability": stability,
        "timestamp_monotonic": True,
        "last_audio_ms": event_list[-1].audio_ms if event_list else 0.0,
    }
