from __future__ import annotations

import asyncio

from asr.contracts import assert_monotonic_timestamps
from asr.fallback import TextFallbackProvider


def test_text_fallback_is_available_and_not_claimed_as_asr() -> None:
    provider = TextFallbackProvider()
    assert provider.status() == {
        "provider": "text-fallback",
        "status": "available",
        "ready": True,
        "language": None,
        "reason": "TEXT_INPUT_FALLBACK",
        "is_asr": False,
    }

    async def collect():
        return [event async for event in provider.stream_text("hello")]

    events = asyncio.run(collect())
    assert [event.type for event in events] == ["session", "ready", "final", "closed"]
    assert events[2].text == "hello"
    assert events[2].metadata["is_asr"] is False
    assert_monotonic_timestamps(events)


def test_text_fallback_empty_input_is_silence() -> None:
    async def collect():
        return [event async for event in TextFallbackProvider().stream_text(" ")]

    events = asyncio.run(collect())
    assert [event.type for event in events] == ["session", "ready", "silence", "closed"]
