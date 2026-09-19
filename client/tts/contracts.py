"""Small, dependency-light contracts for the independent B06 TTS tranche."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


class TTSError(RuntimeError):
    """Sanitized, machine-readable TTS failure."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


class TTSUnavailable(TTSError):
    """The selected provider is not available in the current local profile."""

    def __init__(self, code: str = "TTS_UNAVAILABLE", message: str = "") -> None:
        super().__init__(code, message)


class TTSCancelled(TTSError):
    """A synthesis request was explicitly cancelled by its owner."""

    def __init__(self) -> None:
        super().__init__("TTS_CANCELLED")


class TTSValidationError(TTSError):
    """The caller supplied an invalid sentence or profile value."""

    def __init__(self, code: str = "TTS_INVALID_INPUT", message: str = "") -> None:
        super().__init__(code, message)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _bounded_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))


def _path_state(value: str) -> dict[str, bool]:
    if not value:
        return {"configured": False, "exists": False}
    path = Path(value)
    return {"configured": True, "exists": path.exists()}


def _public_provider_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Expose provider switches without echoing private cache/model paths."""

    path_keys = {
        "model_license_path",
        "numba_cache_dir",
        "quality_gate_python",
        "quality_gate_cache_root",
        "temp_root",
        "wetext_fst_root",
    }
    result: dict[str, Any] = {}
    for key, value in options.items():
        normalized = str(key)
        if normalized in path_keys:
            path = Path(str(value)) if value else None
            result[normalized] = {
                "configured": bool(value),
                "exists": bool(path and path.exists()),
            }
        else:
            result[normalized] = value
    return result


@dataclass(frozen=True)
class TTSConfig:
    """A local profile; it contains paths but public views never echo them."""

    profile: str = "cosyvoice3-live"
    provider: str = "cosyvoice3"
    enabled: bool = True
    runtime_root: str = ""
    model_dir: str = ""
    reference_audio: str = ""
    reference_text: str = ""
    language: str = "zh"
    license_id: str = "Apache-2.0"
    fallback: str = "text"
    speed: float = 1.0
    leading_trim_seconds: float = 0.0
    max_input_chars: int = 12000
    fp16: bool = True
    provider_options: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "TTSConfig":
        data = dict(raw or {})
        provider = str(data.get("provider", "cosyvoice3") or "cosyvoice3").strip().lower()
        fallback = str(data.get("fallback", "text") or "text").strip().lower()
        if fallback not in {"text", "unavailable"}:
            raise TTSValidationError("TTS_INVALID_FALLBACK", "fallback must be text or unavailable")
        options = data.get("provider_options", {})
        if not isinstance(options, Mapping):
            options = {}
        return cls(
            profile=str(data.get("profile", "cosyvoice3-live") or "cosyvoice3-live").strip(),
            provider=provider,
            enabled=_as_bool(data.get("enabled", True), True),
            runtime_root=str(data.get("runtime_root", "") or "").strip(),
            model_dir=str(data.get("model_dir", "") or "").strip(),
            reference_audio=str(data.get("reference_audio", "") or "").strip(),
            reference_text=str(data.get("reference_text", "") or "").strip(),
            language=str(data.get("language", "zh") or "zh").strip(),
            license_id=str(data.get("license_id", "Apache-2.0") or "Apache-2.0").strip(),
            fallback=fallback,
            speed=_bounded_float(data.get("speed", 1.0), 1.0, 0.5, 2.0),
            leading_trim_seconds=_bounded_float(
                data.get("leading_trim_seconds", 0.0), 0.0, 0.0, 30.0
            ),
            max_input_chars=_bounded_int(data.get("max_input_chars", 12000), 12000, 1, 100000),
            fp16=_as_bool(data.get("fp16", True), True),
            provider_options=dict(options),
        )

    def public_dict(self) -> dict[str, Any]:
        """Return status metadata without private paths or input text."""

        return {
            "profile": self.profile,
            "provider": self.provider,
            "enabled": self.enabled,
            "runtime_root": _path_state(self.runtime_root),
            "model_dir": _path_state(self.model_dir),
            "reference_audio": _path_state(self.reference_audio),
            "reference_text_configured": bool(self.reference_text),
            "language": self.language,
            "license_id": self.license_id,
            "fallback": self.fallback,
            "speed": self.speed,
            "leading_trim_seconds": self.leading_trim_seconds,
            "max_input_chars": self.max_input_chars,
            "fp16": self.fp16,
            "provider_options": _public_provider_options(self.provider_options),
        }


@dataclass
class TTSRequest:
    """One text request.  The service splits it into sentence units."""

    text: str
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    stream: bool = True
    language: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    started_at: float = field(default_factory=time.perf_counter, init=False)

    def validate(self, max_input_chars: int) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise TTSValidationError("TTS_EMPTY_INPUT", "text is required")
        if len(self.text) > max_input_chars:
            raise TTSValidationError("TTS_INPUT_TOO_LONG", "text exceeds the profile limit")

    def cancel(self) -> None:
        self.cancel_event.set()


@dataclass(frozen=True)
class AudioChunk:
    """One mono float audio chunk emitted inside one sentence."""

    samples: Sequence[float]
    sample_rate: int
    sentence_index: int
    chunk_index: int
    emitted_at: float = field(default_factory=time.perf_counter)

    @property
    def sample_count(self) -> int:
        return len(self.samples)


@dataclass(frozen=True)
class TTSResult:
    request_id: str
    status: str
    provider: str
    sentence_count: int = 0
    chunk_count: int = 0
    sample_rate: int | None = None
    sample_count: int = 0
    duration_seconds: float = 0.0
    first_audio_ms: float | None = None
    ended_ms: float | None = None
    output_path: str | None = None
    fallback_text: str | None = None
    error_code: str | None = None

    @property
    def completed(self) -> bool:
        return self.status == "completed"


@dataclass(frozen=True)
class TTSStreamEvent:
    request_id: str
    event: str
    timestamp_ms: float
    chunk: AudioChunk | None = None
    result: TTSResult | None = None
    error_code: str | None = None


class TTSRun:
    """Cancellable event stream wrapper used by tests and local callers."""

    def __init__(self, request: TTSRequest, task: asyncio.Task[TTSResult] | None = None) -> None:
        self.request = request
        self.task = task
        self.queue: asyncio.Queue[TTSStreamEvent] = asyncio.Queue(maxsize=128)
        self._result: asyncio.Future[TTSResult] = asyncio.get_running_loop().create_future()

    def cancel(self) -> bool:
        if self.task is None or self.task.done():
            return False
        self.request.cancel()
        return True

    async def wait(self) -> TTSResult:
        return await self._result

    async def events(self):
        while True:
            event = await self.queue.get()
            yield event
            if event.event in {"completed", "cancelled", "unavailable", "text_fallback", "failed"}:
                return
