from __future__ import annotations

from pathlib import Path

import pytest

from minimax_profile import (
    CURRENT_MINIMAX_PROFILE,
    MINIMAX_INFERENCE_PROFILE_SCHEMA_VERSION,
    MiniMaxInferenceProfile,
    MiniMaxProfileError,
    OFFICIAL_COMFY_MINIMAX_PROFILE,
    minimax_profile_from_mapping,
)
from music_caption import render_minimax_caption
from song_content import (
    PianoTexture,
    SongDynamicArc,
    SongEmotionArc,
    SongEnding,
    SongSemanticPlan,
    VocalDelivery,
)
from tools import minimax_music3_worker as worker


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


def _request(
    duration: int = 90,
    profile: MiniMaxInferenceProfile | None = None,
) -> dict[str, object]:
    plan = SongSemanticPlan(
        emotion_arc=SongEmotionArc.GENTLE_REASSURANCE,
        piano_texture=PianoTexture.TRANSPARENT_BROKEN_CHORDS,
        vocal_delivery=VocalDelivery.CLEAR_LEGATO,
        dynamic_arc=SongDynamicArc.SOFT_GENTLE_RISE_SETTLE,
        ending=SongEnding.COMPLETE_SOFT_CADENCE,
        lyrics=_lyrics(duration),
        duration_seconds=duration,
    )
    request: dict[str, object] = {
        "max_duration": duration,
        "lyrics": plan.lyrics,
        "caption": render_minimax_caption(plan),
    }
    if profile is not None:
        request["inference_profile"] = profile.to_dict()
    return request


def test_current_and_official_profiles_are_explicit_a_b_candidates() -> None:
    assert CURRENT_MINIMAX_PROFILE.to_dict() == {
        "schema_version": MINIMAX_INFERENCE_PROFILE_SCHEMA_VERSION,
        "name": "current-1.5",
        "seed": 200717,
        "text_cfg_scale": 1.5,
        "top_k": 50,
        "sampler_cfg_scale": 1.5,
        "steps": 30,
        "sampler_name": "euler",
        "scheduler": "simple",
        "denoise": 1.0,
    }
    assert OFFICIAL_COMFY_MINIMAX_PROFILE.text_cfg_scale == 1.7
    assert OFFICIAL_COMFY_MINIMAX_PROFILE.sampler_cfg_scale == 1.7
    assert minimax_profile_from_mapping(None) is CURRENT_MINIMAX_PROFILE
    assert minimax_profile_from_mapping({}) is CURRENT_MINIMAX_PROFILE


def test_worker_graph_uses_current_profile_and_zeroed_negative_conditioning() -> None:
    graph = worker._graph(_request(), filename_prefix="audio/test")

    assert graph["4"]["inputs"]["seed"] == 200717
    assert graph["4"]["inputs"]["cfg_scale"] == 1.5
    assert graph["4"]["inputs"]["top_k"] == 50
    assert graph["7"]["inputs"]["steps"] == 30
    assert graph["7"]["inputs"]["cfg"] == 1.5
    assert graph["7"]["inputs"]["sampler_name"] == "euler"
    assert graph["7"]["inputs"]["scheduler"] == "simple"
    assert graph["7"]["inputs"]["denoise"] == 1.0
    assert graph["5"] == {
        "class_type": "ConditioningZeroOut",
        "inputs": {"conditioning": ["4", 0]},
    }
    assert graph["7"]["inputs"]["negative"] == ["5", 0]


def test_worker_graph_accepts_official_comfy_profile_without_making_it_default() -> None:
    graph = worker._graph(
        _request(profile=OFFICIAL_COMFY_MINIMAX_PROFILE),
        filename_prefix="audio/official",
    )

    assert graph["4"]["inputs"]["cfg_scale"] == 1.7
    assert graph["7"]["inputs"]["cfg"] == 1.7
    assert CURRENT_MINIMAX_PROFILE.text_cfg_scale == 1.5


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        ("lyrics", "", "MINIMAX_MUSIC3_LYRICS_REQUIRED"),
        ("caption", "", "MINIMAX_MUSIC3_CAPTION_REQUIRED"),
        ("max_duration", 91, "MINIMAX_MUSIC3_DURATION_INVALID"),
        ("max_duration", True, "MINIMAX_MUSIC3_DURATION_INVALID"),
    ],
)
def test_worker_has_no_fallback_for_missing_or_invalid_inputs(
    field: str,
    value: object,
    error_code: str,
) -> None:
    request = _request()
    request[field] = value
    with pytest.raises(RuntimeError, match=error_code):
        worker._graph(request, filename_prefix="audio/invalid")


@pytest.mark.parametrize(
    ("suffix", "error_code"),
    [
        ("\nNo drums.", "MINIMAX_MUSIC3_CAPTION_NEGATIVE_SYNTAX"),
        ("\nSoft strings enter.", "MINIMAX_MUSIC3_CAPTION_DISALLOWED_TERM"),
        ("\nR&B groove.", "MINIMAX_MUSIC3_CAPTION_DISALLOWED_TERM"),
    ],
)
def test_worker_rejects_caption_drift_before_starting_comfy(
    suffix: str,
    error_code: str,
) -> None:
    request = _request()
    request["caption"] = str(request["caption"]) + suffix
    with pytest.raises(RuntimeError, match=error_code):
        worker._graph(request, filename_prefix="audio/drift")


def test_worker_rejects_legacy_short_lyrics_instead_of_filling_them() -> None:
    request = _request()
    request["lyrics"] = "[Verse]\n只有一句"
    with pytest.raises(RuntimeError, match="MINIMAX_MUSIC3_LYRICS_INVALID"):
        worker._graph(request, filename_prefix="audio/legacy")


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        ("seed", True, "MINIMAX_PROFILE_SEED_INVALID"),
        ("text_cfg_scale", 0.0, "MINIMAX_PROFILE_TEXT_CFG_SCALE_INVALID"),
        ("top_k", 0, "MINIMAX_PROFILE_TOP_K_INVALID"),
        ("sampler_cfg_scale", 5.1, "MINIMAX_PROFILE_SAMPLER_CFG_SCALE_INVALID"),
        ("steps", 0, "MINIMAX_PROFILE_STEPS_INVALID"),
        ("sampler_name", "dpmpp_2m", "MINIMAX_PROFILE_SAMPLER_INVALID"),
        ("scheduler", "karras", "MINIMAX_PROFILE_SCHEDULER_INVALID"),
        ("denoise", 0.0, "MINIMAX_PROFILE_DENOISE_INVALID"),
    ],
)
def test_profile_validation_rejects_unregistered_sampling_space(
    field: str,
    value: object,
    error_code: str,
) -> None:
    profile = CURRENT_MINIMAX_PROFILE.to_dict()
    profile[field] = value
    with pytest.raises(MiniMaxProfileError, match=error_code):
        minimax_profile_from_mapping(profile)


def test_worker_source_contains_no_music_fallback() -> None:
    source = Path(worker.__file__).read_text(encoding="utf-8").casefold()
    assert "fallback_caption" not in source
    assert "fallback_lyrics" not in source
    assert "soft strings gradually enter" not in source
    assert "subtle cello" not in source
    assert "sparse percussion" not in source
