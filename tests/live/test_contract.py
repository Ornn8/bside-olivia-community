from __future__ import annotations

import asyncio

import numpy as np
import pytest

from llm_gateway import Gateway, GatewayResponse, UnconfiguredAdapter
from memory_port import NullMemoryPort
from memory_port import CONVERSATION_MEMORY, LEGACY_LETTERS, MemoryRecord
from asr.contracts import AsrEvent, EventClock
from tts import TTSConfig, TTSService
from tts.contracts import AudioChunk, TTSResult, TTSStreamEvent
from tts.registry import TTSProviderRegistry
from visual_driver import VisualDriver
from visual_driver import OriginalVisualFrame, VisualDriverRequest

from live import LiveConfig, LiveError, LiveService, replay_trace
from asr.fallback import TextFallbackProvider


def test_health_never_reports_ready_when_llm_is_unavailable() -> None:
    service = LiveService(
        gateway=UnconfiguredAdapter(),
        memory_port=NullMemoryPort(),
        asr_provider=TextFallbackProvider(),
        tts_service=TTSService(TTSConfig(provider="not-installed")),
        visual_driver=VisualDriver(),
    )

    health = service.health()

    assert health["status"] == "UNAVAILABLE"
    assert health["ready"] is False
    assert health["components"]["llm"]["status"] == "UNAVAILABLE"
    assert health["components"]["asr"]["fallback"] == "text_input"
    assert health["components"]["tts"]["status"] == "UNAVAILABLE"
    assert health["components"]["visual"]["fallback"] == "original_static_or_clip"


def test_health_components_expose_the_versioned_component_contract() -> None:
    health = LiveService.from_environment(
        environ={"OLIVIA_LLM_PROVIDER": "mock"}
    ).health()

    required = {"status", "ready", "provider", "fallback", "reason_code"}

    assert all(required <= set(component) for component in health["components"].values())


def test_injected_llm_without_health_contract_is_not_reported_ready() -> None:
    health = LiveService(gateway=Gateway()).health()

    llm = health["components"]["llm"]

    assert llm["status"] == "DEGRADED"
    assert llm["ready"] is False
    assert llm["reason_code"] == "LLM_REACHABILITY_UNVERIFIED"


class ReplyGateway(Gateway):
    async def complete(self, messages, *, request_id=None):
        return GatewayResponse("local reply", request_id or "request", "test-provider", "test-model")


def test_public_live_events_are_text_free_and_schema_shaped() -> None:
    async def exercise():
        service = LiveService(gateway=ReplyGateway())
        session = await service.start_session("user-a")
        result = await session.send_text("private input")
        events = []
        while True:
            event = await session.next_event()
            events.append(event)
            if event.event == "turn_completed":
                break
        await service.stop()
        return result, events

    result, events = asyncio.run(exercise())

    assert result.completed
    expected = {
        "session_id",
        "sequence",
        "timestamp_ms",
        "event",
        "state",
        "turn_id",
        "component",
        "status",
        "error_code",
        "metadata",
        "text_present",
    }
    for event in events:
        public = event.to_dict()
        assert set(public) == expected
        assert "text" not in public
        assert not hasattr(event, "text")
    assert any(event.event == "text_output" and event.text_present for event in events)


def test_health_public_payload_matches_strict_contract_without_environment_payload() -> None:
    health = LiveService.from_environment(environ={"OLIVIA_LLM_PROVIDER": "mock"}).health()

    assert set(health) == {"status", "ready", "components", "network_called"}
    assert "environment" not in health
    required = {"status", "ready", "provider", "fallback", "reason_code"}
    allowed = required | {"network_called", "source"}
    assert all(required <= set(component) <= allowed for component in health["components"].values())


class SlowReplyGateway(Gateway):
    async def complete(self, messages, *, request_id=None):
        await asyncio.sleep(1)
        return GatewayResponse("late reply", request_id or "request", "slow", "model")


class CancellableGateway(Gateway):
    def __init__(self, cancelled: asyncio.Event) -> None:
        self.cancelled = cancelled

    async def complete(self, messages, *, request_id=None):
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return GatewayResponse("late reply", request_id or "request", "slow", "model")


