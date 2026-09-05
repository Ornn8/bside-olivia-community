from __future__ import annotations

import json
import sys
import wave
from array import array
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import tts.delivery as delivery
import http_contract

from runtime.reply.reply_delivery import (
    build_ordinary_video_llm_content,
    build_ordinary_video_repair_content,
    ordinary_video_reply_length_ok,
    plan_reply_delivery,
)
from tts import TTSConfig, external_cosyvoice_worker
from tts.external_audio_quality_worker import assess_transcript, normalize_transcript
from tts.delivery import (
    DeliveryAudioError,
    _fit_overlong_wav,
    _validated_quality_report,
    build_external_delivery_request,
    delivery_configured,
    delivery_tempo_factor,
    render_delivery_wav,
)
from voice_direction import VoicePerformancePlan

_QUALITY_EXPECTED = "这是完整的冻结语音内容，必须原样说完。"
_QUALITY_FORBIDDEN = "声音柔软自然地承接"


def _quality_report(*, passed: bool = False, expected: str = _QUALITY_EXPECTED,
                    forbidden: str = _QUALITY_FORBIDDEN) -> dict[str, object]:
    transcript = expected if passed else "这是完全不同的内容。"
    return assess_transcript(expected, transcript, forbidden)


def test_ordinary_video_copy_contract_targets_cross_lingual_delivery_length() -> None:
    initial = build_ordinary_video_llm_content("原始来信")
    repair = build_ordinary_video_repair_content("旧候选")

    assert "180到200个非空白字符" in initial
    assert "目标为190字" in initial
    assert "180到200个非空白字符" in repair
    for prompt in (initial, repair):
        assert "汉字、标点、数字和英文字母均计入" in prompt
        assert "空格和换行不计入" in prompt
    assert ordinary_video_reply_length_ok("林" * 179 + "。") is True
    assert ordinary_video_reply_length_ok("林" * 190 + "。" * 11) is False
    assert ordinary_video_reply_length_ok("林" * 179) is False
    assert ordinary_video_reply_length_ok("林" * 180) is True
    assert ordinary_video_reply_length_ok("林" * 200) is True
    assert ordinary_video_reply_length_ok("林" * 201) is False


