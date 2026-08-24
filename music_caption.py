"""Deterministic positive-only MiniMax Music 3 caption rendering."""

from __future__ import annotations

import re

from music_duration import normalize_music_duration
from song_content import (
    PianoTexture,
    SongDynamicArc,
    SongEmotionArc,
    SongEnding,
    SongSemanticPlan,
    VocalDelivery,
)


MINIMAX_CAPTION_VERSION = "p03.minimax-caption.v2"

_EMOTION = {
    SongEmotionArc.QUIET_LONGING: (
        "The emotional motion begins in quiet longing, opens slightly around "
        "the middle, and settles with composure."
    ),
    SongEmotionArc.GENTLE_REASSURANCE: (
        "The emotional motion begins with attentive tenderness, gains gentle "
        "reassurance through the middle, and settles calmly."
    ),
    SongEmotionArc.RESTRAINED_SADNESS: (
        "The emotional motion holds restrained sadness, gathers a little "
        "weight through the middle, and returns to a settled close."
    ),
    SongEmotionArc.WARM_GRATITUDE: (
        "The emotional motion begins with quiet appreciation, grows into warm "
        "gratitude, and resolves with an unhurried sense of closeness."
    ),
    SongEmotionArc.SOFT_RECONCILIATION: (
        "The emotional motion begins with careful distance, softens through "
        "reconciliation, and reaches a calm, complete resolution."
    ),
    SongEmotionArc.CALM_AFFECTION: (
        "The emotional motion carries calm affection from the opening, gains "
        "gentle warmth in the middle, and settles into a tender close."
    ),
}

_PIANO_TEXTURE = {
    PianoTexture.TRANSPARENT_BROKEN_CHORDS: (
        "The left hand establishes a simple low-register tonal foundation "
        "while the right hand shapes transparent broken chords and brief "
        "lyrical answers."
    ),
    PianoTexture.LYRICAL_ARPEGGIOS: (
        "The left hand establishes measured tonal support while the right hand "
        "unfolds lyrical arpeggios with clear phrase direction and open space."
    ),
    PianoTexture.MEASURED_CHORDAL_VOICING: (
        "The piano uses measured chordal voicings, clear inner movement, and "
        "well-spaced phrase endings to support the vocal melody."
    ),
    PianoTexture.SPARSE_COUNTERLINE: (
        "The piano combines sustained voicings with a sparse lyrical "
        "counterline, leaving deliberate space around each vocal phrase."
    ),
}

_VOCAL = {
    VocalDelivery.CLEAR_LEGATO: (
        "One adult female Mandarin lead carries a clear centered melody with "
        "natural diction, mostly syllabic legato phrasing, a moderate range, "
        "and measured phrase endings."
    ),
    VocalDelivery.GENTLE_NARRATIVE: (
        "One adult female Mandarin lead delivers a gentle narrative melody "
        "with clear consonants, restrained sustained notes, conversational "
        "pacing, and precise phrase endings."
    ),
    VocalDelivery.QUIET_SONGFUL: (
        "One adult female Mandarin lead sings a quiet songful line with rounded "
        "vowels, smooth sustained notes, steady melodic focus, and controlled "
        "dynamic shaping."
    ),
    VocalDelivery.CONTAINED_INTIMATE: (
        "One adult female Mandarin lead uses a contained intimate tone, close "
        "phrasing, controlled dynamics, calm projection, and precise natural "
        "diction."
    ),
}

_DYNAMIC = {
    SongDynamicArc.SOFT_GENTLE_RISE_SETTLE: (
        "The performance begins softly, broadens through slightly fuller "
        "voicing and note density near the middle, then returns to a soft "
        "settled profile."
    ),
    SongDynamicArc.SOFT_STEADY_SETTLE: (
        "The performance maintains a soft steady profile, using small changes "
        "in register and voicing before easing into the closing phrase."
    ),
    SongDynamicArc.QUIET_GRADUAL_WARMTH: (
        "The performance begins quietly, gains gradual warmth through fuller "
        "voicing and longer resonance, then settles with restraint."
    ),
}

_ENDING = {
    SongEnding.COMPLETE_SOFT_CADENCE: (
        "The final phrase resolves through a complete soft piano cadence."
    ),
    SongEnding.LINGERING_PIANO_CADENCE: (
        "The final phrase resolves through a lingering piano cadence with "
        "natural decay."
    ),
    SongEnding.SHORT_SETTLED_CADENCE: (
        "The final phrase resolves through a short settled piano cadence."
    ),
}

