from __future__ import annotations

from itertools import product

import pytest

from runtime.media.music_caption import (
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


def _short_lyrics(duration: int, marker: str = "不会进入音乐描述") -> str:
    verse_count, chorus_count = ((6, 6) if duration == 40 else (8, 8))
    verse = [f"主歌第{index}句轻轻落下" for index in range(1, verse_count + 1)]
    verse[0] = marker
    chorus = [f"副歌第{index}句慢慢收好" for index in range(1, chorus_count + 1)]
    return "\n".join(
        ("[Intro]", "[Verse]", *verse, "[Chorus]", *chorus, "[Outro]")
    )


def _plan(
    *,
    duration: int = 40,
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
        lyrics=_short_lyrics(duration),
        duration_seconds=duration,
    )


def test_caption_version_is_explicit() -> None:
    assert MINIMAX_CAPTION_VERSION == "p03.minimax-caption.v2"
    assert MINIMAX_CAPTION_VERSION != "p03.minimax-caption.v1"


def test_short_caption_describes_one_full_verse_chorus_and_fade_ready_ending() -> None:
    plan = SongSemanticPlan(
        emotion_arc=SongEmotionArc.GENTLE_REASSURANCE,
        piano_texture=PianoTexture.TRANSPARENT_BROKEN_CHORDS,
        vocal_delivery=VocalDelivery.CLEAR_LEGATO,
        dynamic_arc=SongDynamicArc.SOFT_GENTLE_RISE_SETTLE,
        ending=SongEnding.COMPLETE_SOFT_CADENCE,
        lyrics="\n".join(
            (
                "[Intro]",
                "[Verse]",
                "第一句轻轻落下",
                "第二句慢慢说完",
                "第三句留住灯光",
                "第四句不催答案",
                "第五句回到此刻",
                "第六句温柔收好",
                "[Chorus]",
                "第七句留住呼吸",
                "第八句走向副歌",
                "第九句唱出回答",
                "第十句接住心事",
                "第十一句不说永远",
                "第十二句让琴声渐远",
                "[Outro]",
            )
        ),
        duration_seconds=40,
    )

    caption = render_minimax_caption(plan)

    assert "lasting 40 seconds" in caption
    assert "one verse and one chorus" in caption
    assert "second verse" not in caption
    assert "interlude" not in caption


@pytest.mark.parametrize("duration", [40, 60])
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
An intimate Mandarin vocal-and-acoustic-grand-piano lyrical song lasting 40 seconds at 68 BPM in B-flat major, straight 4/4. The emotional motion begins with attentive tenderness, gains gentle reassurance through the middle, and settles calmly. The recording presents close, natural small-room acoustics, a transparent tonal balance, and a complete compact shape.

### Vocal Details
One adult female Mandarin lead carries a clear centered melody with natural diction, mostly syllabic legato phrasing, a moderate range, and measured phrase endings. The melodic delivery remains personal, restrained, and consistent across one verse and one chorus.

### Arrangement
The complete instrumental arrangement is performed by one acoustic grand piano from the opening through the final cadence. The left hand establishes a simple low-register tonal foundation while the right hand shapes transparent broken chords and brief lyrical answers. The performance begins softly, broadens through slightly fuller voicing and note density near the middle, then returns to a soft settled profile. Section timing: 0-4 seconds piano opening; 4-25 seconds one verse and one chorus from 25-35 seconds; 35-40 seconds closing cadence. The final phrase resolves through a complete soft piano cadence."""


def test_all_audited_enum_combinations_render_and_validate() -> None:
    for duration, emotion, texture, vocal, dynamic, ending in product(
        (40, 60),
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
        validate_minimax_caption(mutate(caption), 40)


def test_render_minimax_caption_requires_typed_plan() -> None:
    with pytest.raises(TypeError, match="MINIMAX_CAPTION_PLAN_TYPE_INVALID"):
        render_minimax_caption({})  # type: ignore[arg-type]


@pytest.mark.parametrize("duration", [0, 39, 41, 60.0, True])
def test_validate_minimax_caption_reuses_product_duration_contract(duration) -> None:
    with pytest.raises(ValueError, match="MUSIC_DURATION_INVALID"):
        validate_minimax_caption(render_minimax_caption(_plan()), duration)