class DelayedCancelGateway(Gateway):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cleaned = asyncio.Event()

    async def complete(self, messages, *, request_id=None):
        self.started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(0.02)
            self.cleaned.set()
            raise
        return GatewayResponse("late reply", request_id or "request", "slow", "model")


class BargeInGateway(Gateway):
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, request_id=None):
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(1)
            return GatewayResponse("stale reply", request_id or "request", "slow", "model")
        return GatewayResponse("barge-in reply", request_id or "request", "fast", "model")


class BurstGateway(Gateway):
    stream_enabled = True

    async def stream(self, messages, *, request_id=None):
        for index in range(200):
            yield type("Delta", (), {"text": str(index), "request_id": request_id or "request", "index": index})()


class InspectingGateway(Gateway):
    def __init__(self) -> None:
        self.messages = []

    async def complete(self, messages, *, request_id=None):
        self.messages = list(messages)
        return GatewayResponse("context reply", request_id or "request", "inspect", "model")


class MemoryFixture:
    enabled = True

    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.remembered = []

    def status(self):
        if self.unavailable:
            return {"status": "unavailable", "provider": "fixture"}
        return {
            "status": "available",
            "provider": "fixture",
            "conversation_enabled": True,
        }

    def search(self, query, *, domains=None, limit=8):
        if self.unavailable:
            raise RuntimeError("fixture memory unavailable")
        return [
            MemoryRecord(
                memory_id="letter-1",
                domain=LEGACY_LETTERS,
                text="synthetic legacy reference",
                source="fixture",
                created_at=1,
                provenance={"read_only": True},
            ),
            MemoryRecord(
                memory_id="memory-1",
                domain=CONVERSATION_MEMORY,
                text="synthetic conversation memory",
                source="fixture",
                created_at=1,
                provenance={"current_conversation": True},
            ),
        ]

    def remember_conversation(self, summary, *, facts=(), ttl_seconds=None, metadata=None):
        self.remembered.append((summary, tuple(facts)))
        return "memory-2"


class PersonaFixture:
    def snapshot(self):
        return type("Snapshot", (), {"system_prompt": "synthetic persona policy"})()


class FakeAsrSession:
    def __init__(self) -> None:
        self.clock = EventClock("fake-asr")
        self.events_queue = asyncio.Queue()
        self.closed = False

    async def send_audio(self, pcm16):
        assert pcm16

    async def commit(self):
        await self.events_queue.put(
            self.clock.emit("partial", provider="fake-asr", text="hel")
        )
        await self.events_queue.put(
            self.clock.emit("final", provider="fake-asr", text="hello from audio")
        )

    async def cancel(self):
        await self.events_queue.put(
            self.clock.emit("canceled", provider="fake-asr", code="ASR_CANCELED")
        )

    async def events(self):
        while not self.closed:
            yield await self.events_queue.get()

    async def close(self):
        self.closed = True


class NativeAsrFixture:
    def __init__(self) -> None:
        self.sessions = []

    def status(self):
        return {"status": "available", "ready": True, "is_asr": True, "provider": "fake-asr"}

    async def open_session(self):
        session = FakeAsrSession()
        self.sessions.append(session)
        return session


class ReconnectingAsrFixture(NativeAsrFixture):
    def __init__(self) -> None:
        super().__init__()
        self.open_calls = 0

    async def open_session(self):
        self.open_calls += 1
        if self.open_calls == 1:
            raise RuntimeError("synthetic disconnect")
        return await super().open_session()


class BackpressureAsrSession(FakeAsrSession):
    async def commit(self):
        await self.events_queue.put(
            self.clock.emit("error", provider="fake-asr", code="ASR_BACKPRESSURE")
        )


class BackpressureAsrFixture(NativeAsrFixture):
    async def open_session(self):
        session = BackpressureAsrSession()
        self.sessions.append(session)
        return session


class DelayedCloseAsrSession(FakeAsrSession):
    async def close(self):
        await asyncio.sleep(0.02)
        await super().close()


class DelayedCloseAsrFixture(NativeAsrFixture):
    async def open_session(self):
        session = DelayedCloseAsrSession()
        self.sessions.append(session)
        return session


