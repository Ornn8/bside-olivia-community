"""Real local Nemotron-Speech.cpp WebSocket provider.

The native path only speaks the documented local HTTP/WebSocket protocol.  It
does not run offline inference, replay a transcript, or synthesize readiness.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import aiohttp

from .config import (
    MODEL_REVISION,
    MODEL_SHA256,
    NATIVE_MAX_CER,
    NATIVE_MAX_WER,
    RUNTIME_REVISION,
    AsrConfig,
)
from .contracts import AsrEvent, EventClock, pcm16_rms
from .errors import AsrError
from .fallback import TextFallbackProvider
from .protocol import NemotronProtocolAdapter


def _http_url(server_url: str) -> str:
    parsed = urlparse(server_url)
    scheme = "https" if parsed.scheme == "wss" else "http"
    return urlunparse((scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class NemotronProvider:
    """Status, readiness probe, and session factory for the local runtime."""

    provider_name = "nemotron-speech-cpp"

    def __init__(self, config: AsrConfig) -> None:
        if config.provider != self.provider_name:
            raise AsrError("ASR_CONFIG_INVALID", "NemotronProvider requires the native provider config")
        self.config = config

    def _runtime_present(self) -> bool:
        return self.config.effective_runtime_executable.is_file()

    def _model_state(self) -> tuple[str, str]:
        path = self.config.effective_model_path
        if not path.is_file():
            return "missing", "ASR_MODEL_MISSING"
        if path.stat().st_size < 1024:
            return "corrupt", "ASR_MODEL_CORRUPT"
        digest = _sha256_file(path)
        if digest != MODEL_SHA256:
            return "corrupt", "ASR_MODEL_CORRUPT"
        sidecar = Path(f"{path}.sha256")
        if sidecar.is_file():
            expected = sidecar.read_text(encoding="utf-8").strip().split()[0]
            if expected != MODEL_SHA256:
                return "corrupt", "ASR_MODEL_CORRUPT"
            if digest != expected:
                return "corrupt", "ASR_MODEL_CORRUPT"
        return "present", ""

    def _acceptance_verified(self) -> tuple[bool, dict[str, Any]]:
        path = self.config.acceptance_manifest
        if not path.is_file():
            return False, {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False, {}
        if not isinstance(payload, Mapping):
            return False, {}
        required = {
            "verified": True,
            "backend": "cuda",
            "model_revision": MODEL_REVISION,
            "runtime_revision": RUNTIME_REVISION,
            "websocket_roundtrip": True,
        }
        verified = all(payload.get(key) == value for key, value in required.items())
        device = str(payload.get("device", ""))
        verified = verified and "rtx 3080" in device.casefold()
        verified = verified and payload.get("model_sha256") == MODEL_SHA256
        verified = verified and payload.get("models_before_status") == 200
        verified = verified and payload.get("models_after_status") == 200
        verified = verified and payload.get("ready_http_status") == 200
        verified = verified and payload.get("health_status") == 200
        verified = verified and payload.get("process_exit_before_cleanup") is None
        order = payload.get("request_order")
        verified = verified and isinstance(order, Mapping) and order.get("models_first") is True
        for metric, limit in (("cer", NATIVE_MAX_CER), ("wer", NATIVE_MAX_WER)):
            value = payload.get(metric)
            verified = verified and isinstance(value, (int, float)) and math.isfinite(value) and value <= limit
        controls = payload.get("controls")
        verified = verified and isinstance(controls, Mapping)
        if isinstance(controls, Mapping):
            cancel = controls.get("cancel")
            disconnect = controls.get("disconnect")
            silence = controls.get("silence")
            backpressure = controls.get("backpressure")
            verified = verified and isinstance(cancel, Mapping) and cancel.get("cleared") is True
            verified = verified and isinstance(disconnect, Mapping) and disconnect.get("closed_without_commit") is True
            verified = verified and isinstance(silence, Mapping) and silence.get("completed") is True and silence.get("empty") is True
            verified = verified and isinstance(backpressure, Mapping) and backpressure.get("provider_queue_saturated") is True
        return verified, dict(payload)

    def _base_status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "provider": self.provider_name,
            "status": "unavailable",
            "ready": False,
            "reason": "ASR_NOT_PROBED",
            "language": self.config.language,
            "network_called": False,
            "verified": False,
            "runtime_revision": RUNTIME_REVISION,
            "model_revision": MODEL_REVISION,
            "runtime_present": False,
            "model_present": False,
        }
        if not self._runtime_present():
            result["reason"] = "ASR_RUNTIME_MISSING"
            return result
        result["runtime_present"] = True
        model_state, model_reason = self._model_state()
        if model_state != "present":
            result["reason"] = model_reason
            return result
        result["model_present"] = True
        verified, evidence = self._acceptance_verified()
        result["verified"] = verified
        if evidence:
            result["device"] = evidence.get("device")
            result["backend"] = evidence.get("backend")
        if verified:
            result.update({"status": "available", "ready": True, "reason": "ASR_READY_VERIFIED"})
        return result

    def status(self) -> dict[str, Any]:
        """Return local diagnostics without starting a process or making I/O."""

        return self._base_status()

    async def probe_ready(self) -> dict[str, Any]:
        """Probe the documented ``/ready`` endpoint without inventing readiness."""

        result = self._base_status()
        if not result["runtime_present"] or not result["model_present"]:
            return result
        timeout = aiohttp.ClientTimeout(total=self.config.connect_timeout_ms / 1000)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as client:
                async with client.get(f"{_http_url(self.config.server_url)}/ready") as response:
                    result["network_called"] = True
                    result["ready_http_status"] = response.status
                    try:
                        payload = await response.json()
                    except (aiohttp.ContentTypeError, ValueError):
                        payload = {}
                    runtime_ready = response.status == 200 and isinstance(payload, Mapping) and payload.get("ready") is True
                    result["runtime_ready"] = runtime_ready
                    if not runtime_ready:
                        result.update({"status": "unavailable", "ready": False, "reason": "ASR_NOT_READY"})
                        return result
                    if not result["verified"]:
                        # A /ready response proves only server initialization.  The
                        # product gate additionally requires the recorded real CUDA
                        # WebSocket acceptance on an RTX 3080.
                        result.update({"status": "unavailable", "ready": False, "reason": "ASR_NOT_PROBED"})
                    return result
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            result.update(
                {
                    "network_called": True,
                    "status": "unavailable",
                    "ready": False,
                    "reason": "ASR_PROVIDER_UNAVAILABLE",
                    "diagnostic": type(exc).__name__,
                }
            )
            return result

    async def open_session(self) -> "NemotronStreamingSession":
        status = await self.probe_ready()
        if status.get("status") != "available":
            raise AsrError(str(status.get("reason", "ASR_PROVIDER_UNAVAILABLE")), "native ASR is not ready", status)
        return await NemotronStreamingSession.connect(self)


class NemotronStreamingSession:
    """One real PCM16 WebSocket session against NeMo-Speech.cpp."""

    def __init__(self, provider: NemotronProvider) -> None:
        self.provider = provider
        self.config = provider.config
        self.clock = EventClock()
        self.adapter = NemotronProtocolAdapter(self.clock, requested_language=self.config.language)
        self._client: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._sender_task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=self.config.max_queue_chunks)
        self._pending: list[AsrEvent] = []
        self._audio_ms = 0.0
        self._closed = False
        self._committed = False
        self._terminal_timeout = False

    @classmethod
    async def connect(cls, provider: NemotronProvider) -> "NemotronStreamingSession":
        self = cls(provider)
        timeout = aiohttp.ClientTimeout(total=self.config.connect_timeout_ms / 1000)
        self._client = aiohttp.ClientSession(timeout=timeout)
        try:
            self._ws = await self._client.ws_connect(f"{self.config.server_url.rstrip('/')}/v1/realtime")
            first = await self._receive_json(timeout=self.config.connect_timeout_ms / 1000)
            if first.get("type") != "session.created":
                raise AsrError("ASR_PROTOCOL_ERROR", "first WebSocket event was not session.created")
            self._pending.append(self.adapter.ingest(first))
            await self._ws.send_json(
                {
                    "type": "session.update",
                    "session": {
                        "sample_rate": self.config.sample_rate,
                        "language": self.config.language,
                        "word_timestamps": self.config.word_timestamps,
                        "endpointing_ms": self.config.endpointing_ms,
                    },
                }
            )
            updated = await self._receive_json(timeout=self.config.connect_timeout_ms / 1000)
            if updated.get("type") != "session.updated":
                raise AsrError("ASR_PROTOCOL_ERROR", "WebSocket did not confirm session.update")
            self._pending.append(self.adapter.ingest(updated))
            self._sender_task = asyncio.create_task(self._send_loop())
            return self
        except Exception:
            await self.close()
            raise

    async def _receive_json(self, *, timeout: float) -> Mapping[str, Any]:
        if self._ws is None:
            raise AsrError("ASR_NOT_READY", "WebSocket is not connected")
        try:
            message = await asyncio.wait_for(self._ws.receive(), timeout=timeout)
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            raise AsrError("ASR_PROVIDER_UNAVAILABLE", "WebSocket receive failed", {"diagnostic": type(exc).__name__}) from exc
        if message.type != aiohttp.WSMsgType.TEXT:
            raise AsrError("ASR_PROTOCOL_ERROR", "expected a JSON text WebSocket event")
        try:
            payload = json.loads(message.data)
        except ValueError as exc:
            raise AsrError("ASR_PROTOCOL_ERROR", "provider sent invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise AsrError("ASR_PROTOCOL_ERROR", "provider JSON event is not an object")
        return payload

    async def _send_loop(self) -> None:
        try:
            while True:
                payload = await self._queue.get()
                try:
                    if payload is None:
                        return
                    if self._ws is None or self._ws.closed:
                        raise AsrError("ASR_DISCONNECTED", "WebSocket closed while sending audio")
                    await self._ws.send_bytes(payload)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise
        except AsrError as exc:
            self._pending.append(
                self.clock.emit("error", provider=self.provider.provider_name, code=exc.code, reason=exc.reason)
            )

    async def send_audio(self, pcm16: bytes) -> None:
        if self._closed:
            raise AsrError("ASR_DISCONNECTED", "session is closed")
        if not pcm16 or len(pcm16) % 2:
            raise AsrError("ASR_INVALID_AUDIO", "audio must be non-empty PCM16")
        try:
            pcm16_rms(pcm16)
        except ValueError as exc:
            raise AsrError("ASR_INVALID_AUDIO", str(exc)) from exc
        try:
            await asyncio.wait_for(
                self._queue.put(pcm16), timeout=self.config.backpressure_timeout_ms / 1000
            )
        except asyncio.TimeoutError as exc:
            event = self.clock.emit(
                "error",
                provider=self.provider.provider_name,
                code="ASR_BACKPRESSURE",
                reason="audio queue is full",
            )
            self._pending.append(event)
            raise AsrError("ASR_BACKPRESSURE", "audio queue is full") from exc
        self._audio_ms += len(pcm16) / 2 / self.config.sample_rate * 1000

    async def commit(self) -> None:
        if self._ws is None or self._ws.closed:
            raise AsrError("ASR_DISCONNECTED", "session is not connected")
        await self._queue.join()
        await self._ws.send_json({"type": "input_audio_buffer.commit"})
        self._committed = True

    async def cancel(self) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.send_json({"type": "response.cancel"})
            await self._ws.send_json({"type": "input_audio_buffer.clear"})
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break
        self._pending.append(
            self.clock.emit(
                "canceled", provider=self.provider.provider_name, code="ASR_CANCELED", reason="session canceled"
            )
        )
        self._committed = False

    def _with_audio(self, event: AsrEvent) -> AsrEvent:
        if event.metadata.get("audio_timestamp_source") == "native_audio_processed":
            return event
        if event.audio_ms == self._audio_ms:
            return event
        return replace(event, audio_ms=self._audio_ms)

    async def events(self) -> AsyncIterator[AsrEvent]:
        while self._pending:
            yield self._with_audio(self._pending.pop(0))
        while not self._closed:
            if self._ws is None:
                return
            timeout = self.config.final_timeout_ms / 1000 if self._committed else None
            try:
                message = await asyncio.wait_for(self._ws.receive(), timeout=timeout) if timeout else await self._ws.receive()
            except asyncio.TimeoutError:
                if not self._terminal_timeout:
                    self._terminal_timeout = True
                    yield self.clock.emit(
                        "error",
                        provider=self.provider.provider_name,
                        code="ASR_FINAL_TIMEOUT",
                        reason="provider did not emit a final or silence event",
                        audio_ms=self._audio_ms,
                    )
                return
            except (aiohttp.ClientError, OSError):
                yield self.clock.emit(
                    "disconnected",
                    provider=self.provider.provider_name,
                    code="ASR_DISCONNECTED",
                    reason="WebSocket receive failed",
                    audio_ms=self._audio_ms,
                )
                return
            if message.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                except ValueError:
                    yield self.clock.emit(
                        "error",
                        provider=self.provider.provider_name,
                        code="ASR_PROTOCOL_ERROR",
                        reason="provider sent invalid JSON",
                        audio_ms=self._audio_ms,
                    )
                    continue
                if not isinstance(payload, Mapping):
                    continue
                yield self._with_audio(self.adapter.ingest(payload))
                continue
            if message.type in {aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                yield self.clock.emit(
                    "disconnected",
                    provider=self.provider.provider_name,
                    code="ASR_DISCONNECTED",
                    reason="provider WebSocket closed",
                    audio_ms=self._audio_ms,
                )
                return
            yield self.clock.emit(
                "error",
                provider=self.provider.provider_name,
                code="ASR_PROTOCOL_ERROR",
                reason="unexpected binary/server WebSocket message",
                audio_ms=self._audio_ms,
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._sender_task is not None:
            self._sender_task.cancel()
            await asyncio.gather(self._sender_task, return_exceptions=True)
            self._sender_task = None
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._client is not None and not self._client.closed:
            await self._client.close()
        self._pending.append(self.clock.emit("closed", provider=self.provider.provider_name, audio_ms=self._audio_ms))

    async def __aenter__(self) -> "NemotronStreamingSession":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()


def create_provider(config: AsrConfig | None = None) -> TextFallbackProvider | NemotronProvider:
    config = config or AsrConfig.from_env()
    if config.provider == "text-fallback":
        return TextFallbackProvider()
    return NemotronProvider(config)


async def transcribe_stream(
    chunks: AsyncIterable[bytes], config: AsrConfig | None = None
) -> AsyncIterator[AsrEvent]:
    """Stream PCM16 chunks through the real native WebSocket path."""

    provider = create_provider(config)
    if not isinstance(provider, NemotronProvider):
        raise AsrError("ASR_CONFIG_INVALID", "native transcribe_stream requires the native provider")
    session = await provider.open_session()
    try:
        async for chunk in chunks:
            await session.send_audio(chunk)
        await session.commit()
        async for event in session.events():
            yield event
            if event.type in {"final", "silence", "error", "disconnected"}:
                break
    finally:
        await session.close()
