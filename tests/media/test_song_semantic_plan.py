from __future__ import annotations

import json

import pytest

from runtime.media.song_content import (
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


def _short_lyrics(duration: int) -> str:
    verse_count, chorus_count = {40: (6, 6), 60: (8, 8), 110: (12, 12)}[duration]
    return "\n".join(
        (
            "[Intro]",
            "[Verse]",
            *(f"主歌第{index}句轻轻落下" for index in range(1, verse_count + 1)),
            "[Chorus]",
            *(f"副歌第{index}句慢慢收好" for index in range(1, chorus_count + 1)),
            "[Outro]",
        )
    )


def _payload(duration: int = 40) -> dict[str, str]:
    return {
        "schema_version": SONG_SEMANTIC_PLAN_SCHEMA_VERSION,
        "emotion_arc": "gentle_reassurance",
        "piano_texture": "transparent_broken_chords",
        "vocal_delivery": "clear_legato",
        "dynamic_arc": "soft_gentle_rise_settle",
        "ending": "complete_soft_cadence",
        "lyrics": _short_lyrics(duration),
    }


def test_short_song_keeps_a_full_verse_and_chorus_without_a_second_verse() -> None:
    payload = {
        **_payload(),
        "lyrics": "\n".join(
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
    }

    plan = parse_song_semantic_plan(
        json.dumps(payload, ensure_ascii=False),
        40,
    )

    assert plan.duration_seconds == 40
    assert plan.lyrics.count("[Verse]") == 1
    assert plan.lyrics.count("[Chorus]") == 1
    assert "[Interlude]" not in plan.lyrics


@pytest.mark.parametrize("duration", [40, 60, 110])
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


def test_full_song_requires_original_110_second_lyrics_instead_of_short_plan():
    from runtime.media.song_content import _planner_contract

    with pytest.raises(ValueError, match="SONG_SEMANTIC_PLAN_LYRICS_LINE_COUNT_INVALID"):
        parse_song_semantic_plan(json.dumps(_payload(40), ensure_ascii=False), 110)
    contract = _planner_contract(110)
    assert "24 original Simplified Chinese lyric lines: 12 in Verse and 12 in Chorus" in contract


def test_model_writes_only_sung_lines_and_program_builds_empty_intro_outro():
    from runtime.media.song_content import _plan_from_lyrics_response
    payload = {'verse': ['窗外的晚风轻轻吹来'] * 12, 'chorus': ['让我把这首歌唱给你'] * 12}
    plan = _plan_from_lyrics_response(json.dumps(payload, ensure_ascii=False), 110)
    assert plan.lyrics.startswith('[Intro]\n[Verse]\n')
    assert plan.lyrics.endswith('让我把这首歌唱给你\n[Outro]')
    assert plan.lyrics.count('[Verse]') == plan.lyrics.count('[Chorus]') == 1
    assert plan.ending is SongEnding.LINGERING_PIANO_CADENCE


@pytest.mark.parametrize('change', ['extra', 'line_count', 'embedded_tag', 'not_array'])
def test_structured_lyrics_do_not_allow_model_to_change_arrangement(change):
    from runtime.media.song_content import _plan_from_lyrics_response
    payload = {'verse': ['窗外的晚风轻轻吹来'] * 12, 'chorus': ['让我把这首歌唱给你'] * 12}
    if change == 'extra': payload['outro'] = ['钢琴尾奏']
    if change == 'line_count': payload['verse'].pop()
    if change == 'embedded_tag': payload['verse'][0] = '第一句歌词\n[Outro]'
    if change == 'not_array': payload['verse'] = '不是歌词数组'
    with pytest.raises(ValueError, match='SONG_SEMANTIC_PLAN_'):
        _plan_from_lyrics_response(json.dumps(payload, ensure_ascii=False), 110)


def test_song_plan_repair_receives_failed_output_for_targeted_correction():
    from types import SimpleNamespace
    from runtime.media.song_content import plan_song_content
    invalid = json.dumps({'lyrics': _short_lyrics(40)}, ensure_ascii=False)
    valid = json.dumps({'lyrics': _short_lyrics(110)}, ensure_ascii=False)
    class Gateway:
        def __init__(self):
            self.calls = []
        async def complete(self, messages):
            self.calls.append(messages)
            return SimpleNamespace(text=invalid if len(self.calls) == 1 else valid)
    gateway = Gateway()
    plan = plan_song_content('合成来信', '合成回信', 110, gateway=gateway)
    assert plan.duration_seconds == 110
    assert gateway.calls[1][-2] == {'role': 'assistant', 'content': invalid}
    assert 'only verse and chorus arrays' in gateway.calls[1][-1]['content']


@pytest.mark.parametrize('extra', [None, 'piano_texture', 'emotion_arc', 'ending'])
def test_planner_accepts_only_lyrics_and_fixes_musical_direction_locally(extra):
    from types import SimpleNamespace
    from runtime.media.song_content import plan_song_content
    payload = {'lyrics': _short_lyrics(110)}
    if extra:
        payload[extra] = _payload()[extra]
    class Gateway:
        calls = []
        async def complete(self, messages):
            self.calls.append(messages)
            return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))
    gateway = Gateway()
    if extra:
        with pytest.raises(ValueError, match='SONG_SEMANTIC_PLAN_FIELDS_INVALID'):
            plan_song_content('合成来信', '合成回信', 110, gateway=gateway)
        return
    plan = plan_song_content('合成来信', '合成回信', 110, gateway=gateway)
    assert plan.semantic_plan.emotion_arc is SongEmotionArc.WARM_GRATITUDE
    assert plan.semantic_plan.piano_texture is PianoTexture.LYRICAL_ARPEGGIOS
    assert plan.semantic_plan.vocal_delivery is VocalDelivery.GENTLE_NARRATIVE
    assert plan.semantic_plan.dynamic_arc is SongDynamicArc.SOFT_GENTLE_RISE_SETTLE
    assert plan.semantic_plan.ending is SongEnding.LINGERING_PIANO_CADENCE
    assert plan.lyrics == payload['lyrics']
    assert 'exactly two keys: verse and chorus' in gateway.calls[0][0]['content']


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
        parse_song_semantic_plan(json.dumps(payload, ensure_ascii=False), 40)


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
        parse_song_semantic_plan(text, 40)


