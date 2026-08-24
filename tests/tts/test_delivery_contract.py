from __future__ import annotations

import json
import sys
import wave
from array import array
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import tts.delivery as delivery

from reply_delivery import (
    build_ordinary_video_llm_content,
    build_ordinary_video_repair_content,
    ordinary_video_reply_length_ok,
    plan_reply_delivery,
)
from tts import external_cosyvoice_worker
from tts.delivery import (
    DeliveryAudioError,
    _fit_overlong_wav,
    build_external_delivery_request,
    delivery_tempo_factor,
)
from voice_direction import VoicePerformancePlan


def test_ordinary_video_copy_contract_targets_cross_lingual_delivery_length() -> None:
    initial = build_ordinary_video_llm_content("原始来信")
    repair = build_ordinary_video_repair_content("旧候选")

    assert "180到200个汉字" in initial
    assert "目标为190字" in initial
    assert "180到200个汉字" in repair
    assert ordinary_video_reply_length_ok("林" * 179) is False
    assert ordinary_video_reply_length_ok("林" * 180) is True
    assert ordinary_video_reply_length_ok("林" * 200) is True
    assert ordinary_video_reply_length_ok("林" * 201) is False


def test_delivery_request_uses_one_contextual_long_form_payload() -> None:
    config = SimpleNamespace(
        runtime_root="runtime",
        model_dir="model",
        reference_audio="reference.wav",
        reference_text="accepted reference transcript",
        fp16=True,
    )

    request = build_external_delivery_request(
        config,
        plan_reply_delivery("第一句。第二句。"),
    )

    assert request["voice_condition_mode"] == "contextual_long_form"
    assert "reference_text" not in request
    assert "instruct_text" not in request
    assert request["blocks"] == ["第一句。第二句。"]
    assert request["seed"] == 200717
    assert request["performance_control_mode"] == "single_pass_global"


def test_llm_voice_plan_builds_one_non_spoken_instruct2_request() -> None:
    reply_text = "I hear you. I will stay with you through this."
    plan = VoicePerformancePlan(
        reply_text=reply_text,
        overall_emotion="restrained empathy becoming steady reassurance",
        global_speed=1.06,
        energy=0.62,
        breath_before_sentences=(),
        emphasize_sentences=(1,),
    )
    config = SimpleNamespace(
        runtime_root="runtime",
        model_dir="model",
        reference_audio="reference.wav",
        fp16=True,
    )

    request = build_external_delivery_request(config, plan)

    assert request["voice_condition_mode"] == "instruct2_single_pass"
    assert request["text"] == plan.render_text
    assert request["instruct_text"].count("<|endofprompt|>") == 1
    assert request["performance_control_mode"] == "single_pass_llm_instruct"
    assert request["duration_target_seconds"] == [40.0, 50.0]
    assert request["max_attempts"] == 3
    assert "blocks" not in request
    assert "reference_text" not in request


def test_delivery_tempo_allows_only_modest_whole_utterance_fit() -> None:
    assert delivery_tempo_factor(50.0) is None
    assert delivery_tempo_factor(51.0) == 1.02
    assert delivery_tempo_factor(52.0) == 1.04
    assert delivery_tempo_factor(52.01) is None


def test_delivery_fit_rejects_audio_over_52_seconds(tmp_path) -> None:
    sample_rate = 8_000
    path = tmp_path / "overlong.wav"
    samples = array("h", [3_000] * (53 * sample_rate))
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(samples.tobytes())

    with pytest.raises(DeliveryAudioError, match="TTS_DELIVERY_DURATION_OUT_OF_RANGE"):
        _fit_overlong_wav(path, 53.0)


