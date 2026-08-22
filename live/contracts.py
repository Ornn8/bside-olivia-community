"""Public B08 event, state, error, and result contracts."""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping


class LiveSessionState(str, Enum):
    CREATED = "created"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    RECONNECTING = "reconnecting"
    CANCELED = "canceled"
    FAILED = "failed"
    CLOSED = "closed"


LIVE_ERROR_CODES = frozenset(
    {
        "LIVE_INVALID_INPUT",
        "LIVE_SESSION_NOT_FOUND",
        "LIVE_SESSION_FORBIDDEN",
        "LIVE_SESSION_CLOSED",
        "LIVE_BACKPRESSURE",
        "LIVE_TIMEOUT",
        "LIVE_CANCELED",
        "LIVE_INTERRUPTED",
        "LIVE_LLM_UNAVAILABLE",
        "LIVE_LLM_ERROR",
        "ASR_UNAVAILABLE",
        "ASR_DISCONNECTED",
        "ASR_BACKPRESSURE",
        "ASR_INVALID_AUDIO",
        "MEMORY_UNAVAILABLE",
        "TTS_UNAVAILABLE",
        "VISUAL_UNAVAILABLE",
        "IDEMPOTENCY_CONFLICT",
    }
)


class LiveError(RuntimeError):
    """Sanitized error crossing the Live session boundary."""

    def __init__(self, code: str, message: str = "", *, retryable: bool = False) -> None:
        if code not in LIVE_ERROR_CODES:
            raise ValueError(f"unknown live error code: {code}")
        self.code = code
        self.retryable = retryable
        super().__init__(message or code)


@dataclass(frozen=True)
class LiveConfig:
    max_events: int = 128
    turn_timeout_seconds: float = 30.0
    event_backpressure_timeout_ms: int = 50
    reconnect_attempts: int = 1
    max_history_turns: int = 12
    max_input_chars: int = 10000
    safe_unavailable_text: str = "当前文字服务暂不可用，请稍后再试。"

    def __post_init__(self) -> None:
        if self.max_events < 1 or self.turn_timeout_seconds <= 0:
            raise ValueError("invalid live queue or timeout setting")
        if self.event_backpressure_timeout_ms < 0 or self.reconnect_attempts < 0:
            raise ValueError("invalid live backpressure or reconnect setting")
        if self.max_history_turns < 0 or self.max_input_chars < 1:
            raise ValueError("invalid live history or input setting")


