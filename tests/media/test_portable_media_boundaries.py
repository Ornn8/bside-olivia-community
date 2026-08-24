from __future__ import annotations

from array import array
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from types import ModuleType
import wave

import pytest

import latentsync_reply
import music_reply
from latentsync_reply import render_latentsync_video
from music_duration import MUSIC_DURATION_OPTIONS, normalize_music_duration
from music_reply import MusicReplyError, render_musical_reply
from reply_delivery import (
    build_ordinary_video_llm_content,
    build_ordinary_video_repair_content,
    ordinary_video_reply_length_ok,
    plan_reply_delivery,
)
from reply_media import ReplyMediaError, _tts_config, render_reply_video
from tts.delivery import (
    DeliveryAudioError,
    _fit_overlong_wav,
    build_external_delivery_request,
    delivery_tempo_factor,
)
from tts import external_cosyvoice_worker


def test_delivery_plan_preserves_spoken_text_and_duration_options():
    plan = plan_reply_delivery("我听见了。慢慢来，好吗？")
    assert plan.spoken_text == "我听见了。慢慢来，好吗？"
    assert plan.duration_target_seconds == (40.0, 50.0)
    assert MUSIC_DURATION_OPTIONS == (40, 60)
    assert normalize_music_duration(40) == 40


def test_ordinary_video_copy_contract_matches_cross_lingual_calibration():
    initial = build_ordinary_video_llm_content("原始来信")
    repair = build_ordinary_video_repair_content("旧候选")

    assert "180到200个汉字" in initial
    assert "目标为190字" in initial
    assert "180到200个汉字" in repair
    assert ordinary_video_reply_length_ok("林" * 179) is False
    assert ordinary_video_reply_length_ok("林" * 180) is True
    assert ordinary_video_reply_length_ok("林" * 200) is True
    assert ordinary_video_reply_length_ok("林" * 201) is False
    assert normalize_music_duration(60) == 60


def test_media_workers_fail_closed_without_external_provider(tmp_path):
    with pytest.raises(ReplyMediaError):
        render_reply_video("测试回信", tmp_path / "reply.mp4", tts_config_path=tmp_path / "missing.json", visual_config_path=tmp_path / "missing.json", worker_path=tmp_path / "missing.py")
    with pytest.raises(MusicReplyError):
        render_musical_reply(
            "测试来信",
            "测试回信",
            tmp_path / "music.mp4",
            normal_video_path=tmp_path / "normal.mp4",
            official_reply_reference_path=tmp_path / "missing-official.mp4",
            song_video_path=tmp_path / "song.mp4",
            tts_config_path=tmp_path / "missing.json",
            visual_config_path=tmp_path / "missing.json",
            worker_path=tmp_path / "missing.py",
            performance_video_path=tmp_path / "performance.mp4",
            duration_seconds=40,
        )


def test_roformer_uses_explicit_f_drive_assets_and_utf8(tmp_path, monkeypatch):
    executable = tmp_path / "roformer.exe"
    model = tmp_path / "models" / "MelBandRoformer.ckpt"
    config = tmp_path / "models" / "config.yaml"
    song = tmp_path / "song.flac"
    vocals = tmp_path / "vocals.wav"
    for path in (executable, model, config, song):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")
    monkeypatch.setenv("OLIVIA_ROFORMER_EXE", str(executable))
    monkeypatch.setenv("OLIVIA_ROFORMER_MODEL_PATH", str(model))
    monkeypatch.setenv("OLIVIA_ROFORMER_CONFIG_PATH", str(config))
    monkeypatch.setattr(music_reply, "_ffmpeg", lambda: "ffmpeg")
    observed = []

    def fake_run(command, error_code, *, timeout=900.0, env=None):
        observed.append((command, error_code, env))
        if "--store_dir" in command:
            output_root = Path(command[command.index("--store_dir") + 1])
            (output_root / "synthetic_vocals.wav").write_bytes(b"vocals")

    monkeypatch.setattr(music_reply, "_run", fake_run)

    music_reply.separate_vocals(song, vocals)

    assert observed[0][0][:4] == ["ffmpeg", "-y", "-i", str(song)]
    assert observed[0][1] == "ROFORMER_INPUT_CONVERSION_FAILED"
    assert observed[1][0][-4:] == [
        "--model_path",
        str(model),
        "--config_path",
        str(config),
    ]
    assert observed[1][2]["PYTHONUTF8"] == "1"
    assert observed[1][2]["PYTHONIOENCODING"] == "utf-8"
    assert vocals.read_bytes() == b"vocals"


