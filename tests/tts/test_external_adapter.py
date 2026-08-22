from __future__ import annotations

import json
import sys
import threading
import wave
from array import array
from pathlib import Path

import pytest

from tts import TTSConfig, TTSRequest
from tts.contracts import TTSUnavailable
from tts.providers import CosyVoice3Provider


def _config(tmp_path: Path) -> TTSConfig:
    runtime = tmp_path / "CosyVoice"
    (runtime / "cosyvoice" / "cli").mkdir(parents=True)
    (runtime / "cosyvoice" / "cli" / "cosyvoice.py").write_text("# upstream\n", encoding="utf-8")
    (runtime / "LICENSE").write_text("Apache-2.0\n", encoding="utf-8")
    model = runtime / "model"
    model.mkdir()
    for name in (
        "cosyvoice3.yaml",
        "llm.pt",
        "flow.pt",
        "hift.pt",
        "campplus.onnx",
        "speech_tokenizer_v3.onnx",
        "speech_tokenizer_v3.batch.onnx",
    ):
        (model / name).write_bytes(b"external-only")
    reference = runtime / "reference.wav"
    reference.write_bytes(b"external-only")
    return TTSConfig(
        runtime_root=str(runtime),
        model_dir=str(model),
        reference_audio=str(reference),
        reference_text="reference",
        fallback="unavailable",
        provider_options={"external_python": sys.executable},
    )


def test_external_venv_adapter_emits_real_wav_metadata_without_importing_cosyvoice(tmp_path: Path, monkeypatch) -> None:
    calls: list[object] = []

    class CompletedProcess:
        returncode = 0

        def __init__(self, command, **_kwargs) -> None:
            calls.append(command)
            output = Path(command[-1])
            with wave.open(str(output), "wb") as sink:
                sink.setnchannels(1)
                sink.setsampwidth(2)
                sink.setframerate(24000)
                sink.writeframes(array("h", [1, -2, 3]).tobytes())

        def poll(self):
            return 0

    monkeypatch.setattr("tts.providers.subprocess.Popen", CompletedProcess)
    provider = CosyVoice3Provider(_config(tmp_path))
    chunks = list(provider.stream_sentence("外部 venv。", TTSRequest("外部 venv。"), 0))

    assert provider.health()["status"] == "available"
    assert provider.health()["execution"] == "external-process"
    assert len(calls) == 1
    assert len(chunks) == 1
    assert chunks[0].sample_rate == 24000
    assert chunks[0].sample_count == 3
    assert provider._model is None


def test_external_venv_adapter_terminates_and_cleans_up_when_cancelled(tmp_path: Path, monkeypatch) -> None:
    processes: list[object] = []

    class WaitingProcess:
        returncode = None

        def __init__(self, *_args, **_kwargs) -> None:
            processes.append(self)
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr("tts.providers.subprocess.Popen", WaitingProcess)
    provider = CosyVoice3Provider(_config(tmp_path))
    request = TTSRequest("取消。")
    request.cancel()

    assert list(provider.stream_sentence("取消。", request, 0)) == []
    assert processes == []
    assert provider._external_jobs == set()


def test_external_venv_jobs_are_isolated_and_close_rejects_delayed_work(tmp_path: Path, monkeypatch) -> None:
    started = threading.Event()
    processes: dict[str, object] = {}
    processes_lock = threading.Lock()

    class WaitingProcess:
        returncode = None

        def __init__(self, args, **_kwargs) -> None:
            self.terminated = False
            request_path = Path(args[args.index("--request") + 1])
            request_text = json.loads(request_path.read_text(encoding="utf-8"))["text"]
            with processes_lock:
                processes[request_text] = self
                if len(processes) == 2:
                    started.set()

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr("tts.providers.subprocess.Popen", WaitingProcess)
    provider = CosyVoice3Provider(_config(tmp_path))
    first = TTSRequest("first")
    second = TTSRequest("second")
    outcomes: dict[str, str | None] = {}

    def run(name: str, text: str, request: TTSRequest) -> None:
        try:
            list(provider.stream_sentence(text, request, 0))
            outcomes[name] = None
        except TTSUnavailable as exc:
            outcomes[name] = exc.code

    workers = [
        threading.Thread(target=run, args=("first", "first", first), daemon=True),
        threading.Thread(target=run, args=("second", "second", second), daemon=True),
    ]
    for worker in workers:
        worker.start()
    try:
        assert started.wait(timeout=2)

        first.cancel()
        workers[0].join(timeout=2)
        assert not workers[0].is_alive()
        assert processes["first"].terminated is True
        assert processes["second"].terminated is False

        provider.close()
        workers[1].join(timeout=2)
        assert not workers[1].is_alive()
        assert processes["second"].terminated is True
        assert outcomes == {"first": None, "second": "TTS_EXTERNAL_PROCESS_FAILED"}
        assert provider._external_jobs == set()
        provider.close()
        with pytest.raises(TTSUnavailable, match="TTS_CLOSED"):
            list(provider.stream_sentence("late", TTSRequest("late"), 0))
        assert set(processes) == {"first", "second"}
    finally:
        first.cancel()
        second.cancel()
        provider.close()
        for worker in workers:
            worker.join(timeout=2)


def test_external_venv_close_during_delayed_spawn_terminates_before_audio_read(tmp_path: Path, monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    processes: list[object] = []

    class DelayedProcess:
        returncode = None

        def __init__(self, *_args, **_kwargs) -> None:
            entered.set()
            assert release.wait(timeout=2)
            self.terminated = False
            processes.append(self)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr("tts.providers.subprocess.Popen", DelayedProcess)
    provider = CosyVoice3Provider(_config(tmp_path))
    outcome: list[str | None] = []

    def run() -> None:
        try:
            list(provider.stream_sentence("delayed", TTSRequest("delayed"), 0))
            outcome.append(None)
        except TTSUnavailable as exc:
            outcome.append(exc.code)

    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(timeout=2)
    closer = threading.Thread(target=provider.close)
    closer.start()
    release.set()
    worker.join(timeout=2)
    closer.join(timeout=2)

    assert outcome == [None]
    assert len(processes) == 1
    assert processes[0].terminated is True
    assert provider._external_jobs == set()