class LiveTtsFixture:
    name = "live-fixture"
    license_id = "MIT"

    def __init__(self, config):
        self.config = config

    def health(self):
        return {"status": "available", "provider": self.name, "license_id": self.license_id}

    def stream_sentence(self, text, request, sentence_index):
        yield AudioChunk((0.1, -0.1), 16000, sentence_index, 0)

    def close(self):
        return None


class BrokenTtsService:
    def health(self):
        return {"status": "available", "provider": "broken-fixture"}

    async def start(self, _request):
        raise RuntimeError("synthetic tts connector failure")

    def close(self):
        return None


class DelayedCancelTtsRun:
    def __init__(self, request) -> None:
        self.request = request
        self.events_queue = asyncio.Queue()
        self.cancelled = asyncio.Event()
        self.cleaned = asyncio.Event()
        self.task = asyncio.create_task(self._produce())

    async def _produce(self) -> None:
        await self.events_queue.put(TTSStreamEvent(self.request.request_id, "accepted", 0.0))
        await self.cancelled.wait()
        await asyncio.sleep(0.02)
        self.cleaned.set()
        await self.events_queue.put(
            TTSStreamEvent(self.request.request_id, "cancelled", 20.0, error_code="TTS_CANCELLED")
        )

    def cancel(self) -> bool:
        self.cancelled.set()
        return True

    async def wait(self) -> TTSResult:
        await self.cleaned.wait()
        return TTSResult(
            request_id=self.request.request_id,
            status="cancelled",
            provider="delayed-fixture",
            error_code="TTS_CANCELLED",
        )

    async def events(self):
        while True:
            event = await self.events_queue.get()
            yield event
            if event.event == "cancelled":
                return


class DelayedCancelTtsService:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.run = None

    def health(self):
        return {"status": "available", "provider": "delayed-fixture"}

    async def start(self, request):
        self.run = DelayedCancelTtsRun(request)
        self.started.set()
        return self.run

    def close(self):
        return None


def live_tts_registry() -> TTSProviderRegistry:
    registry = TTSProviderRegistry()
    registry.register("live-fixture", LiveTtsFixture, license_id="MIT")
    return registry


def test_text_turn_uses_llm_and_degrades_tts_to_text() -> None:
    async def exercise():
        service = LiveService(gateway=ReplyGateway())
        session = await service.start_session("user-a")
        result = await session.send_text("hello", idempotency_key="turn-1")
        events = []
        while True:
            event = await session.next_event()
            events.append(event)
            if event.event == "turn_completed":
                break
        await session.close()
        events.append(await session.next_event())
        return result, events

    result, events = asyncio.run(exercise())

    assert result.status == "completed"
    assert result.text == "local reply"
    assert result.text_source == "llm"
    assert result.tts_status == "text_fallback"
    assert [event.event for event in events][-5:] == [
        "text_output",
        "tts_fallback",
        "visual_fallback",
        "turn_completed",
        "session_closed",
    ]
    assert [event.sequence for event in events] == sorted(event.sequence for event in events)


def test_idempotency_reuses_one_turn_and_owner_isolation_is_explicit() -> None:
    async def exercise():
        service = LiveService(gateway=ReplyGateway())
        session = await service.start_session("user-a")
        first_handle = await session.submit_text("same", idempotency_key="same-key")
        first = await first_handle.wait()
        second_handle = await session.submit_text("same", idempotency_key="same-key")
        second = await second_handle.wait()
        conflict = await (await session.submit_text("different", idempotency_key="same-key")).wait()
        try:
            service.get_session(session.session_id, "user-b")
        except LiveError as exc:
            forbidden = exc.code
        else:  # pragma: no cover - assertion documents the required boundary
            forbidden = "missing"
        await service.stop()
        return first_handle, second_handle, first, second, conflict, forbidden

    first_handle, second_handle, first, second, conflict, forbidden = asyncio.run(exercise())

    assert second_handle is first_handle
    assert second == first
    assert conflict.error_code == "IDEMPOTENCY_CONFLICT"
    assert forbidden == "LIVE_SESSION_FORBIDDEN"