def test_official_voice_reference_is_bounded_for_cosyvoice(tmp_path, monkeypatch):
    reference = tmp_path / "official-reference.wav"
    with wave.open(str(reference), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(b"\0\0" * (16000 * 41))
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "settings": {
                    "runtime_root": str(tmp_path / "runtime"),
                    "model_dir": str(tmp_path / "model"),
                    "reference_audio": str(reference),
                    "reference_text": "synthetic reference",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OLIVIA_REPLY_VOICE_REFERENCE", str(reference))

    configured = _tts_config(config_path, tmp_path / "work", ordinary_video=True)

    with wave.open(configured.reference_audio, "rb") as bounded:
        assert bounded.getnframes() / bounded.getframerate() == pytest.approx(4.85)
    assert configured.leading_trim_seconds == 0
    with wave.open(str(reference), "rb") as original:
        assert original.getnframes() / original.getframerate() == 41


def test_explicit_missing_voice_reference_fails_closed(tmp_path, monkeypatch):
    generic_reference = tmp_path / "generic-reference.wav"
    with wave.open(str(generic_reference), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(b"\0\0" * 16000)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "settings": {
                    "runtime_root": str(tmp_path / "runtime"),
                    "model_dir": str(tmp_path / "model"),
                    "reference_audio": str(generic_reference),
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "OLIVIA_REPLY_VOICE_REFERENCE",
        str(tmp_path / "missing-official-reference.wav"),
    )

    with pytest.raises(ReplyMediaError, match="VOICE_REFERENCE_UNAVAILABLE"):
        _tts_config(config_path, tmp_path / "work", ordinary_video=True)


def test_unconfigured_voice_reference_keeps_controlled_default(tmp_path, monkeypatch):
    generic_reference = tmp_path / "generic-reference.wav"
    with wave.open(str(generic_reference), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(b"\0\0" * 16000)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "settings": {
                    "runtime_root": str(tmp_path / "runtime"),
                    "model_dir": str(tmp_path / "model"),
                    "reference_audio": str(generic_reference),
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("OLIVIA_REPLY_VOICE_REFERENCE", raising=False)

    configured = _tts_config(config_path, tmp_path / "work", ordinary_video=True)

    assert Path(configured.reference_audio) == generic_reference


def test_delivery_request_excludes_reference_text_and_instruct_controls():
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

    assert "reference_text" not in request
    assert "instruct_text" not in request
    assert request["blocks"] == ["第一句。第二句。"]
    assert request["seed"] == 200717


def test_delivery_tempo_allows_only_modest_whole_utterance_fit():
    assert delivery_tempo_factor(50.0) is None
    assert delivery_tempo_factor(51.0) == 1.02
    assert delivery_tempo_factor(52.0) == 1.04
    assert delivery_tempo_factor(52.01) is None


def test_delivery_fit_rejects_audio_over_52_seconds(tmp_path):
    sample_rate = 8_000
    path = tmp_path / "overlong.wav"
    samples = array(
        "h",
        (
            3_000 if index < 53 * sample_rate else 0
            for index in range(61 * sample_rate)
        ),
    )
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(samples.tobytes())

    with pytest.raises(DeliveryAudioError, match="TTS_DELIVERY_DURATION_OUT_OF_RANGE"):
        _fit_overlong_wav(path, 61.0)


def test_external_worker_renders_delivery_blocks_with_one_model_load(
    tmp_path, monkeypatch
):
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
            "stream": False,
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


def test_latentsync_process_receives_bundled_ffmpeg(tmp_path, monkeypatch):
    root = tmp_path / "latentsync"
    python = tmp_path / "python.exe"
    source = tmp_path / "source.mp4"
    audio = tmp_path / "speech.wav"
    output = tmp_path / "reply.mp4"
    ffmpeg = tmp_path / "ffmpeg" / "ffmpeg-win-x86_64.exe"
    required = (
        python,
        source,
        audio,
        root / "scripts" / "inference.py",
        root / "configs" / "unet" / "stage2_efficient.yaml",
        root / "checkpoints" / "latentsync_unet.pt",
        ffmpeg,
    )
    for path in required:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")
    monkeypatch.setitem(
        sys.modules,
        "imageio_ffmpeg",
        SimpleNamespace(get_ffmpeg_exe=lambda: str(ffmpeg)),
    )
    monkeypatch.delenv("OLIVIA_FFMPEG_EXE", raising=False)
    monkeypatch.setenv("PATH", "synthetic-original-path")
    monkeypatch.setenv("OLIVIA_PROVIDER_CACHE_ROOT", str(tmp_path / "provider-cache"))
    observed = {}

    def run(command, **kwargs):
        if Path(command[0]).name.casefold() == "ffmpeg.exe":
            Path(command[-1]).write_bytes(b"prepared-video")
            observed["prepare_command"] = command
            return SimpleNamespace(returncode=0)
        observed["environment"] = kwargs.get("env")
        observed["config_path"] = command[
            command.index("--unet_config_path") + 1
        ]
        observed["ffmpeg"] = shutil.which(
            "ffmpeg", path=observed["environment"]["PATH"]
        )
        Path(command[command.index("--video_out_path") + 1]).write_bytes(
            b"synthetic-video"
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(latentsync_reply.subprocess, "run", run)

    render_latentsync_video(
        source,
        audio,
        output,
        python_path=python,
        latentsync_root=root,
    )

    environment = observed["environment"]
    assert environment is not None
    assert observed["ffmpeg"] is not None
    assert Path(environment["HF_HOME"]).is_relative_to(tmp_path.parent)
    assert Path(environment["TORCH_HOME"]).is_relative_to(tmp_path.parent)
    assert Path(environment["TEMP"]).is_relative_to(tmp_path)
    assert output.read_bytes() == b"synthetic-video"
    assert Path(observed["prepare_command"][0]).name.casefold() == "ffmpeg.exe"
    assert "libx264" in observed["prepare_command"]
    assert Path(observed["config_path"]).name == "stage2_efficient.yaml"


def test_latentsync_rejects_missing_explicit_ffmpeg_without_fallback(
    tmp_path, monkeypatch
):
    root = tmp_path / "latentsync"
    python = tmp_path / "python.exe"
    source = tmp_path / "source.mp4"
    audio = tmp_path / "speech.wav"
    for path in (
        python,
        source,
        audio,
        root / "scripts" / "inference.py",
        root / "configs" / "unet" / "stage2_efficient.yaml",
        root / "checkpoints" / "latentsync_unet.pt",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")
    monkeypatch.setenv("OLIVIA_FFMPEG_EXE", str(tmp_path / "missing-ffmpeg.exe"))

    def unexpected_discovery(*_args, **_kwargs):
        raise AssertionError("configured FFmpeg failure must not fall back")

    monkeypatch.setattr(latentsync_reply.shutil, "which", unexpected_discovery)
    monkeypatch.setitem(
        sys.modules,
        "imageio_ffmpeg",
        SimpleNamespace(get_ffmpeg_exe=unexpected_discovery),
    )

    def unexpected_subprocess(*_args, **_kwargs):
        raise AssertionError("configured FFmpeg failure must not fall back")

    monkeypatch.setattr(latentsync_reply.subprocess, "run", unexpected_subprocess)

    with pytest.raises(
        latentsync_reply.LatentSyncReplyError,
        match="^LATENTSYNC_FFMPEG_UNAVAILABLE$",
    ):
        render_latentsync_video(
            source,
            audio,
            tmp_path / "reply.mp4",
            python_path=python,
            latentsync_root=root,
        )
