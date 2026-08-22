"""Always-available text-input fallback, explicitly separate from ASR."""

from __future__ import annotations

from collections.abc import AsyncIterator

from .contracts import AsrEvent, EventClock


class TextFallbackProvider:
    provider_name = "text-fallback"

    def status(self) -> dict[str, object]:
        return {
            "provider": self.provider_name,
            "status": "available",
            "ready": True,
            "language": None,
            "reason": "TEXT_INPUT_FALLBACK",
            "is_asr": False,
        }

    async def stream_text(self, text: str, *, language: str | None = None) -> AsyncIterator[AsrEvent]:
        clock = EventClock()
        yield clock.emit(
            "session",
            provider=self.provider_name,
            language=language,
            metadata={"source": "text-fallback", "is_asr": False},
        )
        yield clock.emit(
            "ready",
            provider=self.provider_name,
            language=language,
            metadata={"source": "text-fallback", "is_asr": False},
        )
        if text.strip():
            yield clock.emit(
                "final",
                provider=self.provider_name,
                text=text,
                language=language,
                metadata={"source": "text-fallback", "is_asr": False},
            )
        else:
            yield clock.emit(
                "silence",
                provider=self.provider_name,
                language=language,
                metadata={"source": "text-fallback", "is_asr": False},
            )
        yield clock.emit(
            "closed",
            provider=self.provider_name,
            language=language,
            metadata={"source": "text-fallback", "is_asr": False},
        )