def test_llm_unavailable_emits_explicit_error_and_safe_static_not_model_reply() -> None:
    async def exercise():
        service = LiveService(gateway=UnconfiguredAdapter())
        session = await service.start_session("user-a")
        result = await session.send_text("hello")
        events = []
        while True:
            event = await session.next_event()
            events.append(event)
            if event.event == "turn_degraded":
                break
        await service.stop()
        return result, events

    result, events = asyncio.run(exercise())

    assert result.status == "degraded"
    assert result.error_code == "LIVE_LLM_UNAVAILABLE"
    assert result.text_source == "safe_static"
    assert result.text
    assert any(event.event == "llm_error" and event.error_code == "LIVE_LLM_UNAVAILABLE" for event in events)
    safe = next(event for event in events if event.event == "safe_static_output")
    assert safe.status == "safe_static"
    assert safe.text_present is True
    assert safe.metadata["model_generated"] is False


def test_cancel_turn_is_visible_and_does_not_complete() -> None:
    async def exercise():
        service = LiveService(gateway=SlowReplyGateway())
        session = await service.start_session("user-a")
        handle = await session.submit_text("cancel me")
        await asyncio.sleep(0.01)
        cancelled = await session.cancel_turn(handle.turn_id)
        result = await handle.wait()
        events = []
        while True:
            event = await session.next_event()
            events.append(event)
            if event.event == "turn_cancelled":
                break
        await service.stop()
        return cancelled, result, events

    cancelled, result, events = asyncio.run(exercise())

    assert cancelled is True
    assert result.status == "cancelled"
    assert result.error_code == "LIVE_CANCELED"
    assert any(event.event == "turn_cancelled" for event in events)
    assert not any(event.event == "turn_completed" for event in events)


def test_cancel_propagates_to_the_active_llm_request() -> None:
    async def exercise():
        provider_cancelled = asyncio.Event()
        service = LiveService(gateway=CancellableGateway(provider_cancelled))
        session = await service.start_session("user-a")
        handle = await session.submit_text("cancel provider")
        await asyncio.sleep(0.01)
        await session.cancel_turn(handle.turn_id)
        result = await handle.wait()
        await asyncio.wait_for(provider_cancelled.wait(), timeout=0.2)
        await service.stop()
        return result

    result = asyncio.run(exercise())

    assert result.status == "cancelled"


def test_llm_timeout_is_a_truthful_timeout_and_session_recovers() -> None:
    async def exercise():
        service = LiveService(gateway=SlowReplyGateway(), config=LiveConfig(turn_timeout_seconds=0.05))
        session = await service.start_session("user-a")
        result = await session.send_text("timeout")
        events = []
        while True:
            event = await session.next_event()
            events.append(event)
            if event.event == "turn_timeout":
                break
        await service.stop()
        return result, events

    result, events = asyncio.run(exercise())

    assert result.status == "timeout"
    assert result.error_code == "LIVE_TIMEOUT"
    assert result.text == ""
    assert any(event.event == "turn_timeout" and event.error_code == "LIVE_TIMEOUT" for event in events)


def test_new_text_input_interrupts_speaking_or_thinking_and_new_turn_wins() -> None:
    async def exercise():
        service = LiveService(gateway=BargeInGateway())
        session = await service.start_session("user-a")
        first = await session.submit_text("first")
        await asyncio.sleep(0.01)
        second = await session.send_text("second")
        first_result = await first.wait()
        events = []
        while True:
            event = await session.next_event()
            events.append(event)
            if event.event == "turn_interrupted":
                # The interrupt event may be queued before the second turn's
                # completion; drain until the replacement turn is complete.
                continue
            if event.event == "turn_completed" and event.turn_id == second.turn_id:
                break
        await service.stop()
        return first_result, second, events

    first_result, second, events = asyncio.run(exercise())

    assert first_result.status == "interrupted"
    assert second.status == "completed"
    assert second.text == "barge-in reply"
    assert any(event.event == "interrupted" for event in events)
    assert not any(event.event == "turn_completed" and event.turn_id != second.turn_id for event in events)