def test_directed_delivery_error_schema_is_stable() -> None:
    assert http_contract.LETTER_DETAIL_MEDIA_ERROR_CODES == {
        "BREEZE_TTS_10GB_VRAM_REQUIRED": {
            "status": "UNAVAILABLE",
            "retryable": True,
        },
        "BREEZE_TTS_GPU_CAPABILITY_UNVERIFIED": {
            "status": "UNAVAILABLE",
            "retryable": True,
        },
        "BREEZE_TTS_NVIDIA_GPU_REQUIRED": {
            "status": "UNAVAILABLE",
            "retryable": True,
        },
        "MEDIA_PROVIDER_UNAVAILABLE": {"status": "UNAVAILABLE", "retryable": True},
        "TTS_CONTENT_GATE_UNAVAILABLE": {
            "status": "UNAVAILABLE",
            "retryable": True,
        },
        "TTS_CONTENT_GATE_REJECTED": {
            "status": "FAILED",
            "retryable": False,
        },
    }


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
    reply_text = "First sentence。Second sentence。"
    plan = VoicePerformancePlan(
        reply_text=reply_text,
        overall_emotion="restrained empathy becoming steady reassurance",
        global_speed=1.0,
        energy=0.62,
        breath_before_sentences=(),
        emphasize_sentences=(),
        short_instruction="声音柔软自然地承接，再缓缓托起给到力量",
    )
    config = SimpleNamespace(
        runtime_root="runtime",
        model_dir="model",
        reference_audio="reference.wav",
        fp16=True,
    )

    request = build_external_delivery_request(config, plan)

    assert request["voice_condition_mode"] == "instruct2_single_pass"
    assert plan.render_text == reply_text
    assert request["text"] == reply_text
    assert "[breath]" not in str(request["text"])
    assert "<strong>" not in str(request["text"])
    assert request["llm_variant"] == "base"
    assert request["speed"] == 1.0
    assert request["instruct_text"].count("<|endofprompt|>") == 1
    assert request["instruct_text"] == (
        "You are a helpful assistant. "
        "声音柔软自然地承接，再缓缓托起给到力量。<|endofprompt|>"
    )
    assert request["quality_forbidden_text"] == (
        "You are a helpful assistant. "
        "声音柔软自然地承接，再缓缓托起给到力量"
    )
    assert request["performance_control_mode"] == "single_pass_llm_short_instruct"
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
    monkeypatch.setattr(
        external_cosyvoice_worker,
        "_validate_base_model",
        lambda _model_dir: None,
    )
    output = tmp_path / "directed.wav"

    external_cosyvoice_worker._synthesize(
        {
            "runtime_root": str(tmp_path),
            "model_dir": str(tmp_path),
            "reference_audio": str(tmp_path / "reference.wav"),
            "voice_condition_mode": "instruct2_single_pass",
            "text": "one complete frozen reply",
            "instruct_text": "steady<|endofprompt|> reassurance<|endofprompt|>",
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
    monkeypatch.setattr(
        external_cosyvoice_worker,
        "_validate_base_model",
        lambda _model_dir: None,
    )

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


@pytest.mark.parametrize(
    "control_text",
    ["<|endofprompt|>", "[breath]", "<strong>强调</strong>", "**强调**"],
)
def test_external_worker_rejects_frozen_text_with_model_control_token(
    tmp_path,
    control_text: str,
) -> None:
    calls: list[object] = []

    class Model:
        sample_rate = 100

        def inference_instruct2(self, *_args, **_kwargs):
            calls.append("called")
            return ()

    with pytest.raises(RuntimeError, match="TTS_DIRECTED_TEXT_CONTAINS_CONTROL_TOKEN"):
        external_cosyvoice_worker._synthesize_instruct2_single_pass(
            Model(),
            {
                "reference_audio": str(tmp_path / "reference.wav"),
                "text": f"frozen reply {control_text}",
                "instruct_text": "steady reassurance<|endofprompt|>",
            },
            tmp_path / "directed.wav",
        )

    assert calls == []


@pytest.mark.parametrize(
    "control_text",
    ["<|endofprompt|>", "[breath]", "<strong>强调</strong>", "**强调**"],
)
def test_directed_request_rejects_frozen_text_with_model_control_token(
    control_text: str,
) -> None:
    plan = VoicePerformancePlan(
        reply_text=f"frozen reply {control_text}",
        overall_emotion="steady reassurance",
        global_speed=1.06,
        energy=0.62,
        breath_before_sentences=(),
        emphasize_sentences=(),
    )
    config = SimpleNamespace(
        runtime_root="runtime",
        model_dir="model",
        reference_audio="reference.wav",
        fp16=True,
    )

    with pytest.raises(DeliveryAudioError, match="TTS_DIRECTED_TEXT_CONTAINS_CONTROL_TOKEN"):
        build_external_delivery_request(config, plan)


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
    quality_runtime = upstreams["b06-whisper-quality-runtime"]
    quality_model = upstreams["b06-whisper-base-model"]
    acceptance = (root / "docs/B06_LOCAL_TTS_ACCEPTANCE.md").read_text(
        encoding="utf-8"
    )
    normalized_acceptance = " ".join(acceptance.split())

    assert runtime["revision"] == "074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc"
    assert runtime["license"] == "Apache-2.0"
    assert runtime["adapter_boundary"] in normalized_acceptance
    assert runtime["uninstall_path"] in normalized_acceptance
    assert model["revision"] == "29e01c4e8d000f4bcd70751be16fa94bf3d85a18"
    assert quality_runtime["revision"] == "31243bad24cc746f07d4c8bfdd2d974872cb1803"
    assert quality_runtime["license"] == "MIT"
    assert quality_model["revision"] == "31243bad24cc746f07d4c8bfdd2d974872cb1803"
    assert (
        "ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e"
        in quality_model["version"]
    )
    assert quality_model["license"] == "MIT"
    assert "inference_instruct2" in acceptance
    assert "41.28 seconds" in acceptance
    assert "24 kHz mono" in acceptance
    assert "863ef5185c448f189c46524fd8e87010bf353bc2bf8e3df9f59bdc0948ec14ce" in acceptance
    assert "Candidate 1" in acceptance


def test_content_gate_accepts_small_asr_substitutions_but_rejects_control_or_omission() -> None:
    expected = (
        "这是完全合成的语音验收样例。"
        "虚构的月面温室响起提示音，巡检员关闭报警器，再核对三组传感器。"
        "设备稳定后，她把结果写入公开测试日志。"
    )
    instruction = "保持清楚自然，结尾平稳收住"

    accepted = assess_transcript(
        expected,
        expected.replace("，", "。").replace("。", " "),
        instruction,
    )
    contaminated = assess_transcript(expected, instruction + expected, instruction)
    fixed_prefix = "You are a helpful assistant"
    normalized_expected = normalize_transcript(expected)
    normalized_prefix = normalize_transcript(fixed_prefix)
    equal_length_prefix = normalized_prefix + normalized_expected[len(normalized_prefix) :]
    prefix_contaminated = assess_transcript(
        normalized_expected,
        equal_length_prefix,
        fixed_prefix + instruction,
    )
    omitted = assess_transcript(expected, "这是完全合成的语音验收样例。", instruction)
    one_missing = assess_transcript(expected, expected.replace("月", "", 1), instruction)
    one_extra = assess_transcript(expected, expected + "啊", instruction)
    repeated = assess_transcript(expected, expected + expected[-4:], instruction)

    assert accepted["passed"] is True
    assert contaminated["checks"]["instruction_overlap"] is False
    assert prefix_contaminated["checks"]["instruction_overlap"] is False
    assert omitted["checks"]["length_ratio"] is False
    assert one_missing["checks"]["contiguous_omission"] is False
    assert one_extra["checks"]["extra_speech"] is False
    assert repeated["checks"]["repetition"] is False


def test_empty_asr_report_keeps_the_complete_rejection_schema() -> None:
    report = assess_transcript("完整冻结正文", "", "声音柔软自然地承接")

    assert _validated_quality_report(
        report, expected_text="完整冻结正文", forbidden_text="声音柔软自然地承接"
    )["error_code"] == "TTS_CONTENT_EMPTY"


@pytest.mark.parametrize(
    "report",
    [
        {"passed": True},
        {"passed": False},
        {**_quality_report(), "error_code": {}},
        {**_quality_report(), "error_code": "UNKNOWN_REJECTION"},
        {**_quality_report(passed=True), "checks": {"cer": True}},
        {**_quality_report(), "cer": 0.0},
        {**_quality_report(), "transcript": _QUALITY_EXPECTED},
    ],
)
def test_quality_gate_rejects_incomplete_or_unknown_reports(
    report: dict[str, object],
) -> None:
    with pytest.raises(DeliveryAudioError, match="TTS_CONTENT_GATE_UNAVAILABLE"):
        _validated_quality_report(
            report, expected_text=_QUALITY_EXPECTED, forbidden_text=_QUALITY_FORBIDDEN
        )


def test_external_worker_rejects_unpinned_base_llm_before_model_load(
    tmp_path,
) -> None:
    model = tmp_path / "model"
    model.mkdir()
    (model / "llm.pt").write_bytes(b"not-the-pinned-base-model")

    with pytest.raises(RuntimeError, match="COSYVOICE_BASE_MODEL_HASH_MISMATCH"):
        external_cosyvoice_worker._synthesize(
            {
                "runtime_root": str(tmp_path),
                "model_dir": str(model),
                "reference_audio": str(tmp_path / "reference.wav"),
                "voice_condition_mode": "instruct2_single_pass",
                "llm_variant": "base",
                "verify_accepted_base_model": True,
                "text": "完整冻结回信",
                "instruct_text": "声音柔软自然地承接，再缓缓托起给到力量",
            },
            tmp_path / "directed.wav",
        )


def test_directed_delivery_preflight_requires_pinned_model_and_asr_runtime(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    model = tmp_path / "model"
    cache = tmp_path / "whisper"
    runtime.mkdir()
    model.mkdir()
    cache.mkdir()
    executable = tmp_path / "python.exe"
    reference = tmp_path / "reference.wav"
    executable.write_bytes(b"synthetic executable")
    reference.write_bytes(b"synthetic reference")
    (model / "llm.pt").write_bytes(b"synthetic model")
    (cache / "base.pt").write_bytes(b"synthetic asr")
    verified: list[tuple[str, str]] = []

    def fake_verified(path: Path, expected: str) -> bool:
        verified.append((path.name, expected))
        return True

    monkeypatch.setattr(delivery, "_verified_file", fake_verified)
    monkeypatch.setattr(
        delivery,
        "_quality_runtime_available",
        lambda *_args: True,
    )
    config = TTSConfig(
        runtime_root=str(runtime),
        model_dir=str(model),
        reference_audio=str(reference),
        provider_options={
            "external_python": str(executable),
            "quality_gate_cache_root": str(cache),
        },
    )

    assert delivery_configured(config, require_quality_gate=True) is True
    assert [name for name, _expected in verified] == ["llm.pt", "base.pt"]
    assert all(len(expected) == 64 for _name, expected in verified)


def test_directed_delivery_preflight_rejects_implicit_whisper_cache(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    model = tmp_path / "model"
    runtime.mkdir()
    model.mkdir()
    executable = tmp_path / "python.exe"
    reference = tmp_path / "reference.wav"
    executable.write_bytes(b"synthetic executable")
    reference.write_bytes(b"synthetic reference")
    (model / "llm.pt").write_bytes(b"synthetic model")
    monkeypatch.setattr(delivery, "_verified_file", lambda *_args: True)
    config = TTSConfig(
        runtime_root=str(runtime),
        model_dir=str(model),
        reference_audio=str(reference),
        provider_options={"external_python": str(executable)},
    )

    assert delivery_configured(config, require_quality_gate=True) is False


def test_non_quality_delivery_does_not_require_quality_worker_or_pinned_base(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    model = tmp_path / "model"
    runtime.mkdir()
    model.mkdir()
    executable = tmp_path / "python.exe"
    reference = tmp_path / "reference.wav"
    executable.write_bytes(b"synthetic executable")
    reference.write_bytes(b"synthetic reference")
    (model / "llm.pt").write_bytes(b"unpinned model")
    verified: list[str] = []
    monkeypatch.setattr(
        delivery,
        "_verified_file",
        lambda path, _expected: verified.append(path.name) or False,
    )
    config = TTSConfig(
        runtime_root=str(runtime),
        model_dir=str(model),
        reference_audio=str(reference),
        provider_options={"external_python": str(executable)},
    )

    assert delivery_configured(config) is True
    assert verified == []


def test_quality_runtime_preflight_enforces_pinned_distribution_version(
    tmp_path,
    monkeypatch,
) -> None:
    observed: list[list[str]] = []

    def fake_run(command, **_kwargs):
        observed.append([str(item) for item in command])
        return SimpleNamespace(returncode=3)

    monkeypatch.setattr(delivery.subprocess, "run", fake_run)

    assert delivery._quality_runtime_available(str(tmp_path / "python.exe"), 1, 1) is False
    assert "m.version('openai-whisper')" in observed[0][-1]
    assert "20250625" in observed[0][-1]


def test_three_rejected_candidates_never_replace_existing_output(
    tmp_path,
    monkeypatch,
) -> None:
    output = tmp_path / "reply.wav"
    output.write_bytes(b"existing-output")
    calls = {"tts": 0, "quality": 0}

    def fake_run(command, **_kwargs):
        command = [str(item) for item in command]
        target = Path(command[command.index("--output") + 1])
        if command[1].endswith("external_cosyvoice_worker.py"):
            calls["tts"] += 1
            with wave.open(str(target), "wb") as rendered:
                rendered.setnchannels(1)
                rendered.setsampwidth(2)
                rendered.setframerate(100)
                rendered.writeframes(array("h", [1] * 4_500).tobytes())
        else:
            calls["quality"] += 1
            target.write_text(
                    json.dumps(_quality_report(expected=plan.spoken_text, forbidden=plan.short_instruction)),
                encoding="utf-8",
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(delivery, "delivery_configured", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(delivery.subprocess, "run", fake_run)
    config = TTSConfig(
        runtime_root=str(tmp_path),
        model_dir=str(tmp_path),
        reference_audio=str(tmp_path / "reference.wav"),
        provider_options={"external_python": sys.executable},
    )
    plan = VoicePerformancePlan(
        reply_text="林" * 190,
        overall_emotion="声音柔软自然地承接，再缓缓托起给到力量",
        global_speed=1.0,
        energy=0.55,
        breath_before_sentences=(),
        emphasize_sentences=(),
        short_instruction="声音柔软自然地承接，再缓缓托起给到力量",
    )

    with pytest.raises(DeliveryAudioError, match="TTS_CONTENT_GATE_REJECTED"):
        render_delivery_wav(config, plan, output)

    assert calls == {"tts": 3, "quality": 3}
    assert output.read_bytes() == b"existing-output"


def test_mixed_duration_failures_do_not_fabricate_three_quality_rejections(
    tmp_path,
    monkeypatch,
) -> None:
    output = tmp_path / "reply.wav"
    output.write_bytes(b"existing-output")
    calls = {"tts": 0, "quality": 0}

    def fake_run(command, **_kwargs):
        command = [str(item) for item in command]
        target = Path(command[command.index("--output") + 1])
        if command[1].endswith("external_cosyvoice_worker.py"):
            calls["tts"] += 1
            frame_count = 4_500 if calls["tts"] == 1 else 3_000
            with wave.open(str(target), "wb") as rendered:
                rendered.setnchannels(1)
                rendered.setsampwidth(2)
                rendered.setframerate(100)
                rendered.writeframes(array("h", [1] * frame_count).tobytes())
        else:
            calls["quality"] += 1
            target.write_text(
                    json.dumps(_quality_report(expected=plan.spoken_text, forbidden=plan.short_instruction)),
                encoding="utf-8",
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(delivery, "delivery_configured", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(delivery.subprocess, "run", fake_run)
    config = TTSConfig(
        runtime_root=str(tmp_path),
        model_dir=str(tmp_path),
        reference_audio=str(tmp_path / "reference.wav"),
        provider_options={"external_python": sys.executable},
    )
    plan = VoicePerformancePlan(
        reply_text="林" * 190,
        overall_emotion="声音柔软自然地承接，再缓缓托起给到力量",
        global_speed=1.0,
        energy=0.55,
        breath_before_sentences=(),
        emphasize_sentences=(),
        short_instruction="声音柔软自然地承接，再缓缓托起给到力量",
    )

    with pytest.raises(DeliveryAudioError, match="TTS_DELIVERY_DURATION_OUT_OF_RANGE"):
        render_delivery_wav(config, plan, output)

    assert calls == {"tts": 3, "quality": 1}
    assert output.read_bytes() == b"existing-output"


def test_quality_request_write_failure_is_sanitized_and_atomic(
    tmp_path,
    monkeypatch,
) -> None:
    output = tmp_path / "reply.wav"
    output.write_bytes(b"existing-output")
    original_write_text = Path.write_text

    def guarded_write_text(path: Path, *args, **kwargs):
        if path.name == "quality-request.json":
            raise OSError("private-path-must-not-escape")
        return original_write_text(path, *args, **kwargs)

    def fake_run(command, **_kwargs):
        command = [str(item) for item in command]
        target = Path(command[command.index("--output") + 1])
        with wave.open(str(target), "wb") as rendered:
            rendered.setnchannels(1)
            rendered.setsampwidth(2)
            rendered.setframerate(100)
            rendered.writeframes(array("h", [1] * 4_500).tobytes())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(delivery, "delivery_configured", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(delivery.subprocess, "run", fake_run)
    monkeypatch.setattr(Path, "write_text", guarded_write_text)
    config = TTSConfig(
        runtime_root=str(tmp_path),
        model_dir=str(tmp_path),
        reference_audio=str(tmp_path / "reference.wav"),
        provider_options={"external_python": sys.executable},
    )
    plan = VoicePerformancePlan(
        reply_text="林" * 190,
        overall_emotion="声音柔软自然地承接，再缓缓托起给到力量",
        global_speed=1.0,
        energy=0.55,
        breath_before_sentences=(),
        emphasize_sentences=(),
        short_instruction="声音柔软自然地承接，再缓缓托起给到力量",
    )

    with pytest.raises(DeliveryAudioError) as exc_info:
        render_delivery_wav(config, plan, output)

    assert str(exc_info.value) == "TTS_CONTENT_GATE_UNAVAILABLE"
    assert "private-path" not in str(exc_info.value)
    assert output.read_bytes() == b"existing-output"
