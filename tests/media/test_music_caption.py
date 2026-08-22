from __future__ import annotations

from itertools import product

import pytest

from music_caption import (
    MINIMAX_CAPTION_VERSION,
    render_minimax_caption,
    validate_minimax_caption,
)
from song_content import (
    PianoTexture,
    SongDynamicArc,
    SongEmotionArc,
    SongEnding,
    SongSemanticPlan,
    VocalDelivery,
)


def _lyrics(duration: int, marker: str = "不会进入音乐描述") -> str:
    per_verse = 6 if duration == 90 else 8
    first = [f"第一段第{index}句轻轻落下" for index in range(1, per_verse + 1)]
    first[0] = marker
    second = [f"第二段第{index}句慢慢收好" for index in range(1, per_verse + 1)]
    return "\n".join(
        (
            "[Intro]",
            "[Verse]",
            *first,
            "[Interlude]",
            "[Verse]",
            *second,
            "[Outro]",
        )
    )


def _plan(
    *,
    duration: int = 90,
    emotion: SongEmotionArc = SongEmotionArc.GENTLE_REASSURANCE,
    texture: PianoTexture = PianoTexture.TRANSPARENT_BROKEN_CHORDS,
    vocal: VocalDelivery = VocalDelivery.CLEAR_LEGATO,
    dynamic: SongDynamicArc = SongDynamicArc.SOFT_GENTLE_RISE_SETTLE,
    ending: SongEnding = SongEnding.COMPLETE_SOFT_CADENCE,
) -> SongSemanticPlan:
    return SongSemanticPlan(
        emotion_arc=emotion,
        piano_texture=texture,
        vocal_delivery=vocal,
        dynamic_arc=dynamic,
        ending=ending,
        lyrics=_lyrics(duration),
        duration_seconds=duration,
    )


def test_caption_version_is_explicit() -> None:
    assert MINIMAX_CAPTION_VERSION == "p03.minimax-caption.v1"


@pytest.mark.parametrize("duration", [90, 118])
def test_render_minimax_caption_is_deterministic_positive_and_caption_only(
    duration: int,
) -> None:
    plan = _plan(duration=duration)
    first = render_minimax_caption(plan)
    second = render_minimax_caption(plan)

    assert first == second
    assert "### Global Metadata" in first
    assert "### Vocal Details" in first
    assert "### Arrangement" in first
    assert f"{duration} seconds" in first
    assert "one acoustic grand piano" in first.casefold()
    assert "adult female Mandarin lead" in first
    assert "不会进入音乐描述" not in first
    assert "[Verse]" not in first
    assert len(first.split()) <= 300


def test_render_minimax_caption_snapshot() -> None:
    caption = render_minimax_caption(_plan())
    assert caption == """### Global Metadata
An intimate Mandarin vocal-and-acoustic-grand-piano lyrical song lasting 90 seconds at 68 BPM in B-flat major, straight 4/4. The emotional motion begins with attentive tenderness, gains gentle reassurance through the middle, and settles calmly. The recording presents close, natural small-room acoustics, a transparent tonal balance, and a complete long-form shape.

### Vocal Details
One adult female Mandarin lead carries a clear centered melody with natural diction, mostly syllabic legato phrasing, a moderate range, and measured phrase endings. The melodic delivery remains personal, restrained, and consistent across both verses.

### Arrangement
The complete instrumental arrangement is performed by one acoustic grand piano from the opening through the final cadence. The left hand establishes a simple low-register tonal foundation while the right hand shapes transparent broken chords and brief lyrical answers. The performance begins softly, broadens through slightly fuller voicing and note density near the middle, then returns to a soft settled profile. Section timing: 0-8 seconds piano opening; 8-38 seconds first verse; 38-44 seconds piano interlude; 44-78 seconds second verse; 78-90 seconds closing cadence. The final phrase resolves through a complete soft piano cadence."""


def test_all_audited_enum_combinations_render_and_validate() -> None:
    for duration, emotion, texture, vocal, dynamic, ending in product(
        (90, 118),
        SongEmotionArc,
        PianoTexture,
        VocalDelivery,
        SongDynamicArc,
        SongEnding,
    ):
        caption = render_minimax_caption(
            _plan(
                duration=duration,
                emotion=emotion,
                texture=texture,
                vocal=vocal,
                dynamic=dynamic,
                ending=ending,
            )
        )
        assert validate_minimax_caption(caption, duration) == caption


@pytest.mark.parametrize(
    ("mutate", "error_code"),
    [
        (
            lambda caption: caption.replace("### Arrangement", "### Production"),
            "MINIMAX_CAPTION_HEADINGS_INVALID",
        ),
        (
            lambda caption: caption.replace("68 BPM", "72 BPM"),
            "MINIMAX_CAPTION_REQUIRED_CONTENT_MISSING",
        ),
        (
            lambda caption: caption + "\nNo drums.",
            "MINIMAX_CAPTION_NEGATIVE_SYNTAX",
        ),
        (
            lambda caption: caption + "\nSoft strings enter.",
            "MINIMAX_CAPTION_DISALLOWED_TERM",
        ),
        (
            lambda caption: caption + "\n[Verse]",
            "MINIMAX_CAPTION_LYRIC_TAG_LEAK",
        ),
    ],
)
def test_validate_minimax_caption_rejects_tampered_output(
    mutate,
    error_code: str,
) -> None:
    caption = render_minimax_caption(_plan())
    with pytest.raises(ValueError, match=error_code):
        validate_minimax_caption(mutate(caption), 90)


def test_render_minimax_caption_requires_typed_plan() -> None:
    with pytest.raises(TypeError, match="MINIMAX_CAPTION_PLAN_TYPE_INVALID"):
        render_minimax_caption({})  # type: ignore[arg-type]


@pytest.mark.parametrize("duration", [0, 89, 91, 118.0, True])
def test_validate_minimax_caption_reuses_product_duration_contract(duration) -> None:
    with pytest.raises(ValueError, match="MUSIC_DURATION_INVALID"):
        validate_minimax_caption(render_minimax_caption(_plan()), duration)
