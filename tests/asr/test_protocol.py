from __future__ import annotations

from asr.contracts import EventClock, assert_monotonic_timestamps
from asr.protocol import NemotronProtocolAdapter


def test_offline_adapter_maps_documented_session_partial_final() -> None:
    adapter = NemotronProtocolAdapter(EventClock(session_id="adapter"), requested_language="auto")
    events = [
        adapter.ingest({"type": "session.created", "session": {"id": "s", "language": "en-US"}}),
        adapter.ingest({"type": "session.updated", "session": {"language": "en-US"}}),
        adapter.ingest(
            {
                "type": "conversation.item.input_audio_transcription.delta",
                "item_id": "i",
                "delta": "hello",
            }
        ),
        adapter.ingest(
            {
                "type": "conversation.item.input_audio_transcription.delta",
                "item_id": "i",
                "delta": " world",
            }
        ),
        adapter.ingest(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "i",
                "transcript": "hello world",
            }
        ),
    ]

    assert [event.type for event in events] == ["session", "ready", "partial", "partial", "final"]
    assert events[2].text == "hello"
    assert events[3].text == "hello world"
    assert events[-1].text == "hello world"
    assert all(event.language == "en-US" for event in events)
    assert_monotonic_timestamps(events)


def test_offline_adapter_maps_silence_clear_and_protocol_error() -> None:
    adapter = NemotronProtocolAdapter(EventClock(session_id="adapter"))
    silence = adapter.ingest(
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": ""}
    )
    cleared = adapter.ingest({"type": "input_audio_buffer.cleared"})
    unknown = adapter.ingest({"type": "server.new_event"})

    assert silence.type == "silence"
    assert cleared.type == "cleared"
    assert unknown.type == "error"
    assert unknown.code == "ASR_PROTOCOL_ERROR"


def test_provider_error_code_is_safely_normalized() -> None:
    adapter = NemotronProtocolAdapter(EventClock(session_id="adapter"))
    event = adapter.ingest({"type": "error", "error": {"code": "ECONNRESET", "message": "closed"}})
    assert event.type == "error"
    assert event.code == "ASR_PROVIDER_UNAVAILABLE"
    assert event.reason == "closed"


def test_native_audio_progress_and_word_timestamps_are_exposed_for_av_sync() -> None:
    adapter = NemotronProtocolAdapter(EventClock(session_id="timing"))
    partial = adapter.ingest(
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "delta": "hello",
            "audio_processed": 1.2,
        }
    )
    final = adapter.ingest(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "hello",
            "audio_processed": 2.4,
            "words": [{"start": 1.2, "end": 2.4, "word": "hello", "confidence": 0.9}],
        }
    )

    assert partial.audio_ms == 1200.0
    assert partial.metadata["audio_timestamp_source"] == "native_audio_processed"
    assert final.audio_ms == 2400.0
    assert final.metadata["word_timestamps"] == [
        {"start_ms": 1200.0, "end_ms": 2400.0, "word": "hello", "confidence": 0.9}
    ]
