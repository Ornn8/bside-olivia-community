from __future__ import annotations

import asyncio
import json
import sys
import wave
from array import array
from pathlib import Path
from types import SimpleNamespace

import pytest

import tts.delivery as delivery
from tts import TTSConfig, TTSProfileManager, TTSRequest, TTSService, default_registry
from tts.contracts import TTSValidationError
from tts import external_breeze_worker
from tts.breeze import BreezeTTS2Provider
from voice_direction import VoicePerformancePlan


_BREEZE_LICENSE = "BreezeBlue-Research-and-Non-Commercial-1.0"


def _breeze_config(tmp_path: Path) -> TTSConfig:
    runtime = tmp_path / "ComfyUI-Breeze-TTS-2"
    runtime.mkdir()
    for name in ("__init__.py", "loader.py", "nodes.py", "int8.py", "LICENSE"):
        (runtime / name).write_text("# pinned external runtime\n", encoding="utf-8")

    model_root = tmp_path / "models"
    model = model_root / "drbaph_Breeze-TTS-2-comfyui"
    (model / "audio_tokenizer").mkdir(parents=True)
    for name in (
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "generation_config.json",
        "Breeze-TTS-2-int8-hybrid.safetensors",
        "audio_tokenizer/config.json",
        "audio_tokenizer/model.safetensors",
    ):
        (model / name).write_bytes(b"pinned external model")

    model_license = tmp_path / "BREEZE_MODEL_LICENSE"
    model_license.write_text(
        "BREEZEBLUE RESEARCH AND NON-COMMERCIAL LICENSE AGREEMENT\nVersion 1.0\n",
        encoding="utf-8",
    )
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"private local reference")
    return TTSConfig(
        profile="breeze-tts2-int8-hybrid",
        provider="breeze_tts2",
        runtime_root=str(runtime),
        model_dir=str(model_root),
        reference_audio=str(reference),
        reference_text="精确参考转写",
        license_id=_BREEZE_LICENSE,
        fallback="text",
        provider_options={
            "external_python": sys.executable,
            "model_variant": "int8_hybrid",
            "model_license_path": str(model_license),
        },
    )


def _plan() -> VoicePerformancePlan:
    return VoicePerformancePlan(
        reply_text="林" * 190,
        overall_emotion="声音柔软自然地承接，再缓缓托起给到力量",
        global_speed=1.0,
        energy=0.55,
        breath_before_sentences=(),
        emphasize_sentences=(),
        short_instruction="声音柔软自然地承接，再缓缓托起给到力量",
    )


def test_breeze_provider_is_selectable_and_missing_assets_fall_back_before_generation(
    tmp_path: Path,
) -> None:
    config = _breeze_config(tmp_path)
    registry = default_registry()

    provider = registry.create(config)
    try:
        health = provider.health()
    finally:
        provider.close()

    assert registry.names() == ("breeze_tts2", "cosyvoice3")
    assert health == {
        "status": "available",
        "provider": "breeze_tts2",
        "license_id": _BREEZE_LICENSE,
        "model": "Breeze-TTS-2-int8-hybrid",
        "model_variant": "int8_hybrid",
        "streaming": False,
        "delivery_mode": "single_pass_voice_performance_plan",
        "reference_audio_local_only": True,
        "offline_only": True,
        "execution": "external-process",
    }

    missing = TTSConfig.from_mapping(
        {
            **config.__dict__,
            "model_dir": str(tmp_path / "missing-model"),
        }
    )
    service = TTSService(missing)
    try:
        result = asyncio.run(service.synthesize(TTSRequest("保留这段回复。")))
    finally:
        service.close()

    assert result.status == "text_fallback"
    assert result.error_code == "TTS_ASSET_MISSING"
    assert result.fallback_text == "保留这段回复。"

    service = TTSService(config)
    try:
        result = asyncio.run(service.synthesize(TTSRequest("必须走完整语音编排。")))
    finally:
        service.close()
    assert result.status == "text_fallback"
    assert result.error_code == "TTS_PERFORMANCE_PLAN_REQUIRED"

    model_license = Path(str(config.provider_options["model_license_path"]))
    model_license.write_text("Apache License 2.0\n", encoding="utf-8")
    invalid_license = BreezeTTS2Provider(config).health()
    assert invalid_license["status"] == "unavailable"
    assert invalid_license["reason_code"] == "TTS_LICENSE_UNVERIFIED"


