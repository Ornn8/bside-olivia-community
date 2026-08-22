from __future__ import annotations

import json

import pytest

from song_content import (
    PianoTexture,
    SONG_SEMANTIC_PLAN_SCHEMA_VERSION,
    SongDynamicArc,
    SongEmotionArc,
    SongEnding,
    SongSemanticPlan,
    VocalDelivery,
    parse_song_semantic_plan,
)


def _lyrics(duration: int) -> str:
    per_verse = 6 if duration == 90 else 8
    first = [f"第一段第{index}句轻轻落下" for index in range(1, per_verse + 1)]
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


def _payload(duration: int = 90) -> dict[str, str]:
    return {
        "schema_version": SONG_SEMANTIC_PLAN_SCHEMA_VERSION,
        "emotion_arc": "gentle_reassurance",
        "piano_texture": "transparent_broken_chords",
        "vocal_delivery": "clear_legato",
        "dynamic_arc": "soft_gentle_rise_settle",
        "ending": "complete_soft_cadence",
        "lyrics": _lyrics(duration),
    }


@pytest.mark.parametrize("duration", [90, 118])
def test_parse_song_semantic_plan_accepts_strict_plain_and_fenced_json(
    duration: int,
) -> None:
    payload = _payload(duration)
    plain = parse_song_semantic_plan(
        json.dumps(payload, ensure_ascii=False),
        duration,
    )
    fenced = parse_song_semantic_plan(
        "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```",
        duration,
    )

    assert plain == fenced
    assert plain.duration_seconds == duration
    assert plain.emotion_arc is SongEmotionArc.GENTLE_REASSURANCE
    assert plain.piano_texture is PianoTexture.TRANSPARENT_BROKEN_CHORDS
    assert plain.vocal_delivery is VocalDelivery.CLEAR_LEGATO
    assert plain.dynamic_arc is SongDynamicArc.SOFT_GENTLE_RISE_SETTLE
    assert plain.ending is SongEnding.COMPLETE_SOFT_CADENCE
    assert plain.to_dict() == {**payload, "duration_seconds": duration}


@pytest.mark.parametrize(
    ("mutate", "error_code"),
    [
        (lambda value: value.pop("ending"), "SONG_SEMANTIC_PLAN_FIELDS_INVALID"),
        (
            lambda value: value.__setitem__("caption", "not allowed"),
            "SONG_SEMANTIC_PLAN_FIELDS_INVALID",
        ),
        (
            lambda value: value.__setitem__("emotion_arc", 7),
            "SONG_SEMANTIC_PLAN_FIELD_TYPE_INVALID",
        ),
        (
            lambda value: value.__setitem__("schema_version", "future"),
            "SONG_SEMANTIC_PLAN_SCHEMA_UNSUPPORTED",
        ),
        (
            lambda value: value.__setitem__("emotion_arc", "dramatic"),
            "SONG_SEMANTIC_PLAN_ENUM_INVALID",
        ),
        (
            lambda value: value.__setitem__("piano_texture", "full_orchestra"),
            "SONG_SEMANTIC_PLAN_ENUM_INVALID",
        ),
    ],
)
def test_parse_song_semantic_plan_rejects_invalid_contract(
    mutate,
    error_code: str,
) -> None:
    payload = _payload()
    mutate(payload)

    with pytest.raises(ValueError, match=error_code):
        parse_song_semantic_plan(json.dumps(payload, ensure_ascii=False), 90)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "[]",
        "before " + json.dumps(_payload(), ensure_ascii=False),
        json.dumps(_payload(), ensure_ascii=False) + " after",
        "```json\n{}\n```\nextra",
        "```python\n{}\n```",
    ],
)
def test_parse_song_semantic_plan_rejects_missing_or_wrapped_non_json(
    text: str,
) -> None:
    with pytest.raises(ValueError, match="SONG_SEMANTIC_PLAN_JSON_"):
        parse_song_semantic_plan(text, 90)


@pytest.mark.parametrize("duration", [0, 89, 91, 118.0, True])
def test_parse_song_semantic_plan_reuses_product_duration_contract(duration) -> None:
    with pytest.raises(ValueError, match="MUSIC_DURATION_INVALID"):
        parse_song_semantic_plan(json.dumps(_payload(), ensure_ascii=False), duration)


def test_semantic_lyrics_require_exact_empty_nonverse_sections() -> None:
    payload = _payload()
    payload["lyrics"] = payload["lyrics"].replace(
        "[Intro]\n[Verse]",
        "[Intro]\n钢琴先说一句\n[Verse]",
    )

    with pytest.raises(
        ValueError,
        match="SONG_SEMANTIC_PLAN_LYRICS_NONVERSE_CONTENT",
    ):
        parse_song_semantic_plan(json.dumps(payload, ensure_ascii=False), 90)


def test_semantic_lyrics_require_balanced_verse_line_counts() -> None:
    payload = _payload()
    lines = payload["lyrics"].splitlines()
    first_interlude = lines.index("[Interlude]")
    moved = lines.pop(first_interlude - 1)
    second_verse = lines.index("[Verse]", 2)
    lines.insert(second_verse + 1, moved)
    payload["lyrics"] = "\n".join(lines)

    with pytest.raises(
        ValueError,
        match="SONG_SEMANTIC_PLAN_LYRICS_LINE_COUNT_INVALID",
    ):
        parse_song_semantic_plan(json.dumps(payload, ensure_ascii=False), 90)


@pytest.mark.parametrize(
    ("replacement", "error_code"),
    [
        ("啊", "SONG_SEMANTIC_PLAN_LYRIC_LINE_LENGTH_INVALID"),
        ("plain english line", "SONG_SEMANTIC_PLAN_LYRIC_LINE_LANGUAGE_INVALID"),
        ("这句[Bridge]不合法", "SONG_SEMANTIC_PLAN_LYRIC_LINE_INVALID"),
        ("这句含有\t控制符", "SONG_SEMANTIC_PLAN_LYRICS_CONTROL_CHARACTER"),
    ],
)
def test_semantic_lyrics_reject_invalid_line_content(
    replacement: str,
    error_code: str,
) -> None:
    payload = _payload()
    payload["lyrics"] = payload["lyrics"].replace(
        "第一段第1句轻轻落下",
        replacement,
    )

    with pytest.raises(ValueError, match=error_code):
        parse_song_semantic_plan(json.dumps(payload, ensure_ascii=False), 90)


def test_song_semantic_plan_is_typed_and_caption_free() -> None:
    plan = SongSemanticPlan(
        emotion_arc=SongEmotionArc.CALM_AFFECTION,
        piano_texture=PianoTexture.LYRICAL_ARPEGGIOS,
        vocal_delivery=VocalDelivery.CONTAINED_INTIMATE,
        dynamic_arc=SongDynamicArc.QUIET_GRADUAL_WARMTH,
        ending=SongEnding.LINGERING_PIANO_CADENCE,
        lyrics=_lyrics(118),
        duration_seconds=118,
    )

    assert "caption" not in plan.to_dict()
    with pytest.raises(TypeError, match="EMOTION_ARC_TYPE_INVALID"):
        SongSemanticPlan(
            emotion_arc="calm_affection",  # type: ignore[arg-type]
            piano_texture=PianoTexture.LYRICAL_ARPEGGIOS,
            vocal_delivery=VocalDelivery.CONTAINED_INTIMATE,
            dynamic_arc=SongDynamicArc.QUIET_GRADUAL_WARMTH,
            ending=SongEnding.LINGERING_PIANO_CADENCE,
            lyrics=_lyrics(118),
            duration_seconds=118,
        )