def test_bounded_event_stream_reports_backpressure_without_hanging_turn() -> None:
    async def exercise():
        service = LiveService(gateway=BurstGateway(), config=LiveConfig(max_events=4))
        session = await service.start_session("user-a")
        result = await session.send_text("burst")
        events = []
        while True:
            event = await session.next_event()
            events.append(event)
            if event.event == "turn_completed":
                break
        await service.stop()
        return result, events

    result, events = asyncio.run(exercise())

    assert result.status == "completed"
    assert any(event.event == "backpressure" and event.error_code == "LIVE_BACKPRESSURE" for event in events)


def test_single_slot_queue_keeps_terminal_visibility_and_trace_backpressure() -> None:
    async def exercise():
        service = LiveService(gateway=BurstGateway(), config=LiveConfig(max_events=1))
        session = await service.start_session("user-a")
        result = await session.send_text("single slot")
        terminal = await asyncio.wait_for(_next_event_named(session, "turn_completed"), timeout=0.2)
        trace = session.trace()
        await service.stop()
        return result, terminal, trace

    result, terminal, trace = asyncio.run(exercise())

    assert result.status == "completed"
    assert terminal.error_code is None
    assert any(item["event"] == "backpressure" for item in trace)
    assert any(item["error_code"] == "LIVE_BACKPRESSURE" for item in trace if item["event"] == "backpressure")


def test_b03_b04_prompt_integration_is_read_only_and_records_memory_status() -> None:
    async def exercise():
        gateway = InspectingGateway()
        memory = MemoryFixture()
        service = LiveService(gateway=gateway, memory_port=memory, persona_provider=PersonaFixture())
        session = await service.start_session("user-a")
        result = await session.send_text("synthetic question")
        await service.stop()
        return gateway.messages, memory.remembered, result

    messages, remembered, result = asyncio.run(exercise())

    assert messages[0] == {"role": "system", "content": "synthetic persona policy"}
    assert messages[-1]["role"] == "user"
    assert "<MEMORY_CONTEXT_UNTRUSTED_DATA>" in messages[-1]["content"]
    assert "synthetic legacy reference" in messages[-1]["content"]
    assert result.memory_status == "available"
    assert remembered


def test_memory_failure_is_session_only_and_never_blocks_llm() -> None:
    async def exercise():
        gateway = InspectingGateway()
        service = LiveService(
            gateway=gateway,
            memory_port=MemoryFixture(unavailable=True),
            persona_provider=PersonaFixture(),
        )
        session = await service.start_session("user-a")
        result = await session.send_text("still works")
        events = []
        while True:
            event = await session.next_event()
            events.append(event)
            if event.event == "turn_completed":
                break
        await service.stop()
        return result, events

    result, events = asyncio.run(exercise())

    assert result.status == "completed"
    assert result.memory_status == "session-only"
    assert any(event.event == "memory_fallback" and event.status == "session-only" for event in events)


def test_b05_audio_stream_reaches_llm_after_partial_and_final_events() -> None:
    async def exercise():
        asr = NativeAsrFixture()
        service = LiveService(gateway=ReplyGateway(), asr_provider=asr)
        session = await service.start_session("user-a")
        handle = await session.start_audio_turn(idempotency_key="audio-1")
        await session.send_audio(handle.turn_id, b"\x01\x00")
        await session.commit_audio(handle.turn_id)
        result = await handle.wait()
        events = []
        while True:
            event = await session.next_event()
            events.append(event)
            if event.event == "turn_completed":
                break
        await service.stop()
        return result, events

    result, events = asyncio.run(exercise())

    assert result.status == "completed"
    assert result.text == "local reply"
    assert any(event.event == "asr_partial" for event in events)
    assert any(event.event == "asr_final" and event.text_present for event in events)


def test_asr_unavailable_is_text_input_fallback_not_fake_transcription() -> None:
    async def exercise():
        service = LiveService(gateway=ReplyGateway(), asr_provider=TextFallbackProvider())
        session = await service.start_session("user-a")
        handle = await session.start_audio_turn()
        result = await handle.wait()
        event = await session.next_event()
        while event.event != "asr_fallback":
            event = await session.next_event()
        await service.stop()
        return result, event

    result, event = asyncio.run(exercise())

    assert result.status == "text_fallback"
    assert result.error_code == "ASR_UNAVAILABLE"
    assert result.text == ""
    assert result.text_source == "text_input"
    assert event.status == "text_input"
    assert event.text_present is False


