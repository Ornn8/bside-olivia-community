"""Product-level duration choices shared by song planning and rendering."""

from __future__ import annotations


MUSIC_DURATION_OPTIONS = (40, 60)


def normalize_music_duration(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in MUSIC_DURATION_OPTIONS:
        raise ValueError("MUSIC_DURATION_INVALID")
    return value


__all__ = ["MUSIC_DURATION_OPTIONS", "normalize_music_duration"]