def test_delivery_fit_uses_canonical_explicit_ffmpeg_override(tmp_path, monkeypatch) -> None:
    source = tmp_path / "overlong.wav"
    source.write_bytes(b"synthetic")
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"synthetic")
    monkeypatch.setenv("OLIVIA_FFMPEG_EXE", str(executable))
    observed = []

    def fake_run(command, **_kwargs):
        observed.append(command[0])
        fitted = tmp_path / "speech-fitted.wav"
        with wave.open(str(fitted), "wb") as target:
            target.setnchannels(1); target.setsampwidth(2); target.setframerate(8000)
            target.writeframes(array("h", [0] * 8000 * 50).tobytes())
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(delivery.subprocess, "run", fake_run)
    _fit_overlong_wav(source, 51.0)
    assert observed == [str(executable.resolve())]


def test_external_worker_renders_blocks_with_one_cross_lingual_model_load(
    tmp_path, monkeypatch
) -> None:
    calls = {"loads": 0, "texts": []}

    class Tensor:
        def __init__(self, values):
            self.values = values

        def detach(self):
            return self

        def cpu(self):
            return self

        def float(self):
            return self

        def reshape(self, *_shape):
            return self

        def tolist(self):
            return self.values

    class AutoModel:
        sample_rate = 100

        def __init__(self, **_kwargs):
            calls["loads"] += 1

        def inference_cross_lingual(self, text, *_args, **kwargs):
            calls["texts"].append(text)
            calls.setdefault("kwargs", []).append(kwargs)
            value = 0.1 if len(calls["texts"]) == 1 else 0.2
            yield {"tts_speech": Tensor([value] * 100)}

    cosyvoice = ModuleType("cosyvoice")
    cli = ModuleType("cosyvoice.cli")
    module = ModuleType("cosyvoice.cli.cosyvoice")
    module.AutoModel = AutoModel
    torch = ModuleType("torch")
    torch.manual_seed = lambda value: calls.setdefault("seeds", []).append(value)
    monkeypatch.setitem(sys.modules, "cosyvoice", cosyvoice)
    monkeypatch.setitem(sys.modules, "cosyvoice.cli", cli)
    monkeypatch.setitem(sys.modules, "cosyvoice.cli.cosyvoice", module)
    monkeypatch.setitem(sys.modules, "torch", torch)
    output = tmp_path / "delivery.wav"

    external_cosyvoice_worker._synthesize(
        {
            "runtime_root": str(tmp_path),
            "model_dir": str(tmp_path),
            "reference_audio": str(tmp_path / "reference.wav"),
            "blocks": ["第一句", "第二句"],
            "cross_fade_seconds": 0.1,
            "seed": 200717,
        },
        output,
    )

    assert calls == {
        "loads": 1,
        "texts": [
            "You are a helpful assistant.<|endofprompt|>第一句",
            "You are a helpful assistant.<|endofprompt|>第二句",
        ],
        "kwargs": [
            {"zero_shot_spk_id": "", "stream": False, "speed": 1.0},
            {"zero_shot_spk_id": "", "stream": False, "speed": 1.0},
        ],
        "seeds": [200717],
    }
    with wave.open(str(output), "rb") as rendered:
        assert rendered.getnframes() == 190