def test_asr_open_failure_reconnects_before_accepting_audio() -> None:
    async def exercise():
        asr = ReconnectingAsrFixture()
        service = LiveService(
            gateway=ReplyGateway(),
            asr_provider=asr,
            config=LiveConfig(reconnect_attempts=1),
        )
        session = await service.start_session("user-a")
        handle = await session.start_audio_turn()
        await session.send_audio(handle.turn_id, b"\x01\x00")
        await session.commit_audio(handle.turn_id)
        result = await handle.wait()
        events = []
        while True:
            event = await session.next_event()
            events.append(event)
            if event.event == "turn_completed":
                break
        await service.stop()
        return result, events, asr.open_calls

    result, events, open_calls = asyncio.run(exercise())

    assert result.status == "completed"
    assert open_calls == 2
    assert any(event.event == "reconnecting" and event.component == "asr" for event in events)


def test_audio_cancel_resolves_turn_and_closes_the_asr_session() -> None:
    async def exercise():
        asr = NativeAsrFixture()
        service = LiveService(gateway=ReplyGateway(), asr_provider=asr)
        session = await service.start_session("user-a")
        handle = await session.start_audio_turn()
        await asyncio.wait_for(handle.audio_ready.wait(), timeout=0.2)
        cancelled = await session.cancel_turn(handle.turn_id)
        result = await asyncio.wait_for(handle.wait(), timeout=0.2)
        await service.stop()
        return cancelled, result, asr.sessions[0].closed

    cancelled, result, closed = asyncio.run(exercise())

    assert cancelled is True
    assert result.status == "cancelled"
    assert result.error_code == "LIVE_CANCELED"
    assert closed is True


def test_audio_cancel_returns_after_asr_cleanup_finishes() -> None:
    async def exercise():
        asr = DelayedCloseAsrFixture()
        service = LiveService(gateway=ReplyGateway(), asr_provider=asr)
        session = await service.start_session("user-a")
        handle = await session.start_audio_turn()
        await asyncio.wait_for(handle.audio_ready.wait(), timeout=0.2)
        cancelled = await session.cancel_turn(handle.turn_id)
        closed_at_cancel_return = asr.sessions[0].closed
        result = await asyncio.wait_for(handle.wait(), timeout=0.2)
        await service.stop()
        return cancelled, closed_at_cancel_return, result

    cancelled, closed_at_cancel_return, result = asyncio.run(exercise())

    assert cancelled is True
    assert closed_at_cancel_return is True
    assert result.status == "cancelled"


def test_session_close_waits_for_llm_cancellation_cleanup() -> None:
    async def exercise():
        gateway = DelayedCancelGateway()
        service = LiveService(gateway=gateway)
        session = await service.start_session("user-a")
        handle = await session.submit_text("wait for llm cleanup")
        await asyncio.wait_for(gateway.started.wait(), timeout=0.2)
        await session.close()
        cleaned_at_close_return = gateway.cleaned.is_set()
        result = await asyncio.wait_for(handle.wait(), timeout=0.2)
        await service.stop()
        return cleaned_at_close_return, result

    cleaned_at_close_return, result = asyncio.run(exercise())

    assert cleaned_at_close_return is True
    assert result.status == "cancelled"
    assert result.error_code == "LIVE_CANCELED"


def test_asr_backpressure_finishes_with_a_visible_terminal_failure() -> None:
    async def exercise():
        service = LiveService(gateway=ReplyGateway(), asr_provider=BackpressureAsrFixture())
        session = await service.start_session("user-a")
        handle = await session.start_audio_turn()
        await asyncio.wait_for(handle.audio_ready.wait(), timeout=0.2)
        await session.commit_audio(handle.turn_id)
        result = await asyncio.wait_for(handle.wait(), timeout=0.2)
        terminal = await asyncio.wait_for(
            _next_event_named(session, "turn_failed"), timeout=0.2
        )
        await service.stop()
        return result, terminal

    result, terminal = asyncio.run(exercise())

    assert result.status == "failed"
    assert result.error_code == "ASR_BACKPRESSURE"
    assert terminal.error_code == "ASR_BACKPRESSURE"