def test_breeze_performance_request_consumes_the_complete_llm_voice_plan(
    tmp_path: Path,
) -> None:
    reply = "第一句保持温柔。第二句慢慢托起力量。"
    plan = VoicePerformancePlan(
        reply_text=reply,
        overall_emotion="声音柔软自然地承接，再缓缓托起给到力量",
        global_speed=1.06,
        energy=0.72,
        breath_before_sentences=(2,),
        emphasize_sentences=(1,),
        short_instruction="声音柔软自然地承接，再缓缓托起给到力量",
    )

    config = _breeze_config(tmp_path)
    request = BreezeTTS2Provider(config).performance_request(plan)

    assert request["text"] == reply
    assert request["text"] == plan.spoken_text
    assert request["instruction"] == (
        "声音柔软自然地承接，再缓缓托起给到力量，"
        "整体语速略快但保持自然对话感，能量饱满但不喊叫，"
        "在第2句前自然换气，轻轻强调第1句"
    )
    assert request["voice_plan"] == {
        "emotion": plan.overall_emotion,
        "speed": 1.06,
        "energy": 0.72,
        "breath_before_sentences": [2],
        "emphasize_sentences": [1],
    }
    assert request["cfg_scale"] == 4.0
    assert request["model_variant"] == "int8_hybrid"
    assert request["max_new_tokens"] == 650
    assert request["quality_gate_required"] is True
    assert request["quality_forbidden_text"] == request["instruction"]

    limited = TTSConfig.from_mapping(
        {
            **config.__dict__,
            "provider_options": {
                **config.provider_options,
                "max_new_tokens": -10,
            },
        }
    )
    assert BreezeTTS2Provider(limited).performance_request(plan)["max_new_tokens"] == 64


