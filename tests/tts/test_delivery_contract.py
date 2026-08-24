from __future__ import annotations

import sys
import wave
from array import array
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


def test_delivery_request_uses_audio_only_cross_lingual_conditioning() -> None:
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

    assert request["voice_condition_mode"] == "cross_lingual_audio_only"
    assert "reference_text" not in request
    assert "instruct_text" not in request
    assert request["blocks"] == ["第一句。第二句。"]
    assert request["seed"] == 200717


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