def test_asr_open_failure_without_reconnect_finishes_with_visible_fallback() -> None:
    async def exercise():
        service = LiveService(
            gateway=ReplyGateway(),
            asr_provider=ReconnectingAsrFixture(),
            config=LiveConfig(reconnect_attempts=0),
        )
        session = await service.start_session("user-a")
        handle = await session.start_audio_turn()
        await asyncio.wait_for(handle.audio_ready.wait(), timeout=0.2)
        result = await asyncio.wait_for(handle.wait(), timeout=0.2)
        terminal = await asyncio.wait_for(
            _next_event_named(session, "turn_degraded"), timeout=0.2
        )
        await service.stop()
        return result, terminal

    result, terminal = asyncio.run(exercise())

    assert result.status == "text_fallback"
    assert result.error_code == "ASR_UNAVAILABLE"
    assert terminal.error_code == "ASR_UNAVAILABLE"


async def _next_event_named(session, event_name: str):
    while True:
        event = await session.next_event()
        if event.event == event_name:
            return event


def test_b06_tts_audio_is_streamed_as_safe_metadata_only() -> None:
    async def exercise():
        tts = TTSService(
            TTSConfig(provider="live-fixture", profile="live-fixture", fallback="unavailable"),
            registry=live_tts_registry(),
        )
        service = LiveService(gateway=ReplyGateway(), tts_service=tts)
        session = await service.start_session("user-a")
        result = await session.send_text("speak")
        events = []
        while True:
            event = await session.next_event()
            events.append(event)
            if event.event == "turn_completed":
                break
        await service.stop()
        tts.close()
        return result, events

    result, events = asyncio.run(exercise())

    assert result.status == "completed"
    assert result.tts_status == "completed"
    audio = next(event for event in events if event.event == "audio_chunk")
    assert audio.text_present is False
    assert audio.metadata["sample_rate"] == 16000
    assert "samples" not in audio.metadata


def test_tts_connector_failure_preserves_text_and_reports_text_fallback() -> None:
    async def exercise():
        service = LiveService(gateway=ReplyGateway(), tts_service=BrokenTtsService())
        session = await service.start_session("user-a")
        result = await session.send_text("tts failure")
        events = []
        while True:
            event = await session.next_event()
            events.append(event)
            if event.event == "turn_completed":
                break
        await service.stop()
        return result, events

    result, events = asyncio.run(exercise())

    assert result.status == "completed"
    assert result.text == "local reply"
    assert result.tts_status == "text_fallback"
    assert any(event.event == "tts_fallback" and event.error_code == "TTS_UNAVAILABLE" for event in events)
    assert any(event.event == "visual_fallback" for event in events)


def test_session_close_waits_for_tts_run_cleanup() -> None:
    async def exercise():
        tts = DelayedCancelTtsService()
        service = LiveService(gateway=ReplyGateway(), tts_service=tts)
        session = await service.start_session("user-a")
        handle = await session.submit_text("wait for tts cleanup")
        await asyncio.wait_for(tts.started.wait(), timeout=0.2)
        await session.close()
        cleaned_at_close_return = tts.run.cleaned.is_set()
        result = await asyncio.wait_for(handle.wait(), timeout=0.2)
        await service.stop()
        return cleaned_at_close_return, result

    cleaned_at_close_return, result = asyncio.run(exercise())

    assert cleaned_at_close_return is True
    assert result.status == "cancelled"