def test_breeze_worker_marks_ready_before_generation_and_writes_pcm(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: dict[str, object] = {}

    class Waveform:
        def detach(self):
            return self

        def float(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            import numpy

            return numpy.asarray([[[0.25, -0.5, 0.75]]], dtype="float32")

    class Loader:
        HYBRID_LABEL = "int8 hybrid (recommended)"

        @staticmethod
        def load_breeze_bundle(*args):
            calls["load"] = args
            return SimpleNamespace(codec="codec")

    class Runtime:
        @staticmethod
        def comfy_audio_to_tensor(audio):
            calls["reference"] = audio
            return "reference-waveform", 24000

        @staticmethod
        def encode_reference_audio(codec, waveform, sample_rate):
            calls["encoded"] = (codec, waveform, sample_rate)
            return "reference-codes"

    class Nodes:
        @staticmethod
        def _generate_audio(bundle, **kwargs):
            calls["status_before_generation"] = json.loads(
                status.read_text(encoding="utf-8")
            )
            calls["generation"] = kwargs
            return {"waveform": Waveform(), "sample_rate": 24000}

    monkeypatch.setattr(
        external_breeze_worker,
        "_load_package",
        lambda _runtime: (Loader, Nodes, Runtime),
    )
    monkeypatch.setattr(
        external_breeze_worker,
        "_read_reference_audio",
        lambda _path: {"waveform": "private-reference", "sample_rate": 24000},
    )
    request = {
        "runtime_root": str(tmp_path / "runtime"),
        "model_dir": str(tmp_path / "model"),
        "reference_audio": str(tmp_path / "reference.wav"),
        "reference_text": "精确参考转写",
        "text": "完整冻结回复。",
        "instruction": "声音柔软自然地承接，保持自然语速，保持中等能量",
        "model_variant": "int8_hybrid",
        "dtype": "bf16",
        "device": "cuda",
        "attention": "eager",
        "decode_mode": "eager",
        "cfg_scale": 4.0,
        "seed": 200717,
        "max_new_tokens": 1500,
        "temperature": 0.9,
        "top_k": 50,
        "top_p": 1.0,
        "repetition_penalty": 1.1,
        "depth_temperature": 0.9,
        "depth_top_k": 50,
        "depth_top_p": 1.0,
        "gain_db": 0.0,
    }
    output = tmp_path / "speech.wav"
    status = tmp_path / "status.json"

    external_breeze_worker._synthesize(request, output, status)

    assert calls["load"] == (
        "int8 hybrid (recommended)",
        "bf16",
        "cuda",
        "eager",
        False,
        "eager",
    )
    assert calls["status_before_generation"] == {
        "status": "ready",
        "phase": "generation",
        "audio_started": False,
    }
    assert calls["generation"]["text"] == request["text"]
    assert calls["generation"]["instruction"] == request["instruction"]
    assert calls["generation"]["ref_text"] == request["reference_text"]
    assert calls["generation"]["ref_codes"] == "reference-codes"
    assert json.loads(status.read_text(encoding="utf-8")) == {
        "status": "completed",
        "phase": "completed",
        "audio_started": True,
    }
    with wave.open(str(output), "rb") as rendered:
        assert rendered.getnchannels() == 1
        assert rendered.getframerate() == 24000
        assert array("h", rendered.readframes(3)).tolist() == [8192, -16384, 24575]


def test_breeze_delivery_renders_one_complete_plan_and_reports_the_real_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    observed: list[tuple[str, dict[str, object]]] = []

    def fake_run(command, **_kwargs):
        command = [str(item) for item in command]
        request_path = Path(command[command.index("--request") + 1])
        output_path = Path(command[command.index("--output") + 1])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        worker = Path(command[1]).name
        observed.append((worker, request))
        if worker == "external_breeze_worker.py":
            with wave.open(str(output_path), "wb") as target:
                target.setnchannels(1)
                target.setsampwidth(2)
                target.setframerate(24000)
                target.writeframes(b"\x00\x00" * (43 * 24000))
        else:
            output_path.write_text(
                json.dumps(
                    delivery.assess_transcript(
                        str(request["expected_text"]),
                        str(request["expected_text"]),
                        str(request["forbidden_text"]),
                    ),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(delivery, "delivery_configured", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(delivery.subprocess, "run", fake_run)
    output = tmp_path / "reply.wav"

    result = delivery.render_delivery_wav(_breeze_config(tmp_path), _plan(), output)

    assert result.provider == "breeze_tts2"
    assert result.duration_seconds == 43.0
    assert result.quality_report is not None
    assert result.quality_report["passed"] is True
    assert output.is_file()
    assert [name for name, _request in observed] == [
        "external_breeze_worker.py",
        "external_audio_quality_worker.py",
    ]
    synthesis_request = observed[0][1]
    assert synthesis_request["text"] == _plan().spoken_text
    assert synthesis_request["voice_plan"]["emotion"] == _plan().overall_emotion
    assert synthesis_request["max_attempts"] == 1


@pytest.mark.parametrize("phase,audio_started", [("preflight", False), ("generation", True)])
def test_breeze_failure_never_runs_a_second_tts_model(
    tmp_path: Path,
    monkeypatch,
    phase: str,
    audio_started: bool,
) -> None:
    calls: list[str] = []

    def fake_run(command, **_kwargs):
        command = [str(item) for item in command]
        worker = Path(command[1]).name
        calls.append(worker)
        status = Path(command[command.index("--status") + 1])
        status.write_text(
            json.dumps(
                {"status": "failed", "phase": phase, "audio_started": audio_started}
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=2)

    monkeypatch.setattr(delivery, "delivery_configured", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(delivery.subprocess, "run", fake_run)

    with pytest.raises(delivery.DeliveryAudioError, match="TTS_EXTERNAL_PROCESS_FAILED"):
        delivery.render_delivery_wav(
            _breeze_config(tmp_path),
            _plan(),
            tmp_path / "reply.wav",
            enforce_content_gate=False,
        )

    assert calls == ["external_breeze_worker.py"]


def test_breeze_profile_persists_selection_without_exposing_paths(
    tmp_path: Path,
) -> None:
    config = _breeze_config(tmp_path)
    manager = TTSProfileManager(tmp_path / "state")

    installed = manager.install(config)
    loaded = manager.config(config.profile)
    public = loaded.public_dict()

    assert installed["status"] == "INSTALLED"
    assert public["provider"] == "breeze_tts2"
    assert str(tmp_path) not in json.dumps(public, ensure_ascii=False)

    missing = TTSConfig.from_mapping(
        {**config.__dict__, "model_dir": str(tmp_path / "missing")}
    )
    with pytest.raises(TTSValidationError) as exc_info:
        manager.install(missing)
    assert exc_info.value.code == "TTS_EXTERNAL_ASSET_MISSING"
