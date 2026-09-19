from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import aiohttp
import pytest

from asr.config import AsrConfig
from asr.errors import AsrError
from asr.provider import NemotronProvider, NemotronStreamingSession


def _native_config(tmp_path: Path) -> AsrConfig:
    return AsrConfig(provider="nemotron-speech-cpp").with_test_paths(tmp_path)


def test_status_reports_runtime_missing_before_model_or_network(tmp_path: Path) -> None:
    status = NemotronProvider(_native_config(tmp_path)).status()
    assert status["status"] == "unavailable"
    assert status["ready"] is False
    assert status["reason"] == "ASR_RUNTIME_MISSING"
    assert status["network_called"] is False


def test_status_distinguishes_missing_and_corrupt_model(tmp_path: Path) -> None:
    config = _native_config(tmp_path)
    config.runtime_root.mkdir(parents=True)
    config.effective_runtime_executable.write_bytes(b"runtime-placeholder")
    missing = NemotronProvider(config).status()
    assert missing["reason"] == "ASR_MODEL_MISSING"

    config.effective_model_path.parent.mkdir(parents=True)
    config.effective_model_path.write_bytes(b"not-a-model")
    corrupt = NemotronProvider(config).status()
    assert corrupt["reason"] == "ASR_MODEL_CORRUPT"


def test_status_rejects_wrong_model_without_sha256_sidecar(tmp_path: Path) -> None:
    config = _native_config(tmp_path)
    config.runtime_root.mkdir(parents=True)
    config.effective_runtime_executable.write_bytes(b"runtime-placeholder")
    config.effective_model_path.parent.mkdir(parents=True)
    config.effective_model_path.write_bytes(b"wrong-model".ljust(1024, b"x"))

    status = NemotronProvider(config).status()

    assert status["reason"] == "ASR_MODEL_CORRUPT"


def test_status_never_claims_available_without_real_acceptance_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _native_config(tmp_path)
    config.runtime_root.mkdir(parents=True)
    config.effective_runtime_executable.write_bytes(b"runtime-placeholder")
    config.effective_model_path.parent.mkdir(parents=True)
    config.effective_model_path.write_bytes(b"model-placeholder".ljust(1024, b"x"))
    config.acceptance_manifest.parent.mkdir(parents=True)
    config.acceptance_manifest.write_text(
        json.dumps({"verified": False, "backend": "cpu"}), encoding="utf-8"
    )
    monkeypatch.setattr(NemotronProvider, "_model_state", lambda self: ("present", ""))
    status = NemotronProvider(config).status()
    assert status["status"] == "unavailable"
    assert status["ready"] is False
    assert status["reason"] == "ASR_NOT_PROBED"
    assert status["verified"] is False


def test_probe_connection_refused_is_truthful_unavailable(tmp_path: Path) -> None:
    config = _native_config(tmp_path)
    result = asyncio.run(NemotronProvider(config).probe_ready())
    assert result["status"] == "unavailable"
    assert result["ready"] is False
    assert result["reason"] in {"ASR_RUNTIME_MISSING", "ASR_MODEL_MISSING"}


class _OfflineWebSocket:
    """Transport seam only; production never constructs this class."""

    def __init__(self, message=None) -> None:
        self.closed = False
        self.sent: list[tuple[str, object]] = []
        self._message = message

    async def send_json(self, payload) -> None:
        self.sent.append(("json", payload))

    async def send_bytes(self, payload) -> None:
        self.sent.append(("bytes", payload))

    async def receive(self):
        return self._message

    async def close(self) -> None:
        self.closed = True


def test_cancel_clears_queue_and_sends_documented_control_messages(tmp_path: Path) -> None:
    provider = NemotronProvider(_native_config(tmp_path))
    session = NemotronStreamingSession(provider)
    websocket = _OfflineWebSocket()
    session._ws = websocket
    session._queue.put_nowait(b"\x00\x00")

    asyncio.run(session.cancel())

    sent_types = [payload["type"] for kind, payload in websocket.sent if kind == "json"]
    assert sent_types == ["response.cancel", "input_audio_buffer.clear"]
    assert session._queue.empty()
    assert session._pending[-1].type == "canceled"


def test_backpressure_is_error_and_never_drops_the_first_chunk(tmp_path: Path) -> None:
    config = replace(_native_config(tmp_path), max_queue_chunks=1, backpressure_timeout_ms=0)
    session = NemotronStreamingSession(NemotronProvider(config))
    session._queue.put_nowait(b"\x00\x00")

    with pytest.raises(AsrError, match="audio queue is full") as caught:
        asyncio.run(session.send_audio(b"\x01\x00"))

    assert caught.value.code == "ASR_BACKPRESSURE"
    assert session._queue.qsize() == 1
    assert session._pending[-1].code == "ASR_BACKPRESSURE"


def test_disconnect_is_explicit_terminal_event(tmp_path: Path) -> None:
    closed = aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None)
    session = NemotronStreamingSession(NemotronProvider(_native_config(tmp_path)))
    session._ws = _OfflineWebSocket(closed)

    async def collect():
        return [event async for event in session.events()]

    events = asyncio.run(collect())
    assert [event.type for event in events] == ["disconnected"]
    assert events[0].code == "ASR_DISCONNECTED"


def test_provider_preserves_native_audio_position_for_av_sync(tmp_path: Path) -> None:
    session = NemotronStreamingSession(NemotronProvider(_native_config(tmp_path)))
    session._audio_ms = 2400.0
    native = session.adapter.ingest(
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "delta": "hello",
            "audio_processed": 1.2,
        }
    )
    fallback = session.clock.emit("partial", text="hello")

    assert session._with_audio(native).audio_ms == 1200.0
    assert session._with_audio(fallback).audio_ms == 2400.0
