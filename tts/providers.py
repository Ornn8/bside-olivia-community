"""Lazy local provider adapters.

Only the locally installed CosyVoice3 adapter is registered in this tranche.
It never downloads a missing model: a missing runtime/model/reference asset is
reported as unavailable and may be converted to the configured text fallback.
"""

from __future__ import annotations

import gc
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .contracts import AudioChunk, TTSConfig, TTSRequest, TTSUnavailable


_MODEL_FILES = (
    "cosyvoice3.yaml",
    "llm.pt",
    "flow.pt",
    "hift.pt",
    "campplus.onnx",
    "speech_tokenizer_v3.onnx",
    "speech_tokenizer_v3.batch.onnx",
)


@dataclass(eq=False)
class _ExternalJob:
    """One worker process owned by exactly one TTS request."""

    process: subprocess.Popen[bytes] | None = None
    stopping: bool = False
    finished: threading.Event = field(default_factory=threading.Event)


class CosyVoice3Provider:
    """CosyVoice3 0.5B-2512 zero-shot provider using a private local profile."""

    name = "cosyvoice3"
    license_id = "Apache-2.0"

    def __init__(self, config: TTSConfig) -> None:
        self.config = config
        self._model: Any | None = None
        self.sample_rate: int | None = None
        self._trimmed_first_sentence = False
        self._wetext_module: Any | None = None
        self._original_wetext_normalizer: Any | None = None
        self._external_lock = threading.RLock()
        self._external_jobs: set[_ExternalJob] = set()
        self._closed = False

    @property
    def runtime_root(self) -> Path:
        return Path(self.config.runtime_root)

    @property
    def model_dir(self) -> Path:
        return Path(self.config.model_dir)

    @property
    def reference_audio(self) -> Path:
        return Path(self.config.reference_audio)

    @property
    def external_python(self) -> Path | None:
        value = str(self.config.provider_options.get("external_python", "") or "").strip()
        return Path(value) if value else None

    def _missing_files(self) -> list[str]:
        missing: list[str] = []
        for relative in ("cosyvoice/cli/cosyvoice.py", "LICENSE"):
            if not (self.runtime_root / relative).is_file():
                missing.append(f"runtime:{relative}")
        for relative in _MODEL_FILES:
            if not (self.model_dir / relative).is_file():
                missing.append(f"model:{relative}")
        if not self.reference_audio.is_file():
            missing.append("reference_audio")
        if not self.config.reference_text:
            missing.append("reference_text")
        executable = self.external_python
        if executable is not None and not executable.is_file():
            missing.append("external_python")
        return missing

    def health(self) -> dict[str, Any]:
        with self._external_lock:
            if self._closed:
                return {
                    "status": "unavailable",
                    "provider": self.name,
                    "reason_code": "TTS_CLOSED",
                    "license_id": self.license_id,
                }
        if self.config.license_id != self.license_id:
            return {
                "status": "unavailable",
                "provider": self.name,
                "reason_code": "TTS_LICENSE_UNVERIFIED",
                "license_id": self.config.license_id,
            }
        missing = self._missing_files()
        if missing:
            return {
                "status": "unavailable",
                "provider": self.name,
                "reason_code": "TTS_ASSET_MISSING",
                "missing": missing,
                "license_id": self.license_id,
            }
        return {
            "status": "available",
            "provider": self.name,
            "license_id": self.license_id,
            "model": "Fun-CosyVoice3-0.5B-2512",
            "streaming": True,
            "reference_audio_local_only": True,
            "text_frontend": str(self.config.provider_options.get("text_frontend", "none")),
            "offline_only": True,
            "execution": "external-process" if self.external_python is not None else "in-process",
        }

    def _configure_local_environment(self) -> None:
        """Make a local profile deterministic and prevent accidental downloads."""

        for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "MODELSCOPE_OFFLINE"):
            os.environ[key] = "1"

        options = self.config.provider_options
        cache_value = str(options.get("numba_cache_dir", "") or "").strip()
        if cache_value:
            cache_dir = Path(cache_value)
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise TTSUnavailable("TTS_CACHE_CONFIG_INVALID") from exc
            os.environ["NUMBA_CACHE_DIR"] = str(cache_dir)

        temp_value = str(options.get("temp_root", "") or "").strip()
        if temp_value:
            temp_root = Path(temp_value)
            try:
                temp_root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise TTSUnavailable("TTS_TEMP_CONFIG_INVALID") from exc
            os.environ["TEMP"] = str(temp_root)
            os.environ["TMP"] = str(temp_root)

        frontend = str(options.get("text_frontend", "none") or "none").strip().lower()
        if frontend not in {"none", "local"}:
            raise TTSUnavailable("TTS_TEXT_FRONTEND_UNSUPPORTED")
        if frontend == "none":
            self._disable_network_frontend()
        else:
            self._install_local_wetext_frontend()

    def _disable_network_frontend(self) -> None:
        """Force CosyVoice's optional wetext frontend to fail closed locally."""

        try:
            module = importlib.import_module("wetext")
        except Exception:
            return
        if self._wetext_module is None:
            self._wetext_module = module
            self._original_wetext_normalizer = getattr(module, "Normalizer", None)

        class OfflineNormalizer:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("B06 offline profile disables remote wetext discovery")

        module.Normalizer = OfflineNormalizer

    def _install_local_wetext_frontend(self) -> None:
        """Use caller-supplied local FSTs without invoking ModelsScope."""

        root_value = str(self.config.provider_options.get("wetext_fst_root", "") or "").strip()
        root = Path(root_value)
        required = (
            root / "zh" / "tn" / "tagger.fst",
            root / "zh" / "tn" / "verbalizer.fst",
            root / "en" / "tn" / "tagger.fst",
            root / "en" / "tn" / "verbalizer.fst",
        )
        if not root_value or not all(path.is_file() for path in required):
            raise TTSUnavailable("TTS_LOCAL_FRONTEND_MISSING")
        try:
            module = importlib.import_module("wetext")
        except Exception as exc:
            raise TTSUnavailable("TTS_LOCAL_FRONTEND_MISSING") from exc
        if self._wetext_module is None:
            self._wetext_module = module
            self._original_wetext_normalizer = getattr(module, "Normalizer", None)
        original = self._original_wetext_normalizer
        if original is None:
            raise TTSUnavailable("TTS_LOCAL_FRONTEND_MISSING")
        zh_tagger = str(root / "zh" / "tn" / "tagger.fst")
        zh_verbalizer = str(root / "zh" / "tn" / "verbalizer.fst")
        en_tagger = str(root / "en" / "tn" / "tagger.fst")
        en_verbalizer = str(root / "en" / "tn" / "verbalizer.fst")

        class LocalNormalizer:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                remove_erhua = bool(kwargs.get("remove_erhua", False))
                zh_verbalizer_path = zh_verbalizer
                if remove_erhua:
                    candidate = root / "zh" / "tn" / "verbalizer_remove_erhua.fst"
                    if candidate.is_file():
                        zh_verbalizer_path = str(candidate)
                self.zh = original(
                    tagger_path=zh_tagger,
                    verbalizer_path=zh_verbalizer_path,
                    lang="zh",
                    operator="tn",
                )
                self.en = original(
                    tagger_path=en_tagger,
                    verbalizer_path=en_verbalizer,
                    lang="en",
                    operator="tn",
                )

            def normalize(self, text: str) -> str:
                model = self.zh if any("\u4e00" <= char <= "\u9fff" for char in text) else self.en
                return model.normalize(text)

        module.Normalizer = LocalNormalizer

    def _load(self) -> None:
        if self._model is not None:
            return
        health = self.health()
        if health["status"] != "available":
            raise TTSUnavailable(str(health.get("reason_code", "TTS_UNAVAILABLE")))
        runtime = str(self.runtime_root)
        self._configure_local_environment()
        if runtime not in sys.path:
            sys.path.insert(0, runtime)
        try:
            from cosyvoice.cli.cosyvoice import AutoModel
        except Exception as exc:  # pragma: no cover - exercised by real doctor runs
            raise TTSUnavailable("TTS_DEPENDENCY_MISSING") from exc
        try:
            self._model = AutoModel(model_dir=str(self.model_dir), fp16=self.config.fp16)
            self.sample_rate = int(self._model.sample_rate)
        except Exception as exc:  # pragma: no cover - depends on local CUDA/model state
            self._model = None
            raise TTSUnavailable("TTS_MODEL_LOAD_FAILED") from exc

    def stream_sentence(
        self, text: str, request: TTSRequest, sentence_index: int
    ) -> Iterator[AudioChunk]:
        if self.external_python is not None:
            yield from self._stream_sentence_external(text, request, sentence_index)
            return
        self._load()
        assert self._model is not None
        assert self.sample_rate is not None
        if sentence_index == 0:
            self._trimmed_first_sentence = False
        prompt_prefix = str(
            self.config.provider_options.get(
                "prompt_prefix", "You are a helpful assistant.<|endofprompt|>"
            )
        )
        prompt_text = prompt_prefix + self.config.reference_text
        try:
            generated = self._model.inference_zero_shot(
                text,
                prompt_text,
                str(self.reference_audio),
                stream=bool(request.stream),
                speed=self.config.speed,
            )
            trim_remaining = (
                int(self.config.leading_trim_seconds * self.sample_rate)
                if sentence_index == 0 and not self._trimmed_first_sentence
                else 0
            )
            chunk_index = 0
            for model_output in generated:
                if request.cancel_event.is_set():
                    return
                speech = model_output.get("tts_speech") if isinstance(model_output, dict) else None
                if speech is None:
                    continue
                values = [float(value) for value in speech.detach().cpu().float().reshape(-1).tolist()]
                if trim_remaining:
                    removed = min(trim_remaining, len(values))
                    values = values[removed:]
                    trim_remaining -= removed
                if not values:
                    continue
                if sentence_index == 0:
                    self._trimmed_first_sentence = True
                yield AudioChunk(tuple(values), self.sample_rate, sentence_index, chunk_index)
                chunk_index += 1
        except TTSUnavailable:
            raise
        except Exception as exc:  # pragma: no cover - depends on provider internals
            raise TTSUnavailable("TTS_PROVIDER_ERROR") from exc

    def _stream_sentence_external(
        self, text: str, request: TTSRequest, sentence_index: int
    ) -> Iterator[AudioChunk]:
        """Run the maintained CosyVoice venv and return its real PCM output.

        The worker owns model import/inference.  This process only moves one
        temporary WAV across the existing provider boundary and removes it on
        every terminal path.  It never creates a model cache or downloads.
        """

        health = self.health()
        if health.get("status") != "available":
            raise TTSUnavailable(str(health.get("reason_code", "TTS_UNAVAILABLE")))
        executable = self.external_python
        assert executable is not None
        job = self._begin_external_job()
        temp_parent = str(self.config.provider_options.get("temp_root", "") or "").strip()
        try:
            work = Path(tempfile.mkdtemp(prefix="olivia-cosyvoice-", dir=temp_parent or None))
        except OSError as exc:
            self._finish_external_job(job)
            raise TTSUnavailable("TTS_TEMP_CONFIG_INVALID") from exc
        request_path = work / "request.json"
        output_path = work / "speech.wav"
        try:
            if request.cancel_event.is_set() or self._job_stopping(job):
                return
            request_path.write_text(
                json.dumps(
                    {
                        "runtime_root": str(self.runtime_root),
                        "model_dir": str(self.model_dir),
                        "reference_audio": str(self.reference_audio),
                        "reference_text": self.config.reference_text,
                        "text": text,
                        "fp16": bool(self.config.fp16),
                        "speed": self.config.speed,
                        "stream": bool(request.stream),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            env = dict(os.environ)
            env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "MODELSCOPE_OFFLINE": "1"})
            try:
                process = subprocess.Popen(
                    [str(executable), str(Path(__file__).with_name("external_cosyvoice_worker.py")), "--request", str(request_path), "--output", str(output_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                )
            except OSError as exc:
                raise TTSUnavailable("TTS_EXTERNAL_PROCESS_UNAVAILABLE") from exc
            if self._attach_external_process(job, process) or request.cancel_event.is_set():
                self._stop_external_job(job)
                return
            while process.poll() is None:
                if request.cancel_event.is_set():
                    self._stop_external_job(job)
                    return
                time.sleep(0.04)
            if process.returncode != 0:
                raise TTSUnavailable("TTS_EXTERNAL_PROCESS_FAILED")
            sample_rate, samples = self._read_external_wav(output_path)
            if sentence_index == 0:
                trim = int(self.config.leading_trim_seconds * sample_rate)
                samples = samples[trim:]
            if samples:
                yield AudioChunk(samples, sample_rate, sentence_index, 0)
        finally:
            self._stop_external_job(job)
            shutil.rmtree(work, ignore_errors=True)
            self._finish_external_job(job)

    @staticmethod
    def _read_external_wav(path: Path) -> tuple[int, tuple[float, ...]]:
        try:
            with wave.open(str(path), "rb") as source:
                if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getframerate() <= 0:
                    raise ValueError("unsupported external WAV format")
                sample_rate = source.getframerate()
                raw = source.readframes(source.getnframes())
        except (OSError, EOFError, wave.Error, ValueError) as exc:
            raise TTSUnavailable("TTS_EXTERNAL_AUDIO_INVALID") from exc
        pcm = array("h")
        pcm.frombytes(raw)
        if sys.byteorder != "little":
            pcm.byteswap()
        if not pcm:
            raise TTSUnavailable("TTS_EMPTY_AUDIO")
        return sample_rate, tuple(value / 32768.0 for value in pcm)

    def _begin_external_job(self) -> _ExternalJob:
        with self._external_lock:
            if self._closed:
                raise TTSUnavailable("TTS_CLOSED")
            job = _ExternalJob()
            self._external_jobs.add(job)
            return job

    def _job_stopping(self, job: _ExternalJob) -> bool:
        with self._external_lock:
            return self._closed or job.stopping

    def _attach_external_process(self, job: _ExternalJob, process: subprocess.Popen[bytes]) -> bool:
        with self._external_lock:
            job.process = process
            return self._closed or job.stopping

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes] | None) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def _stop_external_job(self, job: _ExternalJob) -> None:
        with self._external_lock:
            job.stopping = True
            process = job.process
        self._terminate_process(process)

    def _finish_external_job(self, job: _ExternalJob) -> None:
        with self._external_lock:
            self._external_jobs.discard(job)
            job.finished.set()

    def close(self) -> None:
        with self._external_lock:
            self._closed = True
            jobs = tuple(self._external_jobs)
            for job in jobs:
                job.stopping = True
        for job in jobs:
            self._terminate_process(job.process)
        for job in jobs:
            job.finished.wait(timeout=3)
        model = self._model
        self._model = None
        self.sample_rate = None
        self._trimmed_first_sentence = False
        if self._wetext_module is not None and self._original_wetext_normalizer is not None:
            self._wetext_module.Normalizer = self._original_wetext_normalizer
        self._wetext_module = None
        self._original_wetext_normalizer = None
        if model is not None:
            del model
            gc.collect()
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
