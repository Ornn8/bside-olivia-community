from __future__ import annotations

import asyncio

from llm_gateway import Gateway, GatewayDelta, GatewayError, GatewayResponse
from reply_orchestrator import (
    ReplyEventType,
    ReplyOrchestrator,
    ReplyRequest,
    ReplyState,
)


def run(coro):
    return asyncio.run(coro)


class StreamGateway(Gateway):
    stream_enabled = True

    async def stream(self, messages, *, request_id=None):
        yield GatewayDelta("one", request_id or "request")
        await asyncio.sleep(0)
        yield GatewayDelta(" two", request_id or "request")


class SlowGateway(Gateway):
    async def complete(self, messages, *, request_id=None):
        await asyncio.sleep(1)
        return GatewayResponse("late", request_id or "request", "mock", "model")


class BurstGateway(Gateway):
    stream_enabled = True

    async def stream(self, messages, *, request_id=None):
        for value in ("one", "two", "three"):
            yield GatewayDelta(value, request_id or "request")


class ErrorGateway(Gateway):
    def __init__(self, error: GatewayError):
        self.error = error

    async def complete(self, messages, *, request_id=None):
        raise self.error


def test_stream_success_emits_ordered_events_and_completed_result() -> None:
    async def exercise():
        orchestrator = ReplyOrchestrator(StreamGateway(), queue_size=2)
        run_handle = await orchestrator.start(ReplyRequest(content="synthetic"))
        events = []

        async def collect():
            async for event in run_handle.events():
                events.append(event)

        collector = asyncio.create_task(collect())
        result = await run_handle.wait()
        await collector
        return result, events

    result, events = run(exercise())
    assert result.state is ReplyState.COMPLETED
    assert result.text == "one two"
    assert [event.event for event in events] == [
        ReplyEventType.REQUEST_ACCEPTED,
        ReplyEventType.STREAM_DELTA,
        ReplyEventType.STREAM_DELTA,
        ReplyEventType.COMPLETED,
    ]
    assert [event.sequence for event in events] == list(range(len(events)))


def test_cancel_emits_cancelled_terminal_event() -> None:
    async def exercise():
        orchestrator = ReplyOrchestrator(SlowGateway(), timeout_seconds=2)
        run_handle = await orchestrator.start(ReplyRequest(content="synthetic"))
        accepted = await run_handle.queue.get()
        assert accepted.event is ReplyEventType.REQUEST_ACCEPTED
        assert run_handle.cancel() is True
        result = await run_handle.wait()
        cancelled = await run_handle.queue.get()
        return result, cancelled

    result, cancelled = run(exercise())
    assert result.state is ReplyState.CANCELLED
    assert cancelled.event is ReplyEventType.CANCELLED


def test_cancel_remains_visible_when_backpressure_queue_is_full() -> None:
    async def exercise():
        orchestrator = ReplyOrchestrator(BurstGateway(), queue_size=1)
        run_handle = await orchestrator.start(ReplyRequest(content="synthetic"))
        await asyncio.sleep(0)
        run_handle.cancel()
        result = await run_handle.wait()
        events = []
        while not run_handle.queue.empty():
            events.append(await run_handle.queue.get())
        return result, events

    result, events = run(exercise())
    assert result.state is ReplyState.CANCELLED
    assert events[-1].event is ReplyEventType.CANCELLED


def test_timeout_and_retryable_or_terminal_errors_are_explicit() -> None:
    async def exercise():
        timeout_orchestrator = ReplyOrchestrator(SlowGateway(), timeout_seconds=0.05)
        timeout = await timeout_orchestrator.run(ReplyRequest(content="timeout"))
        retry_orchestrator = ReplyOrchestrator(
            ErrorGateway(GatewayError("PROVIDER_RETRYABLE", retryable=True))
        )
        retry = await retry_orchestrator.run(ReplyRequest(content="retry"))
        terminal_orchestrator = ReplyOrchestrator(
            ErrorGateway(GatewayError("PROVIDER_PROTOCOL", retryable=False))
        )
        terminal = await terminal_orchestrator.run(ReplyRequest(content="terminal"))
        return timeout, retry, terminal

    timeout, retry, terminal = run(exercise())
    assert timeout.error_code == "LLM_TIMEOUT"
    assert timeout.retryable is True
    assert retry.error_code == "PROVIDER_RETRYABLE"
    assert retry.retryable is True
    assert terminal.error_code == "PROVIDER_PROTOCOL"
    assert terminal.retryable is False


def test_idempotency_reuses_result_and_conflict_is_terminal() -> None:
    async def exercise():
        orchestrator = ReplyOrchestrator(StreamGateway())
        first_request = ReplyRequest(content="same", idempotency_key="key-1")
        first = await orchestrator.run(first_request)
        second = await orchestrator.run(
            ReplyRequest(content="same", idempotency_key="key-1")
        )
        conflict = await orchestrator.run(
            ReplyRequest(content="different", idempotency_key="key-1")
        )
        return first, second, conflict

    first, second, conflict = run(exercise())
    assert second == first
    assert conflict.error_code == "IDEMPOTENCY_CONFLICT"
    assert conflict.state is ReplyState.FAILED


def test_invalid_role_is_rejected_without_provider_call() -> None:
    async def exercise():
        orchestrator = ReplyOrchestrator(StreamGateway())
        return await orchestrator.run(
            ReplyRequest(messages=[{"role": "tool", "content": "forbidden"}])
        )

    result = run(exercise())
    assert result.state is ReplyState.FAILED
    assert result.error_code == "INVALID_ROLE"
