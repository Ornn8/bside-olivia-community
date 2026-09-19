from __future__ import annotations

import pytest

from asr.config import AsrConfig
from asr.contracts import EventClock
from asr.errors import AsrError
from asr.protocol import NemotronProtocolAdapter


@pytest.mark.parametrize(
    ("event_type", "code"),
    [
        ("canceled", "ASR_CANCELED"),
        ("disconnected", "ASR_DISCONNECTED"),
        ("error", "ASR_BACKPRESSURE"),
    ],
)
def test_cancel_disconnect_and_backpressure_are_explicit_contract_events(
    event_type: str, code: str
) -> None:
    event = EventClock(session_id="failure").emit(event_type, code=code, reason=code)
    assert event.type == event_type
    assert event.code == code
    assert event.reason == code


@pytest.mark.parametrize("code", ["ASR_MODEL_MISSING", "ASR_MODEL_CORRUPT", "ASR_PROVIDER_UNAVAILABLE"])
def test_model_and_provider_failures_have_stable_codes(code: str) -> None:
    error = AsrError(code, "diagnostic")
    event = EventClock(session_id="failure").emit(
        "error", code=error.code, reason=error.reason, metadata=error.to_dict()["details"]
    )
    assert event.code == code
    assert event.to_dict()["metadata"] == {}


def test_language_is_explicit_or_server_reported_never_silently_fixed() -> None:
    explicit = AsrConfig(provider="nemotron-speech-cpp", language="ja-JP")
    assert explicit.language == "ja-JP"

    adapter = NemotronProtocolAdapter(EventClock(session_id="language"), requested_language="auto")
    before_server_language = adapter.ingest({"type": "session.created", "session": {}})
    after_server_language = adapter.ingest(
        {"type": "session.updated", "session": {"language": "de-DE"}}
    )
    assert before_server_language.language is None
    assert after_server_language.language == "de-DE"
    assert after_server_language.metadata["requested_language"] == "auto"


def test_silence_and_unavailable_do_not_look_ready() -> None:
    adapter = NemotronProtocolAdapter(EventClock(session_id="availability"))
    silence = adapter.ingest(
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": ""}
    )
    unavailable = adapter.ingest(
        {"type": "error", "error": {"code": "ASR_PROVIDER_UNAVAILABLE", "message": "not ready"}}
    )
    assert silence.type == "silence"
    assert unavailable.type == "error"
    assert unavailable.code == "ASR_PROVIDER_UNAVAILABLE"
    assert silence.type != "ready"
    assert unavailable.type != "ready"
