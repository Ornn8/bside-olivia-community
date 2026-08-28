from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import music_reply
from runtime.media import latentsync_reply
import tts.delivery as tts_delivery
from letter_triage import (
    LetterReplyRouter,
    RoutingContext,
    _current_music_performance,
    routing_context_from_environment,
)
from llm_gateway import GatewayConfig, GatewayToolCall, create_gateway


class _Gateway:
    def __init__(self, arguments: dict[str, object]) -> None:
        self.arguments = arguments
        self.messages = None
        self.requests: list[dict[str, object]] = []

    async def complete_with_tools(
        self,
        *,
        messages,
        tools,
        tool_choice,
        request_id=None,
    ):
        self.messages = messages
        self.requests.append(
            {
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
                "request_id": request_id,
            }
        )
        return [GatewayToolCall("select_reply_mode", self.arguments)]


def test_router_timeout_uses_portable_environment_configuration():
    router = LetterReplyRouter(
        _Gateway({}),
        environ={"OLIVIA_REPLY_ROUTER_TIMEOUT_SECONDS": "90"},
    )

    assert router.timeout_seconds == 90.0


def test_music_performance_uses_one_fixed_accepted_base(tmp_path):
    performance = tmp_path / "accepted-performance.mp4"
    performance.write_bytes(b"scene")
    environ = {"OLIVIA_MUSIC_PERFORMANCE_BASE": str(performance)}

    assert _current_music_performance(environ) == performance


def _route_arguments(**overrides) -> dict[str, object]:
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
    return payload


def _route(*, context=None, **overrides):
    payload = _route_arguments(**overrides)
    gateway = _Gateway(payload)
    result = asyncio.run(
        LetterReplyRouter(
            gateway,
            routing_context=context or RoutingContext(True, True),
        ).classify("synthetic current letter")
    )
    return result, gateway


def test_spoken_only_video_mode_fails_closed_to_text():
    result, _ = _route(
        mode="spoken_video",
        reason_code="voice_adds_presence",
        emotion_level="high",
        direct_response_sufficient=True,
        voice_materially_better=True,
    )
    assert result.reply_mode == "text_letter"
    assert result.status == "unavailable"
    assert result.music_contexts == ()


def test_router_offers_only_text_or_spoken_plus_music_video() -> None:
    _, gateway = _route()

    mode_schema = gateway.requests[0]["tools"][0]["function"]["parameters"]["properties"]["mode"]
    assert mode_schema["enum"] == ["musical_video", "text_letter"]


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


def test_router_accepts_one_offline_structured_musical_tool_call():
    gateway = _Gateway(
        {
            "mode": "musical_video",
            "reason_code": "performance_carries_this_reply",
            "emotion_level": "mixed",
            "music_contexts": ["explicit_performance_or_adaptation_request"],
            "music_role": "performance",
            "music_intent": "perform",
            "request_disposition": "fulfill",
            "direct_response_sufficient": False,
            "voice_materially_better": False,
            "music_materially_better": True,
            "character_willing": True,
        }
    )

    result = asyncio.run(
        LetterReplyRouter(
            gateway,
            routing_context=RoutingContext(True, True),
        ).classify("请把这段心事唱给我听。")
    )

    assert result.reply_mode == "musical_video"
    assert result.status == "completed"
    assert gateway.requests[0]["tool_choice"] == "required"
    assert gateway.requests[0]["request_id"] == "letter-reply-mode-router"
    assert gateway.requests[0]["tools"][0]["function"]["name"] == "select_reply_mode"


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("reason_code", 123),
        ("mode", "TEXT_LETTER"),
        ("mode", " text_letter "),
        ("music_contexts", [{}]),
        ("music_contexts", [[]]),
    ],
)
def test_router_rejects_noncanonical_tool_schema_arguments(
    field: str,
    invalid_value: object,
):
    result, _ = _route(**{field: invalid_value})

    assert result.reply_mode == "text_letter"
    assert result.status == "unavailable"
    assert result.reason_code == "router_invalid_result"


