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
import reply_media
from latentsync_reply import render_latentsync_video
from music_duration import MUSIC_DURATION_OPTIONS, normalize_music_duration
from music_reply import MusicReplyError, render_musical_reply
from reply_delivery import (
    build_ordinary_video_llm_content,
    build_ordinary_video_repair_content,
    ordinary_video_reply_length_ok,
    plan_reply_delivery,
)
from reply_media import (
    ReplyMediaError,
    _tts_config,
    assemble_complete_video_delivery,
    render_reply_video,
)
from song_content import SongContentPlan
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


def test_reply_video_rejects_relative_latentsync_paths_without_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
    monkeypatch.delenv("OLIVIA_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("OLIVIA_LATENTSYNC_PYTHON", "providers/latentsync/python.exe")
    monkeypatch.setenv("OLIVIA_LATENTSYNC_ROOT", "providers/latentsync")

    with pytest.raises(ReplyMediaError, match="^LATENTSYNC_INPUT_UNAVAILABLE$"):
        render_reply_video(
            "synthetic reply",
            tmp_path / "reply.mp4",
            tts_config_path=tmp_path / "missing-tts.json",
            visual_config_path=tmp_path / "missing-visual.json",
            worker_path=tmp_path / "missing-worker.py",
            scene_path=tmp_path / "scene.mp4",
        )


def test_ordinary_quality_preflight_preserves_retryable_gate_error(
    tmp_path,
    monkeypatch,
) -> None:
    worker = tmp_path / "worker.py"
    worker.write_text("# synthetic", encoding="utf-8")
    calls: list[bool] = []

    monkeypatch.setattr(reply_media, "_tts_config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        reply_media,
        "_visual_config",
        lambda *_args, **_kwargs: SimpleNamespace(validate=lambda: None),
    )
    monkeypatch.setattr(reply_media, "media_runtime_available", lambda *_args: True)

    def configured(_config, *, require_quality_gate=False):
        calls.append(require_quality_gate)
        return not require_quality_gate

    monkeypatch.setattr(reply_media, "delivery_configured", configured)

    with pytest.raises(ReplyMediaError, match="TTS_CONTENT_GATE_UNAVAILABLE"):
        reply_media.assemble_complete_video_delivery(
            tmp_path / "tts.json",
            tmp_path / "visual.json",
            worker,
            tmp_path,
            require_quality_gate=True,
        )

    assert calls == [False, True]


def test_shared_music_preflight_never_checks_ordinary_quality_gate(
    tmp_path,
    monkeypatch,
) -> None:
    worker = tmp_path / "worker.py"
    worker.write_text("# synthetic", encoding="utf-8")
    calls: list[bool] = []

    monkeypatch.setattr(reply_media, "_tts_config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        reply_media,
        "_visual_config",
        lambda *_args, **_kwargs: SimpleNamespace(validate=lambda: None),
    )
    monkeypatch.setattr(reply_media, "media_runtime_available", lambda *_args: True)

    def configured(_config, *, require_quality_gate=False):
        calls.append(require_quality_gate)
        return True

    monkeypatch.setattr(reply_media, "delivery_configured", configured)

    reply_media.assemble_complete_video_delivery(
        tmp_path / "tts.json",
        tmp_path / "visual.json",
        worker,
        tmp_path,
        require_quality_gate=False,
    )

    assert calls == [False]


def test_complete_delivery_resolves_tts_internal_paths_from_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    config_path = project_root / "config" / "tts.json"
    reference = project_root / "voice" / "reference.wav"
    worker = project_root / "workers" / "visual.py"
    for path in (worker,):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")
    reference.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(reference), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16_000)
        target.writeframes(b"\0\0" * 16_000)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "settings": {
                    "runtime_root": "providers/cosyvoice",
                    "model_dir": "providers/cosyvoice-model",
                    "reference_audio": "voice/default.wav",
                    "provider_options": {
                        "numba_cache_dir": "cache/numba",
                        "quality_gate_cache_root": "cache/quality-default",
                        "wetext_fst_root": "cache/wetext",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    environment = {
        "OLIVIA_PROJECT_ROOT": str(project_root),
        "OLIVIA_REPLY_VOICE_REFERENCE": "voice/reference.wav",
        "OLIVIA_TTS_QUALITY_GATE_CACHE_ROOT": "cache/quality-override",
    }
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
    monkeypatch.setattr(
        reply_media,
        "_visual_config",
        lambda _path: SimpleNamespace(validate=lambda: None),
    )
    monkeypatch.setattr(reply_media, "media_runtime_available", lambda _env: True)
    monkeypatch.setattr(reply_media, "delivery_configured", lambda _tts: True)

    delivery = assemble_complete_video_delivery(
        config_path,
        tmp_path / "unused-visual.json",
        worker,
        tmp_path / "temporary",
        environment,
    )

    assert Path(delivery.tts.runtime_root) == project_root / "providers" / "cosyvoice"
    assert Path(delivery.tts.model_dir) == project_root / "providers" / "cosyvoice-model"
    assert Path(delivery.tts.reference_audio) == reference
    assert delivery.tts.provider_options["numba_cache_dir"] == str(
        project_root / "cache" / "numba"
    )
    assert delivery.tts.provider_options["quality_gate_cache_root"] == str(
        project_root / "cache" / "quality-override"
    )
    assert delivery.tts.provider_options["wetext_fst_root"] == str(
        project_root / "cache" / "wetext"
    )


def test_complete_delivery_rejects_relative_tts_paths_without_absolute_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "tts.json"
    config_path.write_text(
        json.dumps(
            {
                "settings": {
                    "runtime_root": "providers/cosyvoice",
                    "model_dir": "providers/cosyvoice-model",
                    "reference_audio": "voice/default.wav",
                }
            }
        ),
        encoding="utf-8",
    )
    worker = tmp_path / "worker.py"
    worker.write_bytes(b"synthetic")
    monkeypatch.setattr(
        reply_media,
        "_visual_config",
        lambda _path: SimpleNamespace(validate=lambda: None),
    )
    monkeypatch.setattr(reply_media, "media_runtime_available", lambda _env: True)
    monkeypatch.setattr(reply_media, "delivery_configured", lambda _tts: True)

    with pytest.raises(ReplyMediaError, match="COMPLETE_VIDEO_CONFIG_UNAVAILABLE"):
        assemble_complete_video_delivery(
            config_path,
            tmp_path / "unused-visual.json",
            worker,
            tmp_path / "temporary",
            {},
        )


def test_musical_render_uses_one_immutable_provider_path_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"

    def write(path: Path, payload: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    transition = write(project_root / "scenes" / "transition.mp4", b"transition")
    performance = write(project_root / "scenes" / "performance.mp4", b"performance")
    environment = {
        "OLIVIA_PROJECT_ROOT": str(project_root),
        "OLIVIA_OFFICIAL_REPLY_REFERENCE": "scenes/transition.mp4",
        "OLIVIA_MINIMAX_COMFY_PYTHON": "providers/minimax/python.exe",
        "OLIVIA_MINIMAX_COMFY_ROOT": "providers/minimax/root",
        "OLIVIA_MINIMAX_WORKER": "providers/minimax/worker.py",
        "OLIVIA_ROFORMER_EXE": "providers/roformer/roformer.exe",
        "OLIVIA_ROFORMER_MODEL_PATH": "providers/roformer/model.ckpt",
        "OLIVIA_ROFORMER_CONFIG_PATH": "providers/roformer/config.yaml",
        "OLIVIA_LATENTSYNC_PYTHON": "providers/latentsync/python.exe",
        "OLIVIA_LATENTSYNC_ROOT": "providers/latentsync/root",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    observed: dict[str, object] = {}
    output = tmp_path / "output.mp4"

    monkeypatch.setattr(
        music_reply,
        "plan_song_content",
        lambda *_args: SongContentPlan(
            emotion="gentle_reassurance",
            lyrics="[Verse]\\nsynthetic",
            caption="synthetic piano ballad",
            duration_seconds=40,
        ),
    )
    monkeypatch.setattr(
        music_reply,
        "prepare_official_spoken_base",
        lambda _reference, destination: write(Path(destination), b"spoken-base"),
    )

    def fake_spoken(_text, destination, **kwargs):
        observed["tts_environment"] = kwargs["environment"]
        write(Path(destination), b"spoken")
        for name in environment:
            monkeypatch.delenv(name, raising=False)
        return {"spoken_stage": "completed"}

    monkeypatch.setattr(music_reply, "render_reply_video", fake_spoken)

    def fake_generate(self, _content, _reply_text, destination, **_kwargs):
        observed["minimax"] = (
            self.python_path,
            self.worker_path,
            self.comfy_root,
        )
        write(Path(destination), b"song")
        return {"music_stage": "completed"}

    monkeypatch.setattr(music_reply.MiniMaxMusic3Worker, "generate", fake_generate)

    def fake_separate(_song, destination, **kwargs):
        observed["roformer"] = (
            kwargs["executable"], kwargs["model_path"], kwargs["config_path"]
        )
        observed["roformer_environment"] = kwargs["environment"]
        write(Path(destination), b"vocals")

    monkeypatch.setattr(music_reply, "separate_vocals", fake_separate)

    def fake_face(_base, _vocals, _song, destination, **kwargs):
        observed["face"] = (
            kwargs["latentsync_python_path"],
            kwargs["latentsync_root"],
        )
        write(Path(destination), b"face")
        return {"performance_stage": "completed"}

    monkeypatch.setattr(music_reply, "render_full_face_performance", fake_face)
    monkeypatch.setattr(
        music_reply,
        "concat_videos",
        lambda _normal, _song, destination, **_kwargs: write(Path(destination), b"final"),
    )

    music_reply.render_musical_reply(
        "letter",
        "reply",
        output,
        normal_video_path=tmp_path / "normal.mp4",
        official_reply_reference_path=transition,
        song_video_path=tmp_path / "song.mp4",
        tts_config_path=project_root / "config" / "tts.json",
        visual_config_path=project_root / "config" / "visual.json",
        worker_path=project_root / "workers" / "visual.py",
        performance_video_path=performance,
        duration_seconds=40,
    )

    assert observed["minimax"] == (
        project_root / "providers/minimax/python.exe",
        project_root / "providers/minimax/worker.py",
        project_root / "providers/minimax/root",
    )
    assert observed["roformer"] == (
        project_root / "providers/roformer/roformer.exe",
        project_root / "providers/roformer/model.ckpt",
        project_root / "providers/roformer/config.yaml",
    )
    assert observed["face"] == (
        project_root / "providers/latentsync/python.exe",
        project_root / "providers/latentsync/root",
    )
    assert observed["tts_environment"]["OLIVIA_PROJECT_ROOT"] == str(project_root)
    assert observed["roformer_environment"]["OLIVIA_PROJECT_ROOT"] == str(project_root)
    assert output.read_bytes() == b"final"


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

    music_reply.separate_vocals(
        song,
        vocals,
        executable=executable,
        model_path=model,
        config_path=config,
        environment={},
    )

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

def test_all_video_paths_share_explicit_ffmpeg_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"synthetic")
    monkeypatch.setenv("OLIVIA_FFMPEG_EXE", str(executable))

    assert latentsync_reply.resolve_ffmpeg_executable() == executable.resolve()
    assert music_reply._ffmpeg() == str(executable.resolve())
    assert reply_media._ffmpeg() == str(executable.resolve())


def test_speaking_scene_candidates_are_stable_and_legacy_compatible(tmp_path: Path) -> None:
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    env = {"OLIVIA_SPOKEN_SCENE_CANDIDATES": os.pathsep.join((str(first), str(second), str(first)))}
    candidates = music_reply.speaking_scene_candidates(env)
    assert candidates == (first, second)
    assert music_reply.select_speaking_scene(candidates) == first
    assert music_reply.speaking_scene_candidates({"OLIVIA_OFFICIAL_REPLY_REFERENCE": str(second)}) == (second,)
    assert music_reply.speaking_scene_candidates(
        {
            "OLIVIA_PROJECT_ROOT": str(tmp_path),
            "OLIVIA_SPOKEN_SCENE_CANDIDATES": os.pathsep.join(
                ("first.mp4", "second.mp4")
            ),
        }
    ) == (first, second)


def test_musical_renderer_rejects_relative_latentsync_paths_without_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    transition = tmp_path / "transition.mp4"
    transition.write_bytes(b"synthetic")
    monkeypatch.chdir(unrelated_cwd)
    monkeypatch.delenv("OLIVIA_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("OLIVIA_SPOKEN_SCENE_CANDIDATES", str(transition))
    monkeypatch.setenv("OLIVIA_MINIMAX_COMFY_PYTHON", str(tmp_path / "minimax.exe"))
    monkeypatch.setenv("OLIVIA_MINIMAX_COMFY_ROOT", str(tmp_path / "minimax"))
    monkeypatch.setenv("OLIVIA_MINIMAX_WORKER", str(tmp_path / "minimax-worker.py"))
    monkeypatch.setenv("OLIVIA_LATENTSYNC_PYTHON", "providers/latentsync/python.exe")
    monkeypatch.setenv("OLIVIA_LATENTSYNC_ROOT", "providers/latentsync")
    monkeypatch.setattr(
        music_reply,
        "plan_song_content",
        lambda *_args: SimpleNamespace(lyrics="lyrics", caption="caption", emotion="steady"),
    )
    monkeypatch.setattr(
        music_reply,
        "prepare_official_spoken_base",
        lambda _reference, destination: Path(destination).write_bytes(b"spoken-base"),
    )
    monkeypatch.setattr(
        music_reply,
        "render_reply_video",
        lambda *_args, **_kwargs: pytest.fail("ordinary renderer must not be called"),
    )

    with pytest.raises(MusicReplyError, match="^LATENTSYNC_INPUT_UNAVAILABLE$"):
        render_musical_reply(
            "letter",
            "reply",
            tmp_path / "out" / "reply.mp4",
            normal_video_path=tmp_path / "out" / "normal.mp4",
            official_reply_reference_path=transition,
            song_video_path=tmp_path / "out" / "song.mp4",
            tts_config_path=tmp_path / "tts.json",
            visual_config_path=tmp_path / "visual.json",
            worker_path=tmp_path / "worker.py",
            performance_video_path=tmp_path / "performance.mp4",
            duration_seconds=40,
        )


def test_musical_renderer_resolves_all_provider_paths_from_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "app"
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"synthetic")
    performance = project_root / "assets" / "performance.mp4"
    reference = project_root / "assets" / "reference.mp4"
    for path in (performance, reference):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")

    provider_paths = {
        "OLIVIA_MINIMAX_COMFY_PYTHON": project_root / "providers" / "minimax" / "python.exe",
        "OLIVIA_MINIMAX_COMFY_ROOT": project_root / "providers" / "minimax",
        "OLIVIA_MINIMAX_WORKER": project_root / "providers" / "minimax" / "worker.py",
        "OLIVIA_ROFORMER_EXE": project_root / "providers" / "roformer" / "roformer.exe",
        "OLIVIA_ROFORMER_MODEL_PATH": project_root / "providers" / "roformer" / "model.ckpt",
        "OLIVIA_ROFORMER_CONFIG_PATH": project_root / "providers" / "roformer" / "config.yaml",
        "OLIVIA_LATENTSYNC_PYTHON": project_root / "providers" / "latentsync" / "python.exe",
        "OLIVIA_LATENTSYNC_ROOT": project_root / "providers" / "latentsync",
    }
    provider_roots = {
        provider_paths["OLIVIA_MINIMAX_COMFY_ROOT"],
        provider_paths["OLIVIA_LATENTSYNC_ROOT"],
    }
    for path in provider_paths.values():
        path.mkdir(parents=True, exist_ok=True) if path in provider_roots else path.parent.mkdir(
            parents=True, exist_ok=True
        )
        if path not in provider_roots:
            path.write_bytes(b"synthetic")

    monkeypatch.chdir(unrelated_cwd)
    monkeypatch.setenv("OLIVIA_PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("OLIVIA_FFMPEG_EXE", str(ffmpeg))
    monkeypatch.setenv("OLIVIA_SPOKEN_SCENE_CANDIDATES", "assets/reference.mp4")
    for name, path in provider_paths.items():
        monkeypatch.setenv(name, path.relative_to(project_root).as_posix())

    observed: dict[str, object] = {}

    class FakeMiniMaxWorker:
        def __init__(self, *, python_path, worker_path, comfy_root, **_kwargs):
            observed["minimax_paths"] = (python_path, worker_path, comfy_root)

        def generate(self, _content, _reply_text, destination, **_kwargs):
            Path(destination).write_bytes(b"song")
            return {"audio_model": "synthetic"}

    def fake_run(command, error_code, **_kwargs):
        observed.setdefault("commands", []).append((command, error_code))
        if error_code == "ROFORMER_FAILED":
            output_root = Path(command[command.index("--store_dir") + 1])
            (output_root / "synthetic_vocals.wav").write_bytes(b"vocals")
        elif error_code == "ROFORMER_INPUT_CONVERSION_FAILED":
            Path(command[-1]).write_bytes(b"wav")
        elif error_code == "MUSIC_REPLY_AUDIO_MUX_FAILED":
            Path(command[-1]).write_bytes(b"song-video")

    def fake_render_reply(_reply_text, output, **kwargs):
        observed["ordinary_kwargs"] = kwargs
        Path(output).write_bytes(b"normal-video")
        return {}

    def fake_latentsync(_source, _audio, output, *, python_path, latentsync_root):
        observed["latentsync_paths"] = (python_path, latentsync_root)
        Path(output).write_bytes(b"face-video")
        return {}

    def fake_prepare(_reference, destination):
        Path(destination).write_bytes(b"spoken-base")
        return destination

    monkeypatch.setattr(music_reply, "MiniMaxMusic3Worker", FakeMiniMaxWorker)
    monkeypatch.setattr(music_reply, "_run", fake_run)
    monkeypatch.setattr(music_reply, "_ffmpeg", lambda: str(ffmpeg))
    monkeypatch.setattr(music_reply, "render_reply_video", fake_render_reply)
    monkeypatch.setattr(music_reply, "render_latentsync_video", fake_latentsync)
    monkeypatch.setattr(music_reply, "prepare_official_spoken_base", fake_prepare)
    monkeypatch.setattr(
        music_reply,
        "plan_song_content",
        lambda *_args: SimpleNamespace(lyrics="lyrics", caption="caption", emotion="steady"),
    )
    monkeypatch.setattr(
        music_reply,
        "concat_videos",
        lambda _normal, _song, output, **_kwargs: Path(output).write_bytes(b"final-video"),
    )

    output = tmp_path / "out" / "reply.mp4"
    render_musical_reply(
        "letter",
        "reply",
        output,
        normal_video_path=tmp_path / "out" / "normal.mp4",
        official_reply_reference_path=reference,
        song_video_path=tmp_path / "out" / "song.mp4",
        tts_config_path=tmp_path / "tts.json",
        visual_config_path=tmp_path / "visual.json",
        worker_path=tmp_path / "worker.py",
        performance_video_path=performance,
        duration_seconds=40,
    )

    assert observed["minimax_paths"] == (
        provider_paths["OLIVIA_MINIMAX_COMFY_PYTHON"],
        provider_paths["OLIVIA_MINIMAX_WORKER"],
        provider_paths["OLIVIA_MINIMAX_COMFY_ROOT"],
    )
    assert observed["ordinary_kwargs"]["latentsync_python_path"] == provider_paths[
        "OLIVIA_LATENTSYNC_PYTHON"
    ]
    assert observed["ordinary_kwargs"]["latentsync_root"] == provider_paths[
        "OLIVIA_LATENTSYNC_ROOT"
    ]
    assert observed["latentsync_paths"] == (
        provider_paths["OLIVIA_LATENTSYNC_PYTHON"],
        provider_paths["OLIVIA_LATENTSYNC_ROOT"],
    )
    roformer_commands = [
        command for command, error_code in observed["commands"] if error_code == "ROFORMER_FAILED"
    ]
    assert roformer_commands[0][-4:] == [
        "--model_path",
        str(provider_paths["OLIVIA_ROFORMER_MODEL_PATH"]),
        "--config_path",
        str(provider_paths["OLIVIA_ROFORMER_CONFIG_PATH"]),
    ]
    assert roformer_commands[0][0] == str(provider_paths["OLIVIA_ROFORMER_EXE"])
    assert output.read_bytes() == b"final-video"
