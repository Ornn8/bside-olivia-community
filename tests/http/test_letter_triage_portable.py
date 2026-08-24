from __future__ import annotations

import asyncio
import json

from letter_triage import (
    LetterReplyRouter,
    RoutingContext,
    _current_music_performance,
    routing_context_from_environment,
)


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text


class _Gateway:
    def __init__(self, response: str) -> None:
        self.response = response
        self.messages = None

    async def complete(self, messages, *, request_id=None):
        self.messages = messages
        return _Response(self.response)


def test_router_timeout_uses_portable_environment_configuration():
    router = LetterReplyRouter(
        _Gateway("{}"),
        environ={"OLIVIA_REPLY_ROUTER_TIMEOUT_SECONDS": "90"},
    )

    assert router.timeout_seconds == 90.0


def test_music_performance_uses_system_tod_with_day_morning_fallback(tmp_path):
    day = tmp_path / "TOD1200.mp4"
    dusk = tmp_path / "TOD1730.mp4"
    night = tmp_path / "TOD2000.mp4"
    for path in (day, dusk, night):
        path.write_bytes(b"scene")
    environ = {
        "OLIVIA_MUSIC_SCENE_DAY": str(day),
        "OLIVIA_MUSIC_SCENE_DUSK": str(dusk),
        "OLIVIA_MUSIC_SCENE_NIGHT": str(night),
    }

    assert _current_music_performance(environ, hour=5) == day
    assert _current_music_performance(environ, hour=8) == day
    assert _current_music_performance(environ, hour=9) == day
    assert _current_music_performance(environ, hour=15) == day
    assert _current_music_performance(environ, hour=16) == dusk
    assert _current_music_performance(environ, hour=18) == dusk
    assert _current_music_performance(environ, hour=19) == night
    assert _current_music_performance(environ, hour=4) == night


def _route(*, context=None, **overrides):
    payload = {
        "mode": "text_letter",
        "reason_code": "direct_words_are_enough",
        "emotion_level": "normal",
        "music_contexts": [],
        "music_role": "none",
        "music_intent": "none",
        "request_disposition": "none",
        "direct_response_sufficient": True,
        "voice_materially_better": False,
        "music_materially_better": False,
        "character_willing": True,
    }
    payload.update(overrides)
    gateway = _Gateway(json.dumps(payload))
    result = asyncio.run(
        LetterReplyRouter(
            gateway,
            routing_context=context or RoutingContext(True, True),
        ).classify("synthetic current letter")
    )
    return result, gateway


def test_high_emotion_can_choose_direct_spoken_video_without_music():
    result, _ = _route(
        mode="spoken_video",
        reason_code="voice_adds_presence",
        emotion_level="high",
        direct_response_sufficient=True,
        voice_materially_better=True,
    )
    assert result.reply_mode == "spoken_video"
    assert result.music_contexts == ()


def test_music_discussion_remains_text_when_words_are_enough():
    result, _ = _route(
        reason_code="music_topic_still_needs_words",
        music_contexts=["music_discussion"],
        music_role="discussion",
        music_intent="discuss",
        request_disposition="discuss",
    )
    assert result.reply_mode == "text_letter"
    assert result.status == "completed"


def test_explicit_request_can_be_refused_even_if_music_would_add_value():
    result, _ = _route(
        reason_code="not_willing_to_perform_now",
        music_contexts=["explicit_performance_or_adaptation_request"],
        music_role="discussion",
        music_intent="discuss",
        request_disposition="refuse",
        music_materially_better=True,
        character_willing=False,
    )
    assert result.reply_mode == "text_letter"
    assert result.request_disposition == "refuse"


def test_text_reply_cannot_claim_it_is_actively_performing():
    result, _ = _route(
        reason_code="text_cannot_perform",
        music_contexts=["explicit_performance_or_adaptation_request"],
        music_role="performance",
        music_intent="perform",
        request_disposition="defer",
        music_materially_better=True,
        character_willing=False,
    )
    assert result.reply_mode == "text_letter"
    assert result.status == "unavailable"


