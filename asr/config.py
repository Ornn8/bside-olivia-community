"""Configuration and provenance for the optional Nemotron local provider."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path, PureWindowsPath
from urllib.parse import urlparse

from .errors import AsrError


MODEL_REPO = "nvidia/nemotron-3.5-asr-streaming-0.6b"
MODEL_REVISION = "1c8deaecc64b91f034d73e08dd8b64625eb3395d"
MODEL_LICENSE = "OpenMDW-1.1"
MODEL_FILENAME = "nemotron-3.5-asr-streaming-0.6b.q8_0.gguf"
# SHA-256 recorded from the HF GGUF commit used for this fixed model
# revision.  Install tooling verifies the downloaded blob before it can be
# considered present.
MODEL_SHA256 = "a5c435f294eea8f88ce68dd27b8c3bfea7f777cb2fbba04fcd30eaa555f429ae"
NATIVE_MAX_CER = 0.20
NATIVE_MAX_WER = 0.20
RUNTIME_REPO = "https://github.com/NVIDIA/NeMo-Speech.cpp.git"
RUNTIME_REVISION = "1118951337094db3b362fbf1b27e871696f10590"
RUNTIME_LICENSE = "Apache-2.0"


def default_asr_root() -> Path:
    configured = os.environ.get("LOCALAPPDATA", "").strip()
    if configured:
        return Path(configured).expanduser() / "BSideOliviaLocal" / "asr"
    return Path.home() / "AppData" / "Local" / "BSideOliviaLocal" / "asr"


def _default_root(name: str) -> Path:
    return default_asr_root() / name


def is_local_absolute_path(path: Path | str) -> bool:
    windows = PureWindowsPath(str(path))
    return windows.is_absolute() and len(windows.drive) == 2 and windows.drive[1] == ":"


@dataclass(frozen=True)
class AsrConfig:
    provider: str = "text-fallback"
    server_url: str = "ws://127.0.0.1:8080"
    language: str = "auto"
    sample_rate: int = 16_000
    chunk_ms: int = 160
    max_queue_chunks: int = 32
    backpressure_timeout_ms: int = 100
    final_timeout_ms: int = 2_000
    silence_rms: float = 0.005
    endpointing_ms: int = 600
    word_timestamps: bool = False
    connect_timeout_ms: int = 3_000
    runtime_root: Path = field(default_factory=lambda: _default_root("runtime"))
    model_root: Path = field(default_factory=lambda: _default_root("models"))
    cache_root: Path = field(default_factory=lambda: _default_root("cache"))
    runtime_executable: Path | None = None
    model_path: Path | None = None
    strict_storage: bool = True

    def __post_init__(self) -> None:
        if self.provider not in {"text-fallback", "nemotron-speech-cpp"}:
            raise AsrError("ASR_CONFIG_INVALID", f"unsupported provider: {self.provider}")
        parsed = urlparse(self.server_url)
        if parsed.scheme not in {"ws", "wss"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise AsrError("ASR_CONFIG_INVALID", "ASR server must be loopback WebSocket")
        if self.language != "auto" and not self.language.strip():
            raise AsrError("ASR_CONFIG_INVALID", "language cannot be empty")
        if self.sample_rate <= 0 or self.chunk_ms not in {80, 160, 320, 560, 1120}:
            raise AsrError("ASR_CONFIG_INVALID", "sample_rate or chunk_ms is invalid")
        if self.max_queue_chunks < 1 or self.backpressure_timeout_ms < 0:
            raise AsrError("ASR_CONFIG_INVALID", "queue/backpressure settings are invalid")
        if self.final_timeout_ms < 1 or self.endpointing_ms < 0 or self.connect_timeout_ms < 1:
            raise AsrError("ASR_CONFIG_INVALID", "timeout settings are invalid")
        if not 0 <= self.silence_rms <= 1:
            raise AsrError("ASR_CONFIG_INVALID", "silence_rms must be between 0 and 1")
        if self.strict_storage:
            self.validate_storage_roots()

    def validate_storage_roots(self) -> None:
        paths = [self.runtime_root, self.model_root, self.cache_root]
        if self.runtime_executable is not None:
            paths.append(self.runtime_executable)
        if self.model_path is not None:
            paths.append(self.model_path)
        for path in paths:
            path = Path(path)
            if not is_local_absolute_path(path):
                raise AsrError(
                    "ASR_CONFIG_INVALID",
                    "runtime, model, and cache paths must be absolute local Windows paths",
                    {"path": str(path)},
                )

    @property
    def effective_model_path(self) -> Path:
        return self.model_path or self.model_root / MODEL_FILENAME

    @property
    def effective_runtime_executable(self) -> Path:
        return self.runtime_executable or self.runtime_root / "nemo-speech.exe"

    @property
    def acceptance_manifest(self) -> Path:
        return self.runtime_root.parent / "evidence" / "native_acceptance.json"

    def with_test_paths(self, root: Path) -> "AsrConfig":
        """Return a config for offline tests without weakening production defaults."""

        root = Path(root).absolute()
        return replace(
            self,
            runtime_root=root / "runtime",
            model_root=root / "models",
            cache_root=root / "cache",
            model_path=root / "models" / MODEL_FILENAME,
            strict_storage=False,
        )

    def to_dict(self, *, include_paths: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "provider": self.provider,
            "server_url": self.server_url,
            "language": self.language,
            "sample_rate": self.sample_rate,
            "chunk_ms": self.chunk_ms,
            "max_queue_chunks": self.max_queue_chunks,
            "backpressure_timeout_ms": self.backpressure_timeout_ms,
            "final_timeout_ms": self.final_timeout_ms,
            "silence_rms": self.silence_rms,
            "endpointing_ms": self.endpointing_ms,
            "word_timestamps": self.word_timestamps,
            "connect_timeout_ms": self.connect_timeout_ms,
            "model_repo": MODEL_REPO,
            "model_revision": MODEL_REVISION,
            "model_license": MODEL_LICENSE,
            "runtime_repo": RUNTIME_REPO,
            "runtime_revision": RUNTIME_REVISION,
            "runtime_license": RUNTIME_LICENSE,
        }
        if include_paths:
            result.update(
                {
                    "runtime_root": str(self.runtime_root),
                    "model_root": str(self.model_root),
                    "cache_root": str(self.cache_root),
                    "runtime_executable": str(self.runtime_executable) if self.runtime_executable else None,
                    "model_path": str(self.effective_model_path),
                    "acceptance_manifest": str(self.acceptance_manifest),
                }
            )
        return result

    @classmethod
    def from_mapping(cls, values: dict[str, object]) -> "AsrConfig":
        fields = {
            "provider",
            "server_url",
            "language",
            "sample_rate",
            "chunk_ms",
            "max_queue_chunks",
            "backpressure_timeout_ms",
            "final_timeout_ms",
            "silence_rms",
            "endpointing_ms",
            "word_timestamps",
            "connect_timeout_ms",
            "runtime_root",
            "model_root",
            "cache_root",
            "runtime_executable",
            "model_path",
            "strict_storage",
        }
        kwargs: dict[str, object] = {}
        for key, value in values.items():
            if key in fields:
                kwargs[key] = value
        for key in ("runtime_root", "model_root", "cache_root", "runtime_executable", "model_path"):
            if key in kwargs and kwargs[key] is not None:
                kwargs[key] = Path(str(kwargs[key]))
        return cls(**kwargs)

    @classmethod
    def from_json(cls, path: Path) -> "AsrConfig":
        return cls.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "AsrConfig":
        env = os.environ if environ is None else environ
        values: dict[str, object] = {}
        mapping: dict[str, str] = {
            "ASR_PROVIDER": "provider",
            "ASR_SERVER_URL": "server_url",
            "ASR_LANGUAGE": "language",
            "ASR_RUNTIME_ROOT": "runtime_root",
            "ASR_MODEL_ROOT": "model_root",
            "ASR_CACHE_ROOT": "cache_root",
            "ASR_RUNTIME_EXECUTABLE": "runtime_executable",
            "ASR_MODEL_PATH": "model_path",
        }
        for env_key, config_key in mapping.items():
            if env_key in env:
                values[config_key] = env[env_key]
        for env_key, config_key, caster in (
            ("ASR_SAMPLE_RATE", "sample_rate", int),
            ("ASR_CHUNK_MS", "chunk_ms", int),
            ("ASR_MAX_QUEUE_CHUNKS", "max_queue_chunks", int),
            ("ASR_BACKPRESSURE_TIMEOUT_MS", "backpressure_timeout_ms", int),
            ("ASR_FINAL_TIMEOUT_MS", "final_timeout_ms", int),
            ("ASR_SILENCE_RMS", "silence_rms", float),
            ("ASR_ENDPOINTING_MS", "endpointing_ms", int),
            ("ASR_CONNECT_TIMEOUT_MS", "connect_timeout_ms", int),
        ):
            if env_key in env:
                values[config_key] = caster(env[env_key])
        return cls.from_mapping(values)