@pytest.mark.parametrize(
    ("expected_mode", "overrides"),
    [
        ("text_letter", {}),
        (
            "musical_video",
            {
                "mode": "musical_video",
                "reason_code": "performance_carries_this_reply",
                "emotion_level": "mixed",
                "music_contexts": ["explicit_performance_or_adaptation_request"],
                "music_role": "performance",
                "music_intent": "perform",
                "request_disposition": "fulfill",
                "direct_response_sufficient": False,
                "music_materially_better": True,
            },
        ),
    ],
)
def test_public_mock_gateway_routes_configured_tool_result(
    expected_mode: str,
    overrides: dict[str, object],
):
    arguments = _route_arguments(**overrides)
    gateway = create_gateway(
        GatewayConfig(
            provider="mock",
            provider_options={
                "tool_call": {
                    "name": "select_reply_mode",
                    "arguments": arguments,
                }
            },
        )
    )

    result = asyncio.run(
        LetterReplyRouter(
            gateway,
            routing_context=RoutingContext(True, True),
        ).classify("synthetic current letter")
    )

    assert result.reply_mode == expected_mode
    assert result.status == "completed"
    assert gateway.network_call_count == 0


def test_deepseek_flash_thinking_omits_tool_choice_and_routes_valid_structured_call():
    async def exercise():
        seen: dict[str, object] = {}
        arguments = _route_arguments(
            mode="musical_video",
            reason_code="performance_carries_this_reply",
            emotion_level="mixed",
            music_contexts=["explicit_performance_or_adaptation_request"],
            music_role="performance",
            music_intent="perform",
            request_disposition="fulfill",
            direct_response_sufficient=False,
            music_materially_better=True,
        )

        async def handler(request: web.Request) -> web.Response:
            seen["body"] = await request.json()
            return web.json_response(
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "function": {
                                            "name": "select_reply_mode",
                                            "arguments": json.dumps(arguments),
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        async with TestClient(TestServer(app)) as client:
            gateway = create_gateway(
                GatewayConfig(
                    provider="openai_compatible",
                    base_url=str(client.make_url("/v1")),
                    model="deepseek-v4-flash",
                )
            )
            result = await LetterReplyRouter(
                gateway,
                routing_context=RoutingContext(True, True),
            ).classify("synthetic current letter")
        return result, seen["body"]

    result, body = asyncio.run(exercise())

    assert result.reply_mode == "musical_video"
    assert result.status == "completed"
    assert body["model"] == "deepseek-v4-flash"
    assert "thinking" not in body
    assert "tool_choice" not in body
    assert body["tools"][0]["function"]["name"] == "select_reply_mode"


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
    monkeypatch,
):
    monkeypatch.setattr(
        music_reply, "_python_runtime_ready", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        music_reply, "_executable_runtime_ready", lambda *_args, **_kwargs: True
    )

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
        "OLIVIA_ORDINARY_ACTION_BASE": scene,
        "OLIVIA_MUSIC_PERFORMANCE_BASE": performance,
        "OLIVIA_SPOKEN_SCENE_CANDIDATES": str(tmp_path / "stale-candidate.mp4"),
    }
    ffmpeg = tmp_path / "runtime" / "ffmpeg.exe"
    ffmpeg.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.write_bytes(b"synthetic")
    env["OLIVIA_FFMPEG_EXE"] = str(ffmpeg)
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
        minimax_root / "models/diffusion_models/minimax_music3_dit_int8_convrot.safetensors",
        minimax_root / "models/text_encoders/minimax_music3_text_encoder_pruned_int8_convrot.safetensors",
        minimax_root / "models/vae/minimax_music3_dav.safetensors",
        tmp_path / "latentsync/python.exe",
        latentsync_root / "scripts/inference.py",
        latentsync_root / "configs/unet/stage2_efficient.yaml",
        latentsync_root / "checkpoints/latentsync_unet.pt",
    ]
    for path in required:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")

    tts_runtime = tmp_path / "tts-runtime"
    (tts_runtime / "venv/Scripts").mkdir(parents=True)
    (tts_runtime / "venv/Scripts/python.exe").write_bytes(b"synthetic")
    tts_model = tmp_path / "tts-model"
    tts_model.mkdir()
    tts_llm = tts_model / "llm.pt"
    tts_llm.write_bytes(b"synthetic")
    quality_cache = tmp_path / "whisper"
    quality_cache.mkdir()
    quality_checkpoint = quality_cache / "base.pt"
    quality_checkpoint.write_bytes(b"synthetic")
    required.append(tts_llm)
    tts_reference = write("tts/reference.wav")
    Path(env["OLIVIA_TTS_CONFIG"]).write_text(json.dumps({"settings": {
        "runtime_root": str(tts_runtime), "model_dir": str(tts_model),
        "reference_audio": tts_reference,
        "provider_options": {"quality_gate_cache_root": str(quality_cache)},
    }}), encoding="utf-8")
    visual_runtime = tmp_path / "visual-runtime"
    visual_runtime.mkdir()
    visual_checkpoint = write("visual/checkpoint.pt")
    visual_avatar = visual_runtime / "data/avatars/b11_olivia"
    visual_avatar.mkdir(parents=True)
    visual_work = tmp_path / "visual-work"
    visual_work.mkdir()
    Path(env["OLIVIA_VISUAL_CONFIG"]).write_text(json.dumps({"settings": {
        "runtime_root": str(visual_runtime), "checkpoint_path": visual_checkpoint,
        "checkpoint_sha256": "0" * 64, "avatar_payload": str(visual_avatar),
        "original_reference": env["OLIVIA_OFFICIAL_REPLY_REFERENCE"], "work_root": str(visual_work),
        "avatar_id": "b11_olivia", "checkpoint_url": "https://example.test/checkpoint",
        "checkpoint_revision": "v1", "checkpoint_license": "Apache-2.0",
        "upstream_source": "https://github.com/lipku/LiveTalking",
        "upstream_revision": "a97f01ba366e55eeed94e88d6bae38ed77b3a1b9",
        "upstream_license": "Apache-2.0",
    }}), encoding="utf-8")

    monkeypatch.setattr(
        tts_delivery,
        "_verified_file",
        lambda path, _expected: Path(path).is_file(),
    )
    monkeypatch.setattr(
        tts_delivery,
        "_quality_runtime_available",
        lambda *_args: True,
    )

    assert routing_context_from_environment(env) == RoutingContext(True, True)

    env["OLIVIA_PROJECT_ROOT"] = str(tmp_path)
    for name, value in tuple(env.items()):
        if not name.startswith("OLIVIA_") or name in {
            "OLIVIA_FFMPEG_EXE",
            "OLIVIA_PROJECT_ROOT",
        }:
            continue
        try:
            env[name] = Path(value).relative_to(tmp_path).as_posix()
        except (TypeError, ValueError):
            pass
    assert routing_context_from_environment(env) == RoutingContext(True, True)

    quality_checkpoint.unlink()
    assert routing_context_from_environment(env) == RoutingContext(True, True)
    quality_checkpoint.write_bytes(b"synthetic")

    for missing in required:
        missing.unlink()
        assert routing_context_from_environment(env) == RoutingContext(False, False)
        missing.write_bytes(b"synthetic")

    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
    for resolved_ffmpeg in (None, write("runtime/ffmpeg.exe")):
        monkeypatch.setattr(
            latentsync_reply.shutil,
            "which",
            lambda _name: resolved_ffmpeg,
        )
        assert routing_context_from_environment(env) == RoutingContext(False, False)

    acceptance_document = Path("docs/P03_06_END_TO_END_ACCEPTANCE.md").read_text(
        encoding="utf-8"
    )
    assert "optional transition" not in acceptance_document
    assert "mandatory official silent turn/black transition" in acceptance_document


def test_invalid_router_output_fails_closed_to_text_letter():
    result = asyncio.run(
        LetterReplyRouter(
            _Gateway({}),
            routing_context=RoutingContext(True, True),
        ).classify("普通聊天")
    )
    assert result.reply_mode == "text_letter"
    assert result.status == "unavailable"
    assert result.reason_code == "router_invalid_result"