def test_explicit_request_alone_cannot_trigger_musical_video():
    result, _ = _route(
        mode="musical_video",
        reason_code="request_only_is_not_enough",
        music_contexts=["explicit_performance_or_adaptation_request"],
        music_role="performance",
        music_intent="perform",
        request_disposition="fulfill",
        direct_response_sufficient=False,
        music_materially_better=False,
    )
    assert result.reply_mode == "text_letter"
    assert result.status == "unavailable"


def test_media_unavailable_blocks_otherwise_valid_musical_choice():
    result, _ = _route(
        context=RoutingContext(True, False),
        mode="musical_video",
        reason_code="performance_would_carry_reply",
        music_contexts=["explicit_performance_or_adaptation_request"],
        music_role="performance",
        music_intent="perform",
        request_disposition="fulfill",
        direct_response_sufficient=False,
        music_materially_better=True,
    )
    assert result.reply_mode == "text_letter"
    assert result.reason_code == "router_invalid_result"


def test_all_musical_gates_allow_character_choice():
    result, _ = _route(
        mode="musical_video",
        reason_code="performance_carries_this_reply",
        music_contexts=["explicit_performance_or_adaptation_request"],
        music_role="performance",
        music_intent="perform",
        request_disposition="fulfill",
        direct_response_sufficient=False,
        music_materially_better=True,
    )
    assert result.reply_mode == "musical_video"
    assert result.status == "completed"


def test_current_work_relevance_requires_trusted_current_work():
    result, _ = _route(
        context=RoutingContext(True, True, ()),
        mode="musical_video",
        reason_code="current_piece_matches_event",
        music_contexts=["current_work_relevance"],
        music_role="adaptation",
        music_intent="adapt",
        request_disposition="none",
        direct_response_sufficient=False,
        music_materially_better=True,
    )
    assert result.reply_mode == "text_letter"
    assert result.status == "unavailable"


def test_current_work_relevance_accepts_bounded_trusted_work():
    result, _ = _route(
        context=RoutingContext(True, True, ("正在整理《花》的新段落",)),
        mode="musical_video",
        reason_code="current_piece_matches_event",
        music_contexts=["current_work_relevance"],
        music_role="adaptation",
        music_intent="adapt",
        request_disposition="none",
        direct_response_sufficient=False,
        music_materially_better=True,
    )
    assert result.reply_mode == "musical_video"


def test_melody_idea_requires_spontaneous_motif_and_compose():
    invalid, _ = _route(
        mode="musical_video",
        reason_code="claimed_melody_without_motif",
        music_contexts=["melody_idea"],
        music_role="performance",
        music_intent="perform",
        request_disposition="none",
        direct_response_sufficient=False,
        music_materially_better=True,
    )
    assert invalid.reply_mode == "text_letter"

    valid, _ = _route(
        mode="musical_video",
        reason_code="specific_motif_carries_reply",
        music_contexts=["melody_idea"],
        music_role="spontaneous_motif",
        music_intent="compose",
        request_disposition="none",
        direct_response_sufficient=False,
        music_materially_better=True,
    )
    assert valid.reply_mode == "musical_video"


def test_router_receives_trusted_context_separately_from_letter():
    context = RoutingContext(True, True, ("练习中的合成作品",))
    result, gateway = _route(context=context)
    assert result.reply_mode == "text_letter"
    payload = json.loads(gateway.messages[1]["content"])
    assert payload["current_letter"] == "synthetic current letter"
    assert payload["routing_context"]["current_music_work"] == [
        "练习中的合成作品"
    ]
    assert payload["routing_context"]["musical_video_available"] is True


def test_environment_overrides_do_not_claim_video_availability_and_current_work_is_bounded():
    context = routing_context_from_environment(
        {
            "OLIVIA_SPOKEN_VIDEO_AVAILABLE": "1",
            "OLIVIA_MUSICAL_VIDEO_AVAILABLE": "1",
            "OLIVIA_CURRENT_MUSIC_WORK": '["作品甲", "作品乙", "作品甲"]',
        }
    )
    assert context.spoken_video_available is False
    assert context.musical_video_available is False
    assert context.current_music_work == ("作品甲", "作品乙")


