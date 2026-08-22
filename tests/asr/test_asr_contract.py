from __future__ import annotations

import struct

import pytest

from asr.contracts import AsrEvent, EventClock, assert_monotonic_timestamps, pcm16_rms


def test_event_clock_is_monotonic_and_serializable() -> None:
    clock = EventClock(session_id="test-session", start_ns=0)
    events = [clock.emit("session"), clock.emit("ready"), clock.emit("partial", text="hello")]

    assert [event.sequence for event in events] == [0, 1, 2]
    assert_monotonic_timestamps(events)
    assert events[-1].to_dict()["session_id"] == "test-session"


def test_event_rejects_invalid_error_and_timestamp() -> None:
    with pytest.raises(ValueError, match="error events require code"):
        AsrEvent("error", "session", 0, 0.0)
    with pytest.raises(ValueError, match="finite non-negative"):
        AsrEvent("ready", "session", 0, -1.0)


def test_pcm16_rms_and_odd_payload() -> None:
    pcm = struct.pack("<hhhh", 0, 32767, -32768, 0)
    assert 0.70 < pcm16_rms(pcm) < 0.71
    with pytest.raises(ValueError, match="even number"):
        pcm16_rms(b"\x00")
