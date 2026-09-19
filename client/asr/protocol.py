"""Offline parser for NVIDIA NeMo-Speech.cpp's documented WebSocket events."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
import math
from typing import Any

from .contracts import AsrEvent, EventClock


class NemotronProtocolAdapter:
    """Convert documented server JSON events to the B05 event contract.

    This adapter has no transport and no inference implementation.  It is safe
    to exercise in offline tests with captured protocol-shaped JSON, while the
    production provider remains responsible for a real local WebSocket.
    """

    provider_name = "nemotron-speech-cpp"

    def __init__(self, clock: EventClock | None = None, requested_language: str = "auto") -> None:
        self.clock = clock or EventClock()
        self.requested_language = requested_language
        self._partials: dict[str, str] = defaultdict(str)
        self.server_language: str | None = None

    def _emit(self, event_type: str, **kwargs: Any) -> AsrEvent:
        return self.clock.emit(event_type, provider=self.provider_name, **kwargs)

    @staticmethod
    def _native_audio_ms(payload: Mapping[str, Any]) -> float | None:
        """Return NeMo's processed-audio position when the server supplies it."""

        value = payload.get("audio_processed")
        if isinstance(value, (int, float)) and math.isfinite(float(value)) and value >= 0:
            return round(float(value) * 1000, 3)
        return None

    @staticmethod
    def _native_word_timestamps(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Expose validated native word timing without copying raw audio."""

        raw_words = payload.get("words")
        if not isinstance(raw_words, list):
            return []
        result: list[dict[str, Any]] = []
        for raw_word in raw_words:
            if not isinstance(raw_word, Mapping):
                continue
            start = raw_word.get("start")
            end = raw_word.get("end")
            word = raw_word.get("word")
            if (
                not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or not math.isfinite(float(start))
                or not math.isfinite(float(end))
                or start < 0
                or end < start
                or not isinstance(word, str)
            ):
                continue
            item: dict[str, Any] = {
                "start_ms": round(float(start) * 1000, 3),
                "end_ms": round(float(end) * 1000, 3),
                "word": word,
            }
            confidence = raw_word.get("confidence")
            if isinstance(confidence, (int, float)) and math.isfinite(float(confidence)):
                item["confidence"] = float(confidence)
            result.append(item)
        return result

    def _timing_metadata(self, payload: Mapping[str, Any]) -> tuple[float, dict[str, Any]]:
        audio_ms = self._native_audio_ms(payload)
        metadata: dict[str, Any] = {}
        if audio_ms is not None:
            metadata.update(
                {
                    "audio_timestamp_source": "native_audio_processed",
                    "audio_processed_ms": audio_ms,
                }
            )
        word_timestamps = self._native_word_timestamps(payload)
        if word_timestamps:
            metadata["word_timestamps"] = word_timestamps
        return audio_ms or 0.0, metadata

    def ingest(self, payload: Mapping[str, Any]) -> AsrEvent:
        if not isinstance(payload, Mapping):
            return self._emit(
                "error",
                code="ASR_PROTOCOL_ERROR",
                reason="server event is not a JSON object",
            )

        event_name = payload.get("type")
        if not isinstance(event_name, str):
            return self._emit("error", code="ASR_PROTOCOL_ERROR", reason="server event has no type")

        if event_name == "session.created":
            session = payload.get("session")
            if not isinstance(session, Mapping):
                session = {}
            language = session.get("language")
            if isinstance(language, str) and language:
                self.server_language = language
            return self._emit(
                "session",
                language=self.server_language,
                metadata={"server_event": event_name},
            )

        if event_name == "session.updated":
            session = payload.get("session")
            if not isinstance(session, Mapping):
                session = {}
            language = session.get("language")
            if isinstance(language, str) and language:
                self.server_language = language
            return self._emit(
                "ready",
                language=self.server_language,
                metadata={"server_event": event_name, "requested_language": self.requested_language},
            )

        if event_name == "conversation.item.input_audio_transcription.delta":
            item_id = str(payload.get("item_id") or "default")
            delta = payload.get("delta", "")
            if not isinstance(delta, str):
                delta = str(delta)
            self._partials[item_id] += delta
            audio_ms, timing_metadata = self._timing_metadata(payload)
            return self._emit(
                "partial",
                audio_ms=audio_ms,
                text=self._partials[item_id],
                language=self.server_language,
                metadata={
                    "server_event": event_name,
                    "delta": delta,
                    "item_id": item_id,
                    **timing_metadata,
                },
            )

        if event_name == "conversation.item.input_audio_transcription.completed":
            item_id = str(payload.get("item_id") or "default")
            text = payload.get("transcript", "")
            if not isinstance(text, str):
                text = str(text)
            self._partials.pop(item_id, None)
            audio_ms, timing_metadata = self._timing_metadata(payload)
            if not text.strip():
                return self._emit(
                    "silence",
                    audio_ms=audio_ms,
                    language=self.server_language,
                    metadata={"server_event": event_name, "item_id": item_id, **timing_metadata},
                )
            return self._emit(
                "final",
                audio_ms=audio_ms,
                text=text,
                language=self.server_language,
                metadata={"server_event": event_name, "item_id": item_id, **timing_metadata},
            )

        if event_name == "input_audio_buffer.committed":
            return self._emit("committed", metadata={"server_event": event_name})

        if event_name == "input_audio_buffer.cleared":
            self._partials.clear()
            return self._emit("cleared", metadata={"server_event": event_name})

        if event_name == "error":
            error = payload.get("error")
            if not isinstance(error, Mapping):
                error = {}
            code = str(error.get("code") or "ASR_PROVIDER_UNAVAILABLE")
            reason = str(error.get("message") or error.get("reason") or "provider error")
            return self._emit(
                "error",
                code=code if code.startswith("ASR_") else "ASR_PROVIDER_UNAVAILABLE",
                reason=reason,
                language=self.server_language,
                metadata={"server_event": event_name},
            )

        return self._emit(
            "error",
            code="ASR_PROTOCOL_ERROR",
            reason=f"unsupported server event: {event_name}",
            metadata={"server_event": event_name},
        )
