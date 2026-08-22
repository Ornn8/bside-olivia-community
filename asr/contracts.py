"""Provider-neutral streaming events and audio helpers."""

from __future__ import annotations

import math
import struct
import time
import uuid
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


EVENT_TYPES = {
    "session",
    "ready",
    "partial",
    "final",
    "silence",
    "committed",
    "cleared",
    "canceled",
    "disconnected",
    "error",
    "closed",
    "text_final",
}


@dataclass(frozen=True)
class AsrEvent:
    """A normalized event emitted by either a real provider or text fallback.

    ``timestamp_ms`` and ``audio_ms`` are session-relative.  A provider may
    attach non-sensitive diagnostics in ``metadata`` but must not put raw audio
    or credentials there.
    """

    type: str
    session_id: str
    sequence: int
    timestamp_ms: float
    audio_ms: float = 0.0
    text: str = ""
    language: str | None = None
    provider: str = "unknown"
    code: str | None = None
    reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError(f"unknown ASR event type: {self.type}")
        if not self.session_id:
            raise ValueError("session_id is required")
        if not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("sequence must be a non-negative integer")
        for name, value in (("timestamp_ms", self.timestamp_ms), ("audio_ms", self.audio_ms)):
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")
        if self.type == "error" and not self.code:
            raise ValueError("error events require code")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "timestamp_ms": round(float(self.timestamp_ms), 3),
            "audio_ms": round(float(self.audio_ms), 3),
            "text": self.text,
            "language": self.language,
            "provider": self.provider,
            "code": self.code,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


class EventClock:
    """Creates session-relative events with monotonic timestamps."""

    def __init__(self, session_id: str | None = None, start_ns: int | None = None) -> None:
        self.session_id = session_id or uuid.uuid4().hex
        self._start_ns = start_ns if start_ns is not None else time.monotonic_ns()
        self._sequence = -1
        self._last_timestamp_ms = -0.001

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def last_timestamp_ms(self) -> float:
        return max(0.0, self._last_timestamp_ms)

    def now_ms(self) -> float:
        elapsed = (time.monotonic_ns() - self._start_ns) / 1_000_000
        if elapsed <= self._last_timestamp_ms:
            elapsed = self._last_timestamp_ms + 0.001
        self._last_timestamp_ms = elapsed
        return elapsed

    def emit(
        self,
        event_type: str,
        *,
        audio_ms: float = 0.0,
        text: str = "",
        language: str | None = None,
        provider: str = "unknown",
        code: str | None = None,
        reason: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AsrEvent:
        self._sequence += 1
        return AsrEvent(
            type=event_type,
            session_id=self.session_id,
            sequence=self._sequence,
            timestamp_ms=self.now_ms(),
            audio_ms=audio_ms,
            text=text,
            language=language,
            provider=provider,
            code=code,
            reason=reason,
            metadata=metadata or {},
        )


def pcm16_rms(pcm: bytes) -> float:
    """Return normalized RMS for little-endian signed PCM16 bytes."""

    if len(pcm) % 2:
        raise ValueError("PCM16 payload must contain an even number of bytes")
    if not pcm:
        return 0.0
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768.0


def assert_monotonic_timestamps(events: list[AsrEvent] | tuple[AsrEvent, ...]) -> None:
    previous_timestamp = -1.0
    previous_sequence = -1
    for event in events:
        if event.sequence <= previous_sequence:
            raise AssertionError("ASR event sequence is not strictly increasing")
        if event.timestamp_ms < previous_timestamp:
            raise AssertionError("ASR event timestamp is not monotonic")
        previous_sequence = event.sequence
        previous_timestamp = event.timestamp_ms