def test_b07_visual_driver_preserves_original_boundary_and_reports_driven_frame() -> None:
    manifest = {
        "schema_version": 1,
        "manifest_kind": "private_asset_manifest",
        "tool_version": "1",
        "roots": [{"alias": "fixture", "item_count": 1}],
        "items": [
            {
                "logical_id": "asset_4d1c44521d987dde8e6bd6bf0b0fd4f5",
                "root_alias": "fixture",
                "relative_path": "live.mp4",
                "extension": ".mp4",
                "category": "video",
                "bytes": 1,
                "sha256": "b" * 64,
                "media_metadata": {"image": None, "video": None, "audio": None},
                "probe_status": "unavailable",
                "reason": "synthetic_fixture",
            }
        ],
    }
    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    speaking = np.zeros((8, 8), dtype=np.uint8)
    speaking[3:5, 3:5] = 1
    original = OriginalVisualFrame(
        state_id="live",
        asset_ref="asset_4d1c44521d987dde8e6bd6bf0b0fd4f5",
        frame=frame,
        asset_manifest=manifest,
    )
    request = VisualDriverRequest(original=original, speaking_mask=speaking)

    def backend(backend_request):
        assert backend_request.turn_id
        assert backend_request.chunk_id == "0:0"
        assert backend_request.audio_pcm16
        assert backend_request.sample_rate == 16000
        assert backend_request.sample_count == 2
        assert backend_request.audio_start_seconds == 0.0
        assert backend_request.audio_end_seconds == 0.000125
        assert backend_request.pts_seconds == 0.0
        candidate = backend_request.original.frame.copy()
        candidate[3:5, 3:5] = 255
        return candidate

    async def exercise():
        tts = TTSService(
            TTSConfig(provider="live-fixture", profile="live-fixture", fallback="unavailable"),
            registry=live_tts_registry(),
        )
        service = LiveService(
            gateway=ReplyGateway(),
            tts_service=tts,
            visual_driver=VisualDriver(backend),
            visual_request=request,
        )
        session = await service.start_session("user-a")
        result = await session.send_text("visual")
        events = []
        while True:
            event = await session.next_event()
            events.append(event)
            if event.event == "turn_completed":
                break
        await service.stop()
        tts.close()
        return result, events

    result, events = asyncio.run(exercise())

    assert result.visual_status == "driven"
    assert result.visual_frames == 1
    visual = next(event for event in events if event.event == "visual_frame")
    assert visual.metadata["media_written"] is False
    assert "frame" not in visual.metadata


def test_trace_is_replayable_strictly_ordered_and_does_not_store_turn_text() -> None:
    async def exercise():
        service = LiveService(gateway=ReplyGateway())
        session = await service.start_session("user-a")
        result = await session.send_text("private synthetic phrase")
        trace = session.trace()
        await service.stop()
        return result, trace

    result, trace = asyncio.run(exercise())

    replayed = replay_trace(trace)
    assert result.text == "local reply"
    assert trace
    assert [item["sequence"] for item in replayed] == sorted(item["sequence"] for item in replayed)
    assert all(item["text_present"] is True for item in replayed if item["event"] in {"llm_delta", "text_output"})
    encoded = repr(trace)
    assert "private synthetic phrase" not in encoded
    assert "local reply" not in encoded
    assert all("owner_id" not in item for item in replayed)


def test_replay_trace_rejects_string_audio_and_frame_payloads() -> None:
    unsafe = {
        "session_id": "session",
        "sequence": 0,
        "timestamp_ms": 0,
        "event": "visual_frame",
        "state": "speaking",
        "text_present": False,
        "metadata": {
            "audio_payload": "encoded-audio",
            "frame_payload": "encoded-frame",
        },
    }

    with pytest.raises(ValueError):
        replay_trace([unsafe])


def test_replay_trace_rejects_private_metadata_variants_and_paths() -> None:
    base = {
        "session_id": "session-1",
        "sequence": 1,
        "timestamp_ms": 1.0,
        "event": "safe",
        "state": "listening",
        "text_present": False,
        "metadata": {},
    }

    for metadata in (
        {"raw-text": "private"},
        {"source_path": "D:/private/reference.wav"},
        {"note": "private text"},
    ):
        record = {**base, "metadata": metadata}
        with pytest.raises(ValueError):
            replay_trace([record])


def test_replay_trace_rejects_unknown_root_payload_fields() -> None:
    unsafe = {
        "session_id": "session-1",
        "sequence": 1,
        "timestamp_ms": 1.0,
        "event": "safe",
        "state": "listening",
        "text_present": False,
        "metadata": {},
        "note": "private text",
    }

    with pytest.raises(ValueError):
        replay_trace([unsafe])