def test_spoken_reason_is_unavailable_without_complete_video_pipeline():
    context = routing_context_from_environment(
        {
            "OLIVIA_SPOKEN_VIDEO_AVAILABLE": "1",
            "OLIVIA_MUSICAL_VIDEO_AVAILABLE": "0",
        }
    )

    assert context.spoken_video_available is False
    assert context.musical_video_available is False


def test_complete_video_readiness_fails_closed_for_every_missing_renderer_dependency(
    tmp_path,
):
    def write(relative: str) -> str:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")
        return str(path)

    data_root = tmp_path / "data"
    data_root.mkdir()
    scene = write("scenes/spoken.mp4")
    performance = write("scenes/performance.mp4")
    minimax_root = tmp_path / "minimax"
    latentsync_root = tmp_path / "latentsync"
    env = {
        "OLIVIA_LOCAL_DATA_ROOT": str(data_root),
        "OLIVIA_TTS_CONFIG": write("config/tts.json"),
        "OLIVIA_VISUAL_CONFIG": write("config/visual.json"),
        "OLIVIA_LIVETALKING_WORKER": write("workers/visual.py"),
        "OLIVIA_OFFICIAL_REPLY_REFERENCE": write("official/reply.mp4"),
        "OLIVIA_ROFORMER_EXE": write("roformer/roformer.exe"),
        "OLIVIA_ROFORMER_MODEL_PATH": write("roformer/model.ckpt"),
        "OLIVIA_ROFORMER_CONFIG_PATH": write("roformer/config.yaml"),
        "OLIVIA_MINIMAX_COMFY_PYTHON": write("minimax/python.exe"),
        "OLIVIA_MINIMAX_COMFY_ROOT": str(minimax_root),
        "OLIVIA_MINIMAX_WORKER": write("workers/minimax.py"),
        "OLIVIA_LATENTSYNC_PYTHON": write("latentsync/python.exe"),
        "OLIVIA_LATENTSYNC_ROOT": str(latentsync_root),
        "OLIVIA_SPOKEN_VIDEO_AVAILABLE": "1",
        "OLIVIA_MUSICAL_VIDEO_AVAILABLE": "1",
        **{
            f"OLIVIA_SCENE_{tod}": scene
            for tod in ("MORNING", "DAY", "DUSK", "NIGHT")
        },
        **{
            f"OLIVIA_MUSIC_SCENE_{tod}": performance
            for tod in ("DAY", "DUSK", "NIGHT")
        },
    }
    required = [
        tmp_path / "config/tts.json",
        tmp_path / "config/visual.json",
        tmp_path / "workers/visual.py",
        tmp_path / "official/reply.mp4",
        tmp_path / "roformer/roformer.exe",
        tmp_path / "roformer/model.ckpt",
        tmp_path / "roformer/config.yaml",
        tmp_path / "minimax/python.exe",
        tmp_path / "workers/minimax.py",
        minimax_root / "main.py",
        minimax_root / "comfy_extras/nodes_minimax_music.py",
        minimax_root / "models/unet/minimax_music3_dit_int8_convrot.safetensors",
        minimax_root / "models/clip/minimax_music3_text_encoder_pruned_int8_convrot.safetensors",
        minimax_root / "models/vae/minimax_music3_dav.safetensors",
        tmp_path / "latentsync/python.exe",
        latentsync_root / "scripts/inference.py",
        latentsync_root / "configs/unet/stage2_efficient.yaml",
        latentsync_root / "checkpoints/latentsync_unet.pt",
    ]
    for path in required:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")

    assert routing_context_from_environment(env) == RoutingContext(True, True)

    for missing in required:
        missing.unlink()
        assert routing_context_from_environment(env) == RoutingContext(False, False)
        missing.write_bytes(b"synthetic")


def test_invalid_router_output_fails_closed_to_text_letter():
    result = asyncio.run(
        LetterReplyRouter(
            _Gateway("not-json"),
            routing_context=RoutingContext(True, True),
        ).classify("普通聊天")
    )
    assert result.reply_mode == "text_letter"
    assert result.status == "unavailable"
