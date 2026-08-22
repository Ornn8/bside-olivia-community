"""Provider-neutral B06 tests; real CUDA/model acceptance is a separate tranche."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from tts import TTSConfig, TTSProfileManager, TTSRequest, TTSService
from tts.audio import audio_metrics
from tts.contracts import AudioChunk, TTSUnavailable
from tts.registry import TTSProviderRegistry
from tts.sentence import split_sentences


class FakeProvider:
    name = "fake"
    license_id = "MIT"

    def __init__(self, config: TTSConfig) -> None:
        self.config = config

    def health(self):
        return {"status": "available", "provider": self.name, "license_id": self.license_id}

    def stream_sentence(self, text, request, sentence_index):
        for chunk_index in range(4):
            if request.cancel_event.is_set():
                return
            time.sleep(0.003)
            yield AudioChunk((0.05, 0.1, -0.1, 0.05), 16000, sentence_index, chunk_index)

    def close(self):
        return None


def _registry() -> TTSProviderRegistry:
    registry = TTSProviderRegistry()
    registry.register("fake", FakeProvider, license_id="MIT")
    return registry


def _run(coro):
    return asyncio.run(coro)


def test_split_sentences_preserves_sentence_text_and_punctuation() -> None:
    assert split_sentences("第一句。第二句！ third?") == ("第一句。", "第二句！", "third?")


def test_sentence_stream_writes_playable_wav_and_reports_timestamps(tmp_path: Path) -> None:
    config = TTSConfig(profile="fake", provider="fake", fallback="unavailable")
    service = TTSService(config, registry=_registry())
    output = tmp_path / "speech.wav"
    try:
        result = _run(service.synthesize(TTSRequest("第一句。第二句！"), output_path=output))
    finally:
        service.close()

    assert result.status == "completed"
    assert result.sentence_count == 2
    assert result.chunk_count == 8
    assert result.first_audio_ms is not None
    assert result.ended_ms is not None
    assert result.ended_ms >= result.first_audio_ms
    assert output.is_file()
    metrics = audio_metrics(output)
    assert metrics["sample_rate"] == 16000
    assert metrics["has_audio"] is True
    assert metrics["clipped_samples"] == 0
    assert metrics["truncated"] is False


def test_unavailable_and_disabled_are_truthful_text_fallbacks(tmp_path: Path) -> None:
    unavailable = TTSService(TTSConfig(provider="not-installed", fallback="text"), registry=_registry())
    disabled = TTSService(
        TTSConfig(provider="fake", enabled=False, fallback="text"), registry=_registry()
    )
    try:
        unavailable_result = _run(unavailable.synthesize(TTSRequest("保留文本。")))
        disabled_result = _run(disabled.synthesize(TTSRequest("停用后仍然保留文本。")))
    finally:
        unavailable.close()
        disabled.close()

    assert unavailable_result.status == "text_fallback"
    assert unavailable_result.error_code == "TTS_PROVIDER_UNKNOWN"
    assert unavailable_result.fallback_text == "保留文本。"
    assert disabled_result.status == "text_fallback"
    assert disabled_result.error_code == "TTS_DISABLED"
    assert disabled.health()["status"] == "disabled"


def test_cancelled_long_request_does_not_poison_next_request() -> None:
    async def exercise():
        service = TTSService(TTSConfig(profile="fake", provider="fake", fallback="unavailable"), registry=_registry())
        try:
            run = await service.start(TTSRequest("很长的一句话。" * 20))
            seen_chunk = False
            async for event in run.events():
                if event.event == "audio_chunk":
                    seen_chunk = True
                    assert run.cancel() is True
                    break
            cancelled = await run.wait()
            recovered = await service.synthesize(TTSRequest("取消后继续。"))
            return seen_chunk, cancelled, recovered
        finally:
            service.close()

    seen_chunk, cancelled, recovered = _run(exercise())
    assert seen_chunk is True
    assert cancelled.status == "cancelled"
    assert cancelled.error_code == "TTS_CANCELLED"
    assert recovered.status == "completed"


def test_profile_install_disable_customize_uninstall_preserves_external_assets(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "cosyvoice" / "cli").mkdir(parents=True)
    (runtime / "cosyvoice" / "cli" / "cosyvoice.py").write_text("# external runtime\n", encoding="utf-8")
    (runtime / "LICENSE").write_text("Apache License\n", encoding="utf-8")
    model = tmp_path / "model"
    model.mkdir()
    for filename in (
        "cosyvoice3.yaml",
        "llm.pt",
        "flow.pt",
        "hift.pt",
        "campplus.onnx",
        "speech_tokenizer_v3.onnx",
        "speech_tokenizer_v3.batch.onnx",
    ):
        (model / filename).write_bytes(b"external-model-placeholder")
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"external-reference")
    manager = TTSProfileManager(tmp_path / "state")
    config = TTSConfig(
        runtime_root=str(runtime),
        model_dir=str(model),
        reference_audio=str(reference),
        reference_text="参考文本",
    )

    installed = manager.install(config)
    assert installed["status"] == "INSTALLED"
    assert installed["external_assets_copied"] is False
    assert manager.set_enabled(config.profile, False)["status"] == "DISABLED"
    assert manager.set_enabled(config.profile, True)["status"] == "ENABLED"
    customized = manager.customize(config.profile, {"speed": 1.2, "leading_trim_seconds": 0.3})
    assert customized["status"] == "CUSTOMIZED"
    assert manager.config(config.profile).speed == 1.2
    dry_run = manager.uninstall(config.profile)
    assert dry_run["status"] == "DRY_RUN"
    assert (tmp_path / "state" / "profiles" / "cosyvoice3-live.json").is_file()
    removed = manager.uninstall(config.profile, dry_run=False)
    assert removed["status"] == "UNINSTALLED"
    assert not (tmp_path / "state" / "profiles" / "cosyvoice3-live.json").exists()
    assert runtime.joinpath("LICENSE").is_file()
    assert model.joinpath("llm.pt").read_bytes() == b"external-model-placeholder"
    assert reference.read_bytes() == b"external-reference"