@pytest.mark.parametrize("duration", [0, 39, 41, 60.0, True])
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
        parse_song_semantic_plan(json.dumps(payload, ensure_ascii=False), 40)


def test_semantic_lyrics_require_balanced_verse_and_chorus_line_counts() -> None:
    payload = _payload()
    lines = payload["lyrics"].splitlines()
    chorus = lines.index("[Chorus]")
    moved = lines.pop(chorus - 1)
    chorus = lines.index("[Chorus]")
    lines.insert(chorus + 1, moved)
    payload["lyrics"] = "\n".join(lines)

    with pytest.raises(
        ValueError,
        match="SONG_SEMANTIC_PLAN_LYRICS_LINE_COUNT_INVALID",
    ):
        parse_song_semantic_plan(json.dumps(payload, ensure_ascii=False), 40)


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
        "主歌第1句轻轻落下",
        replacement,
    )

    with pytest.raises(ValueError, match=error_code):
        parse_song_semantic_plan(json.dumps(payload, ensure_ascii=False), 40)


def test_song_semantic_plan_is_typed_and_caption_free() -> None:
    plan = SongSemanticPlan(
        emotion_arc=SongEmotionArc.CALM_AFFECTION,
        piano_texture=PianoTexture.LYRICAL_ARPEGGIOS,
        vocal_delivery=VocalDelivery.CONTAINED_INTIMATE,
        dynamic_arc=SongDynamicArc.QUIET_GRADUAL_WARMTH,
        ending=SongEnding.LINGERING_PIANO_CADENCE,
        lyrics=_short_lyrics(60),
        duration_seconds=60,
    )

    assert "caption" not in plan.to_dict()
    with pytest.raises(TypeError, match="EMOTION_ARC_TYPE_INVALID"):
        SongSemanticPlan(
            emotion_arc="calm_affection",  # type: ignore[arg-type]
            piano_texture=PianoTexture.LYRICAL_ARPEGGIOS,
            vocal_delivery=VocalDelivery.CONTAINED_INTIMATE,
            dynamic_arc=SongDynamicArc.QUIET_GRADUAL_WARMTH,
            ending=SongEnding.LINGERING_PIANO_CADENCE,
            lyrics=_short_lyrics(60),
            duration_seconds=60,
        )
