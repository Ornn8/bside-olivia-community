"""Single-owner cancellable Live session implementation."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import struct
import time
from typing import Any

from llm_gateway import Gateway
from memory_port import MemoryPort
from memory_prompt import MemoryPromptBuilder
from asr.errors import AsrError
from reply_orchestrator import ReplyEventType, ReplyOrchestrator, ReplyRequest, ReplyState
from persona_assembly import UntrustedFragment, assemble_persona
from persona_loader import PersonaSnapshot as PersonaV2Snapshot
from reply_context import ReplyContext, ReplyMode, TrustedTime

from .contracts import (
    LiveConfig,
    LiveError,
    LiveEvent,
    LiveSessionState,
    LiveTurnHandle,
    LiveTurnResult,
    fingerprint_text,
    monotonic_ms,
    replay_trace,
)


class LiveSession:
    def __init__(
        self,
        *,
        session_id: str,
        owner_id: str,
        gateway: Gateway,
        memory_port: MemoryPort,
        persona_provider: Any | None,
        asr_provider: Any,
        tts_service: Any,
        visual_driver: Any,
        visual_request: Any | None,
        config: LiveConfig,
    ) -> None:
        self.session_id = session_id
        self._owner_id = owner_id
        self._gateway = gateway
        self._memory_port = memory_port
        self._persona_provider = persona_provider
        self._asr_provider = asr_provider
        self._tts_service = tts_service
        self._visual_driver = visual_driver
        self._visual_request = visual_request
        self.config = config
        self.state = LiveSessionState.CREATED
        self._started = time.monotonic()
        self._sequence = -1
        self._events: asyncio.Queue[LiveEvent] = asyncio.Queue(maxsize=config.max_events)
        self._timeline: list[LiveEvent] = []
        self._dropped_events = 0
        self._closed = False
        self._turns: dict[str, LiveTurnHandle] = {}
        self._idempotency: dict[str, LiveTurnHandle] = {}
        self._active_turn: LiveTurnHandle | None = None
        self._history: list[tuple[str, str]] = []
        self._input_transcript = ""
        self._memory_prompt = MemoryPromptBuilder(memory_port)
        self._reply = ReplyOrchestrator(
            gateway,
            timeout_seconds=config.turn_timeout_seconds,
            queue_size=max(4, min(config.max_events, 64)),
        )
        self._emit("session_created", LiveSessionState.CREATED, status="created")
        self.state = LiveSessionState.LISTENING
        self._emit("session_ready", self.state, status="ready")

    @property
    def owner_id(self) -> str:
        """Internal authorization value; events never expose it."""

        return self._owner_id

    async def next_event(self) -> LiveEvent:
        return await self._events.get()

    async def events(self):
        while True:
            event = await self.next_event()
            yield event
            if event.event == "session_closed":
                return

    def trace(self) -> tuple[dict[str, Any], ...]:
        """Return the redacted, deterministic session timeline for replay/audit."""

        return replay_trace(event.trace_dict() for event in self._timeline)

    @property
    def input_transcript(self) -> str:
        """Return the last native-ASR transcript for the caller's own turn."""

        return self._input_transcript

    async def submit_text(self, text: str, *, idempotency_key: str | None = None) -> LiveTurnHandle:
        if not isinstance(text, str) or not text.strip() or len(text) > self.config.max_input_chars:
            raise LiveError("LIVE_INVALID_INPUT")
        self._ensure_open()
        key = idempotency_key or f"turn:{fingerprint_text(text)}"
        fingerprint = fingerprint_text(text)
        existing = self._idempotency.get(key)
        if existing is not None:
            if existing.fingerprint != fingerprint:
                return self._failed_handle("IDEMPOTENCY_CONFLICT")
            return existing
        if self._active_turn is not None and not self._active_done():
            await self.interrupt()
        handle = LiveTurnHandle(kind="text", fingerprint=fingerprint)
        handle.result_future = asyncio.get_running_loop().create_future()
        self._turns[handle.turn_id] = handle
        self._idempotency[key] = handle
        self._active_turn = handle
        handle.task = asyncio.create_task(self._run_text(handle, text), name=f"live-turn-{handle.turn_id}")
        return handle

    async def send_text(self, text: str, *, idempotency_key: str | None = None) -> LiveTurnResult:
        handle = await self.submit_text(text, idempotency_key=idempotency_key)
        return await handle.wait()

    async def start_audio_turn(self, *, idempotency_key: str | None = None) -> LiveTurnHandle:
        self._ensure_open()
        key = idempotency_key or f"audio:{self._sequence + 1}"
        fingerprint = f"audio:{key}"
        existing = self._idempotency.get(key)
        if existing is not None:
            if existing.fingerprint != fingerprint:
                return self._failed_handle("IDEMPOTENCY_CONFLICT")
            return existing
        if self._active_turn is not None and not self._active_done():
            await self.interrupt()
        handle = LiveTurnHandle(kind="audio", fingerprint=fingerprint)
        handle.result_future = asyncio.get_running_loop().create_future()
        handle.audio_ready = asyncio.Event()
        handle.asr_session = None
        handle.audio_committed = False
        self._turns[handle.turn_id] = handle
        self._idempotency[key] = handle
        self._active_turn = handle
        handle.task = asyncio.create_task(self._run_audio(handle), name=f"live-audio-{handle.turn_id}")
        return handle

    async def send_audio(self, turn_id: str, pcm16: bytes) -> None:
        handle = self._turns.get(turn_id)
        if handle is None or handle.kind != "audio":
            raise LiveError("LIVE_SESSION_NOT_FOUND")
        await handle.audio_ready.wait()
        if handle.result_future.done() or handle.asr_session is None:
            raise LiveError("ASR_UNAVAILABLE")
        try:
            await handle.asr_session.send_audio(pcm16)
        except Exception as exc:
            code = str(getattr(exc, "code", "ASR_DISCONNECTED"))
            if code not in {"ASR_BACKPRESSURE", "ASR_DISCONNECTED", "ASR_INVALID_AUDIO"}:
                code = "ASR_DISCONNECTED"
            self._emit(
                "asr_error",
                self.state,
                turn_id=turn_id,
                component="asr",
                error_code=code,
                status="unavailable",
            )
            raise LiveError(code, retryable=code == "ASR_BACKPRESSURE") from exc

    async def commit_audio(self, turn_id: str) -> None:
        handle = self._turns.get(turn_id)
        if handle is None or handle.kind != "audio":
            raise LiveError("LIVE_SESSION_NOT_FOUND")
        await handle.audio_ready.wait()
        if handle.result_future.done() or handle.asr_session is None:
            raise LiveError("ASR_UNAVAILABLE")
        await handle.asr_session.commit()
        handle.audio_committed = True

    async def cancel_turn(self, turn_id: str | None = None) -> bool:
        handle = self._turns.get(turn_id) if turn_id else self._active_turn
        if handle is None or handle.result_future is None or handle.result_future.done():
            return False
        handle._cancel_requested = True
        asr_session = getattr(handle, "asr_session", None)
        if asr_session is not None:
            cancel = getattr(asr_session, "cancel", None)
            if callable(cancel):
                await cancel()
        await self._cancel_task_and_wait(handle)
        return True

    async def interrupt(self) -> bool:
        handle = self._active_turn
        if handle is None or handle.result_future is None or handle.result_future.done():
            return False
        handle._cancel_requested = True
        handle._interrupt_requested = True
        self.state = LiveSessionState.LISTENING
        self._emit(
            "interrupted",
            self.state,
            turn_id=handle.turn_id,
            component="session",
            status="barge_in",
            error_code="LIVE_INTERRUPTED",
        )
        await self._cancel_task_and_wait(handle)
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        active = self._active_turn
        if active is not None and active.result_future is not None and not active.result_future.done():
            active._cancel_requested = True
            task = active.task
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self.state = LiveSessionState.CLOSED
        self._emit("session_closed", self.state, status="closed")

    async def _run_text(self, handle: LiveTurnHandle, text: str) -> None:
        started = time.monotonic()
        self.state = LiveSessionState.THINKING
        self._emit("turn_started", self.state, turn_id=handle.turn_id, component="session")
        try:
            messages, memory_status = self._build_messages(text, handle.turn_id)
        except Exception:
            self._emit(
                "llm_error",
                LiveSessionState.FAILED,
                turn_id=handle.turn_id,
                component="llm",
                error_code="LIVE_LLM_ERROR",
                status="unavailable",
            )
            self._finish(
                handle,
                LiveTurnResult(
                    handle.turn_id,
                    "failed",
                    error_code="LIVE_LLM_ERROR",
                    latency_ms=monotonic_ms(started),
                ),
                LiveSessionState.LISTENING,
            )
            return
        self._emit("llm_started", self.state, turn_id=handle.turn_id, component="llm")
        accumulated = ""
        reply_run = None
        try:
            reply_run = await self._reply.start(
                ReplyRequest(
                    messages=messages,
                    request_id=handle.turn_id,
                    idempotency_key=f"{self.session_id}:{handle.turn_id}",
                    max_input_chars=self.config.max_input_chars,
                )
            )
            async for event in reply_run.events():
                if event.event is ReplyEventType.STREAM_DELTA and event.delta:
                    accumulated += event.delta
                    self._emit(
                        "llm_delta",
                        self.state,
                        turn_id=handle.turn_id,
                        component="llm",
                        text_present=True,
                    )
                elif event.event is ReplyEventType.COMPLETED:
                    accumulated = event.text or accumulated
            reply_result = await reply_run.wait()
            if reply_result.state is ReplyState.CANCELLED or handle.cancel_requested():
                raise asyncio.CancelledError
            if not reply_result.completed:
                if reply_result.error_code in {"LLM_TIMEOUT", "PROVIDER_TIMEOUT"}:
                    result = LiveTurnResult(
                        handle.turn_id,
                        "timeout",
                        error_code="LIVE_TIMEOUT",
                        retryable=True,
                        latency_ms=monotonic_ms(started),
                    )
                    self._finish(handle, result, LiveSessionState.LISTENING)
                    return
                error_code = "LIVE_LLM_UNAVAILABLE" if reply_result.error_code in {
                    "PROVIDER_UNAVAILABLE",
                    "PROVIDER_TIMEOUT",
                    "LLM_TIMEOUT",
                } else "LIVE_LLM_ERROR"
                self._emit(
                    "llm_error",
                    LiveSessionState.FAILED,
                    turn_id=handle.turn_id,
                    component="llm",
                    error_code=error_code,
                    status="unavailable",
                )
                safe_text = self.config.safe_unavailable_text
                self.state = LiveSessionState.LISTENING
                self._emit(
                    "safe_static_output",
                    self.state,
                    turn_id=handle.turn_id,
                    component="llm",
                    status="safe_static",
                    error_code=error_code,
                    text_present=bool(safe_text),
                    metadata={"model_generated": False, "provider": "none"},
                )
                result = LiveTurnResult(
                    handle.turn_id,
                    "degraded",
                    text=safe_text,
                    error_code=error_code,
                    retryable=bool(reply_result.retryable),
                    text_source="safe_static",
                    memory_status=memory_status,
                    tts_status="text_fallback",
                    latency_ms=monotonic_ms(started),
                )
                self._finish(handle, result, LiveSessionState.LISTENING)
                return
            accumulated = (reply_result.text or accumulated).strip()
            self._emit(
                "llm_completed",
                self.state,
                turn_id=handle.turn_id,
                component="llm",
                status="completed",
            )
            self.state = LiveSessionState.SPEAKING
            self._emit(
                "text_output",
                self.state,
                turn_id=handle.turn_id,
                component="llm",
                status="model_generated",
                text_present=bool(accumulated),
            )
            tts_status = await self._speak(handle, accumulated)
            result = LiveTurnResult(
                handle.turn_id,
                "completed",
                text=accumulated,
                text_source="llm",
                memory_status=memory_status,
                tts_status=tts_status,
                visual_status=str(getattr(handle, "visual_status", "not_started")),
                audio_chunks=int(getattr(handle, "audio_chunks", 0)),
                visual_frames=int(getattr(handle, "visual_frames", 0)),
                latency_ms=monotonic_ms(started),
            )
            conversation_enabled = False
            if memory_status == "available":
                try:
                    conversation_enabled = (
                        self._memory_port.status().get("conversation_enabled") is True
                    )
                except Exception:
                    conversation_enabled = False
            if conversation_enabled:
                try:
                    self._memory_port.remember_conversation(
                        f"User sent a Live message: {text[:4000]}",
                        facts=(f"Assistant completed a Live reply: {accumulated[:4000]}",),
                        metadata={"source": "live-session"},
                    )
                except Exception:
                    memory_status = "session-only"
                    result = LiveTurnResult(
                        **{
                            **result.__dict__,
                            "memory_status": memory_status,
                        }
                    )
                    self._emit(
                        "memory_fallback",
                        self.state,
                        turn_id=handle.turn_id,
                        component="memory",
                        status="session-only",
                        error_code="MEMORY_UNAVAILABLE",
                    )
            if result.status == "completed":
                self._history.append((text, accumulated))
                if len(self._history) > self.config.max_history_turns:
                    del self._history[: -self.config.max_history_turns]
            self._finish(handle, result, LiveSessionState.LISTENING)
        except asyncio.TimeoutError:
            result = LiveTurnResult(
                handle.turn_id,
                "timeout",
                error_code="LIVE_TIMEOUT",
                retryable=True,
                latency_ms=monotonic_ms(started),
            )
            self._finish(handle, result, LiveSessionState.LISTENING)
        except asyncio.CancelledError:
            if reply_run is not None:
                await self._cancel_reply_and_wait(reply_run)
            tts_run = getattr(handle, "tts_run", None)
            if tts_run is not None:
                await self._cancel_tts_and_wait(tts_run)
            status = "interrupted" if getattr(handle, "_interrupt_requested", False) else "cancelled"
            error_code = "LIVE_INTERRUPTED" if status == "interrupted" else "LIVE_CANCELED"
            result = LiveTurnResult(
                handle.turn_id,
                status,
                error_code=error_code,
                latency_ms=monotonic_ms(started),
            )
            if not handle.result_future.done():
                handle.result_future.set_result(result)
            self._emit(
                "turn_interrupted" if status == "interrupted" else "turn_cancelled",
                self.state,
                turn_id=handle.turn_id,
                component="session",
                error_code=error_code,
            )
            if self._active_turn is handle:
                self._active_turn = None
        except Exception:
            result = LiveTurnResult(
                handle.turn_id,
                "failed",
                error_code="LIVE_LLM_ERROR",
                latency_ms=monotonic_ms(started),
            )
            self._finish(handle, result, LiveSessionState.FAILED)

    async def _cancel_task_and_wait(self, handle: LiveTurnHandle) -> None:
        task = handle.task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            # The session task owns its public result/error boundary; cleanup
            # must still complete when a connector raises during cancellation.
            pass

    async def _cancel_reply_and_wait(self, run: Any) -> None:
        cancel = getattr(run, "cancel", None)
        if callable(cancel):
            cancel()
        wait = getattr(run, "wait", None)
        if callable(wait):
            try:
                await wait()
            except asyncio.CancelledError:
                pass
            except Exception:
                # The LLM boundary is already canceled; cleanup must not
                # replace the sanitized Live cancellation result.
                pass

    async def _run_audio(self, handle: LiveTurnHandle) -> None:
        provider = self._asr_provider
        try:
            try:
                raw_status = dict(provider.status())
            except Exception:
                raw_status = {"status": "unavailable", "ready": False, "is_asr": False}
            native = (
                raw_status.get("status") == "available"
                and raw_status.get("ready") is True
                and raw_status.get("is_asr", True) is True
                and callable(getattr(provider, "open_session", None))
            )
            if not native:
                handle.audio_ready.set()
                self._emit(
                    "asr_fallback",
                    LiveSessionState.LISTENING,
                    turn_id=handle.turn_id,
                    component="asr",
                    status="text_input",
                    error_code="ASR_UNAVAILABLE",
                )
                self._finish(
                    handle,
                    LiveTurnResult(
                        handle.turn_id,
                        "text_fallback",
                        error_code="ASR_UNAVAILABLE",
                        text_source="text_input",
                        latency_ms=0.0,
                    ),
                    LiveSessionState.LISTENING,
                )
                return
            session = None
            for attempt in range(self.config.reconnect_attempts + 1):
                try:
                    session = await provider.open_session()
                    handle.asr_session = session
                    handle.audio_ready.set()
                    self._emit(
                        "asr_ready",
                        LiveSessionState.LISTENING,
                        turn_id=handle.turn_id,
                        component="asr",
                        status="ready",
                    )
                    break
                except Exception as exc:
                    if attempt >= self.config.reconnect_attempts:
                        handle.audio_ready.set()
                        self._emit(
                            "asr_fallback",
                            LiveSessionState.LISTENING,
                            turn_id=handle.turn_id,
                            component="asr",
                            status="text_input",
                            error_code="ASR_UNAVAILABLE",
                        )
                        self._finish(
                            handle,
                            LiveTurnResult(
                                handle.turn_id,
                                "text_fallback",
                                error_code="ASR_UNAVAILABLE",
                                text_source="text_input",
                            ),
                            LiveSessionState.LISTENING,
                        )
                        return
                    self.state = LiveSessionState.RECONNECTING
                    self._emit(
                        "reconnecting",
                        self.state,
                        turn_id=handle.turn_id,
                        component="asr",
                        status="retrying",
                        error_code="ASR_DISCONNECTED",
                        metadata={"attempt": attempt + 1},
                    )
                    await asyncio.sleep(0)
            if session is None:
                return
            async for asr_event in session.events():
                if asr_event.type == "partial":
                    self._emit(
                        "asr_partial",
                        LiveSessionState.LISTENING,
                        turn_id=handle.turn_id,
                        component="asr",
                        status="partial",
                        text_present=bool(asr_event.text),
                    )
                elif asr_event.type == "final":
                    self._input_transcript = asr_event.text
                    self._emit(
                        "asr_final",
                        LiveSessionState.LISTENING,
                        turn_id=handle.turn_id,
                        component="asr",
                        status="final",
                        text_present=bool(asr_event.text),
                    )
                    await self._run_text(handle, asr_event.text)
                    return
                elif asr_event.type == "error":
                    code = asr_event.code or "ASR_DISCONNECTED"
                    self._emit(
                        "asr_error",
                        LiveSessionState.RECONNECTING,
                        turn_id=handle.turn_id,
                        component="asr",
                        status="unavailable",
                        error_code=code,
                    )
                    if code == "ASR_BACKPRESSURE":
                        self._finish(
                            handle,
                            LiveTurnResult(handle.turn_id, "failed", error_code=code),
                            LiveSessionState.LISTENING,
                        )
                        return
                elif asr_event.type == "disconnected":
                    self._emit(
                        "reconnecting",
                        LiveSessionState.RECONNECTING,
                        turn_id=handle.turn_id,
                        component="asr",
                        status="retrying",
                        error_code="ASR_DISCONNECTED",
                    )
                    self._finish(
                        handle,
                        LiveTurnResult(
                            handle.turn_id,
                            "text_fallback",
                            error_code="ASR_UNAVAILABLE",
                            text_source="text_input",
                        ),
                        LiveSessionState.LISTENING,
                    )
                    return
        except asyncio.CancelledError:
            asr_session = getattr(handle, "asr_session", None)
            if asr_session is not None:
                cancel = getattr(asr_session, "cancel", None)
                if callable(cancel):
                    await cancel()
            handle.audio_ready.set()
            if not handle.result_future.done():
                status = "interrupted" if getattr(handle, "_interrupt_requested", False) else "cancelled"
                error_code = "LIVE_INTERRUPTED" if status == "interrupted" else "LIVE_CANCELED"
                handle.result_future.set_result(
                    LiveTurnResult(handle.turn_id, status, error_code=error_code)
                )
                self._emit(
                    "turn_interrupted" if status == "interrupted" else "turn_cancelled",
                    LiveSessionState.LISTENING,
                    turn_id=handle.turn_id,
                    component="session",
                    error_code=error_code,
                )
            if self._active_turn is handle:
                self._active_turn = None
            raise
        except Exception:
            handle.audio_ready.set()
            self._finish(
                handle,
                LiveTurnResult(
                    handle.turn_id,
                    "text_fallback",
                    error_code="ASR_UNAVAILABLE",
                    text_source="text_input",
                ),
                LiveSessionState.LISTENING,
            )
        finally:
            asr_session = getattr(handle, "asr_session", None)
            if asr_session is not None:
                close = getattr(asr_session, "close", None)
                if callable(close):
                    await close()

    async def _speak(self, handle: LiveTurnHandle, text: str) -> str:
        handle.audio_chunks = 0
        handle.visual_frames = 0
        handle.visual_status = "not_started"
        if self._tts_service is None:
            self._emit(
                "tts_fallback",
                self.state,
                turn_id=handle.turn_id,
                component="tts",
                status="text_fallback",
                error_code="TTS_UNAVAILABLE",
            )
            self._emit_visual_fallback(handle, "tts_unavailable")
            return "text_fallback"
        try:
            health = dict(self._tts_service.health())
        except Exception:
            health = {"status": "unavailable"}
        if health.get("status") != "available":
            self._emit(
                "tts_fallback",
                self.state,
                turn_id=handle.turn_id,
                component="tts",
                status="text_fallback",
                error_code=str(health.get("reason_code", "TTS_UNAVAILABLE")),
            )
            self._emit_visual_fallback(handle, "tts_unavailable")
            return "text_fallback"
        # The real B06 service is consumed through its public start/events
        # boundary.  Audio payloads never enter LiveEvent; only safe metadata
        # is exposed to the UI/replay trace.
        from tts import TTSRequest

        run = None
        try:
            run = await self._tts_service.start(TTSRequest(text, request_id=handle.turn_id))
            handle.tts_run = run
            self._emit("tts_started", self.state, turn_id=handle.turn_id, component="tts")
            async for event in run.events():
                if event.event == "audio_chunk" and event.chunk is not None:
                    handle.audio_chunks += 1
                    self._emit(
                        "audio_chunk",
                        self.state,
                        turn_id=handle.turn_id,
                        component="tts",
                        status="audio",
                        metadata={
                            "sample_rate": event.chunk.sample_rate,
                            "sample_count": event.chunk.sample_count,
                            "sentence_index": event.chunk.sentence_index,
                            "chunk_index": event.chunk.chunk_index,
                        },
                    )
                    await self._render_visual(handle, event.chunk)
            result = await run.wait()
        except asyncio.CancelledError:
            if run is not None:
                await self._cancel_tts_and_wait(run)
            raise
        except Exception:
            self._emit(
                "tts_fallback",
                self.state,
                turn_id=handle.turn_id,
                component="tts",
                status="text_fallback",
                error_code="TTS_UNAVAILABLE",
            )
            self._emit_visual_fallback(handle, "tts_connector_error")
            return "text_fallback"
        finally:
            handle.tts_run = None
            release_turn = getattr(self._visual_driver, "release_turn", None)
            if callable(release_turn):
                release_turn(handle.turn_id)
        if result.status == "completed":
            self._emit("tts_completed", self.state, turn_id=handle.turn_id, component="tts")
            return "completed"
        self._emit(
            "tts_fallback",
            self.state,
            turn_id=handle.turn_id,
            component="tts",
            status="text_fallback",
            error_code=result.error_code or "TTS_UNAVAILABLE",
        )
        return "text_fallback"

    async def _cancel_tts_and_wait(self, run: Any) -> None:
        cancel_tts = getattr(run, "cancel", None)
        if callable(cancel_tts):
            cancel_tts()
        wait_tts = getattr(run, "wait", None)
        if callable(wait_tts):
            try:
                await wait_tts()
            except Exception:
                # TTS fallback remains truthful; the session must not leak a
                # connector task merely because its cancellation is noisy.
                pass

    def _emit_visual_fallback(self, handle: LiveTurnHandle, reason: str) -> None:
        handle.visual_status = "original_static_or_clip"
        self._emit(
            "visual_fallback",
            self.state,
            turn_id=handle.turn_id,
            component="visual",
            status="original_static_or_clip",
            error_code="VISUAL_UNAVAILABLE",
            metadata={"fallback_reason": reason, "media_written": False},
        )

    @staticmethod
    def _chunk_pcm16(chunk: Any) -> bytes:
        samples = getattr(chunk, "samples", ())
        encoded = bytearray()
        for sample in samples:
            value = float(sample)
            if value != value or value in {float("inf"), float("-inf")}:
                raise ValueError("visual_audio_sample_invalid")
            integer = max(-32768, min(32767, round(value * 32767)))
            encoded.extend(struct.pack("<h", integer))
        if not encoded:
            raise ValueError("visual_audio_empty")
        return bytes(encoded)

    def _timed_visual_request(self, handle: LiveTurnHandle, chunk: Any) -> Any:
        sample_rate = int(getattr(chunk, "sample_rate"))
        sample_count = int(getattr(chunk, "sample_count"))
        if sample_rate <= 0 or sample_count <= 0:
            raise ValueError("visual_audio_timing_invalid")
        pcm16 = self._chunk_pcm16(chunk)
        if len(pcm16) != sample_count * 2:
            raise ValueError("visual_audio_sample_count_invalid")
        start = float(getattr(handle, "visual_audio_seconds", 0.0))
        end = start + sample_count / sample_rate
        request = replace(
            self._visual_request,
            turn_id=handle.turn_id,
            chunk_id=f"{int(getattr(chunk, 'sentence_index'))}:{int(getattr(chunk, 'chunk_index'))}",
            audio_pcm16=pcm16,
            sample_rate=sample_rate,
            sample_count=sample_count,
            audio_start_seconds=start,
            audio_end_seconds=end,
            pts_seconds=start,
        )
        handle.visual_audio_seconds = end
        return request

    async def _render_visual(self, handle: LiveTurnHandle, chunk: Any) -> None:
        if self._visual_driver is None or self._visual_request is None:
            self._emit_visual_fallback(handle, "original_frame_not_provided")
            return
        try:
            request = self._timed_visual_request(handle, chunk)
            result = await asyncio.to_thread(self._visual_driver.render, request)
            public = result.public_dict()
            if public.get("status") == "DRIVEN":
                handle.visual_status = "driven"
                handle.visual_frames += 1
                self._emit(
                    "visual_frame",
                    self.state,
                    turn_id=handle.turn_id,
                    component="visual",
                    status="driven",
                    metadata={"media_written": False, "source": "in_memory_original_composite"},
                )
            else:
                self._emit_visual_fallback(handle, str(public.get("fallback_reason", "driver_unavailable")))
        except Exception:
            self._emit_visual_fallback(handle, "driver_error")

    def _build_messages(self, text: str, turn_id: str) -> tuple[list[dict[str, str]], str]:
        memory_status = "session-only"
        content = text
        memory_text = ""
        try:
            prompt = self._memory_prompt.build(
                text,
                max_chars=min(self.config.max_input_chars, 2400),
            )
            if prompt.status == "available":
                memory_status = "available"
            elif prompt.status == "unavailable":
                self._emit(
                    "memory_fallback",
                    self.state,
                    turn_id=turn_id,
                    component="memory",
                    status="session-only",
                    error_code="MEMORY_UNAVAILABLE",
                )
            if prompt.text:
                memory_text = prompt.text
        except Exception:
            self._emit(
                "memory_fallback",
                self.state,
                turn_id=turn_id,
                component="memory",
                status="session-only",
                error_code="MEMORY_UNAVAILABLE",
            )
        messages: list[dict[str, str]] = []
        if self._persona_provider is not None:
            snapshot = self._persona_provider.snapshot()
            if isinstance(snapshot, PersonaV2Snapshot):
                history = tuple(
                    fragment
                    for index, (previous_user, previous_reply) in enumerate(
                        self._history[-self.config.max_history_turns :]
                    )
                    for fragment in (
                        UntrustedFragment(
                            f"turn-{index}-user",
                            f"user_message: {previous_user}",
                        ),
                        UntrustedFragment(
                            f"turn-{index}-assistant",
                            f"character_reply: {previous_reply}",
                        ),
                    )
                )
                evidence = (
                    (UntrustedFragment("memory", memory_text),)
                    if memory_text
                    else ()
                )
                context = ReplyContext.create(
                    ReplyMode.FUTURE_IM,
                    trusted_time=TrustedTime(datetime.now(timezone.utc)),
                    future_im_enabled=True,
                )
                return (
                    list(
                        assemble_persona(
                            snapshot,
                            context,
                            user_input=text,
                            max_units=self.config.max_input_chars,
                            history=history,
                            evidence_summaries=evidence,
                        ).to_messages()
                    ),
                    memory_status,
                )
            try:
                system_prompt = str(getattr(snapshot, "system_prompt", ""))
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
            except Exception:
                pass
        if memory_text:
            content = f"{text}\n\n{memory_text}"
        for previous_user, previous_reply in self._history[-self.config.max_history_turns :]:
            messages.append({"role": "user", "content": previous_user})
            messages.append({"role": "assistant", "content": previous_reply})
        messages.append({"role": "user", "content": content})
        return messages, memory_status

    def _finish(self, handle: LiveTurnHandle, result: LiveTurnResult, state: LiveSessionState) -> None:
        if not handle.result_future.done():
            handle.result_future.set_result(result)
        self.state = state
        event_name = (
            "turn_completed"
            if result.status == "completed"
            else "turn_degraded"
            if result.status in {"degraded", "text_fallback"}
            else "turn_timeout"
            if result.status == "timeout"
            else "turn_failed"
        )
        self._emit(
            event_name,
            state,
            turn_id=handle.turn_id,
            component="session",
            status=result.status,
            error_code=result.error_code,
        )
        if self._active_turn is handle:
            self._active_turn = None

    def _failed_handle(self, code: str) -> LiveTurnHandle:
        handle = LiveTurnHandle(kind="text")
        handle.result_future = asyncio.get_running_loop().create_future()
        handle.result_future.set_result(LiveTurnResult(handle.turn_id, "failed", error_code=code))
        return handle

    def _active_done(self) -> bool:
        return self._active_turn is None or self._active_turn.result_future.done()

    def _ensure_open(self) -> None:
        if self._closed or self.state is LiveSessionState.CLOSED:
            raise LiveError("LIVE_SESSION_CLOSED")

    def _emit(
        self,
        event: str,
        state: LiveSessionState,
        *,
        turn_id: str | None = None,
        component: str | None = None,
        status: str | None = None,
        error_code: str | None = None,
        text_present: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> LiveEvent:
        terminal = event in {
            "turn_completed",
            "turn_degraded",
            "turn_failed",
            "turn_timeout",
            "turn_cancelled",
            "turn_interrupted",
            "session_closed",
        }
        if self._events.full():
            self._dropped_events += 1
            if terminal:
                self._sequence += 1
                backpressure = LiveEvent(
                    session_id=self.session_id,
                    sequence=self._sequence,
                    timestamp_ms=monotonic_ms(self._started),
                    event="backpressure",
                    state=state,
                    turn_id=turn_id,
                    component="session",
                    status="dropped_oldest",
                    error_code="LIVE_BACKPRESSURE",
                    metadata={"dropped_events": self._dropped_events},
                )
                self._timeline.append(backpressure)
                if self.config.max_events >= 2:
                    while not self._events.empty():
                        try:
                            self._events.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    self._events.put_nowait(backpressure)
        if self._events.full():
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._sequence += 1
        safe_metadata = dict(metadata or {})
        if self._dropped_events:
            safe_metadata.setdefault("dropped_events", self._dropped_events)
        item = LiveEvent(
            session_id=self.session_id,
            sequence=self._sequence,
            timestamp_ms=monotonic_ms(self._started),
            event=event,
            state=state,
            turn_id=turn_id,
            component=component,
            status=status,
            error_code=error_code,
            text_present=text_present,
            metadata=safe_metadata,
        )
        try:
            self._events.put_nowait(item)
        except asyncio.QueueFull as exc:
            raise LiveError("LIVE_BACKPRESSURE", retryable=True) from exc
        self._timeline.append(item)
        return item


__all__ = ["LiveSession"]