def test_external_worker_runs_one_whole_reply_instruct2_inference(
    tmp_path,
    monkeypatch,
) -> None:
    calls = {"loads": 0, "inference": []}

    class Tensor:
        def detach(self):
            return self

        def cpu(self):
            return self

        def float(self):
            return self

        def reshape(self, *_shape):
            return self

        def tolist(self):
            return [0.05] * 4200

    class AutoModel:
        sample_rate = 100

        def __init__(self, **_kwargs):
            calls["loads"] += 1

        def inference_instruct2(self, text, instruction, reference_audio, **kwargs):
            calls["inference"].append((text, instruction, reference_audio, kwargs))
            yield {"tts_speech": Tensor()}

    cosyvoice = ModuleType("cosyvoice")
    cli = ModuleType("cosyvoice.cli")
    module = ModuleType("cosyvoice.cli.cosyvoice")
    module.AutoModel = AutoModel
    torch = ModuleType("torch")
    torch.manual_seed = lambda _value: None
    torch.cuda = SimpleNamespace(is_available=lambda: False)
    numpy = ModuleType("numpy")
    numpy.random = SimpleNamespace(seed=lambda _value: None)
    monkeypatch.setitem(sys.modules, "cosyvoice", cosyvoice)
    monkeypatch.setitem(sys.modules, "cosyvoice.cli", cli)
    monkeypatch.setitem(sys.modules, "cosyvoice.cli.cosyvoice", module)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "numpy", numpy)
    output = tmp_path / "directed.wav"

    external_cosyvoice_worker._synthesize(
        {
            "runtime_root": str(tmp_path),
            "model_dir": str(tmp_path),
            "reference_audio": str(tmp_path / "reference.wav"),
            "voice_condition_mode": "instruct2_single_pass",
            "text": "one complete frozen reply",
            "instruct_text": "steady reassurance<|endofprompt|>",
            "speed": 1.06,
            "duration_target_seconds": [40.0, 50.0],
            "max_attempts": 3,
            "seed": 200717,
        },
        output,
    )

    assert calls["loads"] == 1
    assert calls["inference"] == [
        (
            "one complete frozen reply",
            "steady reassurance<|endofprompt|>",
            str(tmp_path / "reference.wav"),
            {
                "zero_shot_spk_id": "",
                "stream": False,
                "speed": 1.06,
                "text_frontend": False,
            },
        )
    ]
    with wave.open(str(output), "rb") as rendered:
        assert rendered.getnframes() == 4200


def test_external_worker_rejects_runtime_without_pinned_instruct2_api(
    tmp_path,
    monkeypatch,
) -> None:
    class AutoModel:
        sample_rate = 100

        def __init__(self, **_kwargs):
            pass

    cosyvoice = ModuleType("cosyvoice")
    cli = ModuleType("cosyvoice.cli")
    module = ModuleType("cosyvoice.cli.cosyvoice")
    module.AutoModel = AutoModel
    monkeypatch.setitem(sys.modules, "cosyvoice", cosyvoice)
    monkeypatch.setitem(sys.modules, "cosyvoice.cli", cli)
    monkeypatch.setitem(sys.modules, "cosyvoice.cli.cosyvoice", module)

    with pytest.raises(RuntimeError, match="COSYVOICE_INSTRUCT2_UNSUPPORTED"):
        external_cosyvoice_worker._synthesize(
            {
                "runtime_root": str(tmp_path),
                "model_dir": str(tmp_path),
                "reference_audio": str(tmp_path / "reference.wav"),
                "voice_condition_mode": "instruct2_single_pass",
                "text": "one complete frozen reply",
                "instruct_text": "steady reassurance<|endofprompt|>",
            },
            tmp_path / "directed.wav",
        )


def test_directed_tts_provenance_and_human_acceptance_are_publicly_recorded() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads(
        (root / "runtime/packaging/manifests/b10b.modules.json").read_text(
            encoding="utf-8"
        )
    )
    upstreams = {
        item["id"]: item for item in manifest["provenance"]["upstreams"]
    }
    runtime = upstreams["b06-cosyvoice-runtime"]
    model = upstreams["b06-cosyvoice-model"]
    acceptance = (root / "docs/B06_LOCAL_TTS_ACCEPTANCE.md").read_text(
        encoding="utf-8"
    )
    normalized_acceptance = " ".join(acceptance.split())

    assert runtime["revision"] == "074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc"
    assert runtime["license"] == "Apache-2.0"
    assert runtime["adapter_boundary"] in normalized_acceptance
    assert runtime["uninstall_path"] in normalized_acceptance
    assert model["revision"] == "29e01c4e8d000f4bcd70751be16fa94bf3d85a18"
    assert "inference_instruct2" in acceptance
    assert "41.28 seconds" in acceptance
    assert "24 kHz mono" in acceptance
    assert "863ef5185c448f189c46524fd8e87010bf353bc2bf8e3df9f59bdc0948ec14ce" in acceptance
    assert "Candidate 1" in acceptance