@dataclass(frozen=True)
class LiveEvent:
    session_id: str
    sequence: int
    timestamp_ms: float
    event: str
    state: LiveSessionState
    turn_id: str | None = None
    component: str | None = None
    status: str | None = None
    error_code: str | None = None
    text_present: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the public event contract without user/model text."""

        return {
            "session_id": self.session_id,
            "sequence": self.sequence,
            "timestamp_ms": round(float(self.timestamp_ms), 3),
            "event": self.event,
            "state": self.state.value,
            "turn_id": self.turn_id,
            "component": self.component,
            "status": self.status,
            "error_code": self.error_code,
            "metadata": dict(self.metadata),
            "text_present": self.text_present,
        }

    def trace_dict(self) -> dict[str, Any]:
        """Return a replay-safe record without user/model text or audio."""

        result = self.to_dict()
        _assert_trace_safe(result, "trace")
        return result


@dataclass(frozen=True)
class LiveTurnResult:
    turn_id: str
    status: str
    text: str = ""
    text_source: str = "none"
    error_code: str | None = None
    retryable: bool = False
    memory_status: str = "session-only"
    tts_status: str = "not_started"
    visual_status: str = "not_started"
    audio_chunks: int = 0
    visual_frames: int = 0
    latency_ms: float = 0.0

    @property
    def completed(self) -> bool:
        return self.status == "completed"


@dataclass
class LiveTurnHandle:
    """A cancellable turn handle returned by ``submit_text``/audio methods."""

    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    kind: str = "text"
    fingerprint: str = ""
    task: Any = field(default=None, repr=False)
    result_future: Any = field(default=None, repr=False)
    _cancel_requested: bool = field(default=False, init=False, repr=False)

    def cancel_requested(self) -> bool:
        return self._cancel_requested

    def request_cancel(self) -> bool:
        if self.result_future is not None and self.result_future.done():
            return False
        self._cancel_requested = True
        if self.task is not None and not self.task.done():
            self.task.cancel()
        return True

    async def wait(self) -> LiveTurnResult:
        if self.result_future is None:
            raise RuntimeError("turn has not been started")
        return await self.result_future


def fingerprint_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def monotonic_ms(start: float) -> float:
    return (time.monotonic() - start) * 1000.0


_TRACE_FORBIDDEN_KEYS = frozenset(
    {
        "text",
        "owner",
        "owner_id",
        "audio",
        "audio_payload",
        "pcm",
        "pcm16",
        "samples",
        "waveform",
        "frame",
        "frame_payload",
        "pixels",
        "image",
        "video",
        "raw_audio",
        "raw_frame",
        "payload",
    }
)
_TRACE_ALLOWED_METADATA_KEYS = frozenset(
    {
        "attempt",
        "chunk_index",
        "dropped_events",
        "fallback_reason",
        "media_written",
        "model_generated",
        "provider",
        "sample_count",
        "sample_rate",
        "sentence_index",
        "source",
    }
)
_TRACE_ALLOWED_ROOT_KEYS = frozenset(
    {
        "session_id",
        "sequence",
        "timestamp_ms",
        "event",
        "state",
        "turn_id",
        "component",
        "status",
        "error_code",
        "metadata",
        "text_present",
    }
)
_TRACE_PRIVATE_PATH = re.compile(r"^(?:[a-z]:[\\/]|\\\\|file://)", re.IGNORECASE)
_TRACE_FORBIDDEN_KEY_PARTS = (
    "api_key",
    "audio",
    "authorization",
    "credential",
    "frame",
    "image",
    "owner",
    "password",
    "path",
    "payload",
    "pcm",
    "pixel",
    "prompt",
    "raw",
    "secret",
    "token",
    "video",
)


def _normalize_trace_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")


def _assert_trace_safe(value: Any, path: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized_key = _normalize_trace_key(key)
            root_record = path == "trace" or (path.startswith("trace[") and "." not in path)
            if root_record and normalized_key not in _TRACE_ALLOWED_ROOT_KEYS:
                raise ValueError(f"trace root field is not allowlisted: {path}.{key}")
            if path.endswith(".metadata") and normalized_key not in _TRACE_ALLOWED_METADATA_KEYS:
                raise ValueError(f"trace metadata field is not allowlisted: {path}.{key}")
            if (
                normalized_key in _TRACE_FORBIDDEN_KEYS
                or normalized_key.endswith("_payload")
                or normalized_key != "text_present"
                and any(part in normalized_key for part in _TRACE_FORBIDDEN_KEY_PARTS)
            ):
                raise ValueError(f"trace contains forbidden field: {path}.{key}")
            _assert_trace_safe(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_trace_safe(child, f"{path}[{index}]")
    elif isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError(f"trace contains binary payload: {path}")
    elif isinstance(value, str) and _TRACE_PRIVATE_PATH.match(value.strip()):
        raise ValueError(f"trace contains private path: {path}")


def replay_trace(trace: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Validate and normalize the redacted event timeline for deterministic replay."""

    records: list[dict[str, Any]] = []
    previous_sequence = -1
    previous_timestamp = float("-inf")
    for index, raw in enumerate(trace):
        if not isinstance(raw, Mapping):
            raise ValueError(f"trace record {index} is not an object")
        if "text" in raw:
            raise ValueError(f"trace record {index} contains raw text")
        _assert_trace_safe(raw, f"trace[{index}]")
        sequence = raw.get("sequence")
        timestamp_ms = raw.get("timestamp_ms")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= previous_sequence:
            raise ValueError("trace sequence must be strictly increasing integers")
        if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, (int, float)):
            raise ValueError("trace timestamp must be numeric")
        timestamp = float(timestamp_ms)
        if timestamp < previous_timestamp:
            raise ValueError("trace timestamps must be monotonic")
        if not isinstance(raw.get("session_id"), str) or not raw["session_id"]:
            raise ValueError("trace session_id is required")
        if not isinstance(raw.get("event"), str) or not raw["event"]:
            raise ValueError("trace event is required")
        if not isinstance(raw.get("text_present"), bool):
            raise ValueError("trace text_present is required")
        record = dict(raw)
        record["sequence"] = sequence
        record["timestamp_ms"] = round(timestamp, 3)
        record["metadata"] = dict(raw.get("metadata") or {})
        records.append(record)
        previous_sequence = sequence
        previous_timestamp = timestamp
    return tuple(records)


__all__ = [
    "LIVE_ERROR_CODES",
    "LiveConfig",
    "LiveError",
    "LiveEvent",
    "LiveSessionState",
    "LiveTurnHandle",
    "LiveTurnResult",
    "fingerprint_text",
    "replay_trace",
]