_TIMELINE = {
    40: (
        "0-4 seconds piano opening; 4-25 seconds one verse and one chorus "
        "from 25-35 seconds; 35-40 seconds closing cadence."
    ),
    60: (
        "0-5 seconds piano opening; 5-36 seconds one verse and one chorus "
        "from 36-52 seconds; 52-60 seconds closing cadence."
    ),
}

_HEADINGS = ("### Global Metadata", "### Vocal Details", "### Arrangement")
_NEGATIVE_SYNTAX = re.compile(
    r"\b(?:no|not|without|avoid|exclude|excluding|never|absence|lack)\b",
    flags=re.IGNORECASE,
)
_DISALLOWED_TERMS = re.compile(
    r"\b(?:"
    r"r&b|rnb|neo[- ]?soul|soul|jazz|gospel|cinematic|heritage|folk|pop|"
    r"electronic|ambient|orchestral|orchestra|groove|swing|syncopated|"
    r"backbeat|melisma|riff|ad[- ]?lib|drums?|percussion|bass|guitars?|"
    r"strings?|cello|violins?|synth(?:esizer)?s?|pads?|choir|guzheng|"
    r"erhu|pipa|dizi|flute"
    r")\b",
    flags=re.IGNORECASE,
)


def validate_minimax_caption(caption: str, duration_seconds: int) -> str:
    """Validate the rendered positive caption before it reaches MiniMax."""

    duration = normalize_music_duration(duration_seconds)
    if not isinstance(caption, str) or not caption.strip():
        raise ValueError("MINIMAX_CAPTION_EMPTY")
    normalized = caption.strip()
    if len(normalized.encode("utf-8")) > 4096:
        raise ValueError("MINIMAX_CAPTION_TOO_LARGE")
    headings = tuple(re.findall(r"^### .+$", normalized, flags=re.MULTILINE))
    if headings != _HEADINGS:
        raise ValueError("MINIMAX_CAPTION_HEADINGS_INVALID")
    required = (
        f"{duration} seconds",
        "68 BPM",
        "B-flat major",
        "straight 4/4",
        "adult female Mandarin lead",
        "one acoustic grand piano",
    )
    if any(token.casefold() not in normalized.casefold() for token in required):
        raise ValueError("MINIMAX_CAPTION_REQUIRED_CONTENT_MISSING")
    if _NEGATIVE_SYNTAX.search(normalized):
        raise ValueError("MINIMAX_CAPTION_NEGATIVE_SYNTAX")
    if _DISALLOWED_TERMS.search(normalized):
        raise ValueError("MINIMAX_CAPTION_DISALLOWED_TERM")
    if "[" in normalized or "]" in normalized:
        raise ValueError("MINIMAX_CAPTION_LYRIC_TAG_LEAK")
    if len(normalized.split()) > 300:
        raise ValueError("MINIMAX_CAPTION_WORD_LIMIT")
    return normalized


def render_minimax_caption(plan: SongSemanticPlan) -> str:
    """Render one deterministic caption from audited semantic enums."""

    if not isinstance(plan, SongSemanticPlan):
        raise TypeError("MINIMAX_CAPTION_PLAN_TYPE_INVALID")
    caption = f"""### Global Metadata
An intimate Mandarin vocal-and-acoustic-grand-piano lyrical song lasting {plan.duration_seconds} seconds at 68 BPM in B-flat major, straight 4/4. {_EMOTION[plan.emotion_arc]} The recording presents close, natural small-room acoustics, a transparent tonal balance, and a complete compact shape.

### Vocal Details
{_VOCAL[plan.vocal_delivery]} The melodic delivery remains personal, restrained, and consistent across one verse and one chorus.

### Arrangement
The complete instrumental arrangement is performed by one acoustic grand piano from the opening through the final cadence. {_PIANO_TEXTURE[plan.piano_texture]} {_DYNAMIC[plan.dynamic_arc]} Section timing: {_TIMELINE[plan.duration_seconds]} {_ENDING[plan.ending]}"""
    return validate_minimax_caption(caption, plan.duration_seconds)


__all__ = [
    "MINIMAX_CAPTION_VERSION",
    "render_minimax_caption",
    "validate_minimax_caption",
]
