from __future__ import annotations

from asr.contracts import EventClock
from asr.metrics import cer, measure_events, normalize_transcript, wer


def test_transcript_normalization_and_error_rates() -> None:
    assert normalize_transcript(" Héllo,  WORLD! ") == "héllo world"
    assert wer("hello world", "hello word") == 0.5
    assert cer("abc", "adc") == 1 / 3


def test_streaming_latency_stability_and_monotonicity() -> None:
    clock = EventClock(session_id="metrics", start_ns=0)
    events = [
        clock.emit("session"),
        clock.emit("partial", text="hello"),
        clock.emit("partial", text="hello wor"),
        clock.emit("final", text="hello world", audio_ms=640),
    ]
    metrics = measure_events(events)
    assert metrics["partial_count"] == 2
    assert metrics["final_count"] == 1
    assert metrics["first_partial_latency_ms"] is not None
    assert metrics["first_final_latency_ms"] is not None
    assert metrics["partial_to_final_stability"] == 1.0
    assert metrics["timestamp_monotonic"] is True
    assert metrics["last_audio_ms"] == 640
