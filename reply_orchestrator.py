"""Cancellable reply state machine with a bounded internal event stream."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Mapping, Sequence

from llm_gateway import Gateway, GatewayDelta, GatewayError, GatewayResponse, validate_messages


class ReplyEventType(str, Enum):
    REQUEST_ACCEPTED = "request_accepted"
    STREAM_DELTA = "stream_delta"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    RETRYABLE_ERROR = "retryable_error"
    TERMINAL_ERROR = "terminal_error"


class ReplyState(str, Enum):
    ACCEPTED = "accepted"
    STREAMING = "streaming"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


TERMINAL_EVENTS = frozenset(
    {
        ReplyEventType.COMPLETED,
        ReplyEventType.CANCELLED,
        ReplyEventType.RETRYABLE_ERROR,
        ReplyEventType.TERMINAL_ERROR,
    }
)


@dataclass(frozen=True)
class ReplyRequest:
    content: str | None = None
    messages: Sequence[Mapping[str, Any]] | None = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    idempotency_key: str | None = None
    max_input_chars: int = 10000

    def normalized_messages(self) -> tuple[dict[str, str], ...]:
        if self.messages is not None:
            return validate_messages(self.messages, max_input_chars=self.max_input_chars)
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("content is required")
        return validate_messages(
            [{"role": "user", "content": self.content}],
            max_input_chars=self.max_input_chars,
        )


@dataclass(frozen=True)
class ReplyEvent:
    request_id: str
    sequence: int
    event: ReplyEventType
    state: ReplyState
    timestamp: float
    delta: str = ""
    text: str = ""
    error_code: str | None = None
    retryable: bool = False


@dataclass(frozen=True)
class ReplyResult:
    request_id: str
    state: ReplyState
    text: str = ""
    error_code: str | None = None
    retryable: bool = False

    @property
    def completed(self) -> bool:
        return self.state is ReplyState.COMPLETED


class ReplyRun:
    def __init__(self, request: ReplyRequest, *, queue_size: int) -> None:
        self.request = request
        self.queue: asyncio.Queue[ReplyEvent] = asyncio.Queue(maxsize=queue_size)
        self._result: asyncio.Future[ReplyResult] = asyncio.get_running_loop().create_future()
        self.task: asyncio.Task[None] | None = None
        self.fingerprint = _request_fingerprint(request)
        self._run_draining = False

    async def wait(self) -> ReplyResult:
        return await self._result

    def cancel(self) -> bool:
        if self.task is None or self.task.done():
            return False
        self.task.cancel()
        return True

    async def events(self) -> AsyncIterator[ReplyEvent]:
        while True:
            event = await self.queue.get()
            yield event
            if event.event in TERMINAL_EVENTS:
                return


class ReplyOrchestrator:
    """Run one provider request at a time while preserving event semantics."""

    def __init__(
        self,
        gateway: Gateway,
        *,
        timeout_seconds: float = 30.0,
        queue_size: int = 64,
    ) -> None:
        self.gateway = gateway
        self.timeout_seconds = max(0.05, float(timeout_seconds))
        self.queue_size = max(1, int(queue_size))
        self._runs: dict[str, ReplyRun] = {}
        self._lock = asyncio.Lock()

    async def start(self, request: ReplyRequest) -> ReplyRun:
        key = request.idempotency_key or request.request_id
        async with self._lock:
            existing = self._runs.get(key)
            if existing is not None:
                if existing.fingerprint != _request_fingerprint(request):
                    conflict = ReplyRun(request, queue_size=self.queue_size)
                    conflict.task = asyncio.create_task(
                        self._finish_conflict(conflict),
                        name=f"reply-conflict-{request.request_id}",
                    )
                    return conflict
                return existing
            run = ReplyRun(request, queue_size=self.queue_size)
            self._runs[key] = run
            run.task = asyncio.create_task(
                self._execute(run),
                name=f"reply-{request.request_id}",
            )
            return run

    async def run(self, request: ReplyRequest) -> ReplyResult:
        run = await self.start(request)
        drain: asyncio.Task[None] | None = None
        if not run._result.done() and not run._run_draining:
            run._run_draining = True
            drain = asyncio.create_task(self._drain(run), name=f"reply-drain-{request.request_id}")
        try:
            return await run.wait()
        except asyncio.CancelledError:
            run.cancel()
            raise
        finally:
            if drain is not None:
                await drain

    async def _drain(self, run: ReplyRun) -> None:
        async for _event in run.events():
            pass

    async def _finish_conflict(self, run: ReplyRun) -> None:
        await self._publish(
            run,
            ReplyEventType.TERMINAL_ERROR,
            ReplyState.FAILED,
            error_code="IDEMPOTENCY_CONFLICT",
        )
        self._set_result(run, ReplyResult(run.request.request_id, ReplyState.FAILED, error_code="IDEMPOTENCY_CONFLICT"))

    async def _execute(self, run: ReplyRun) -> None:
        request = run.request
        try:
            messages = request.normalized_messages()
        except (ValueError, GatewayError) as exc:
            code = getattr(exc, "code", "INVALID_INPUT")
            await self._publish(run, ReplyEventType.TERMINAL_ERROR, ReplyState.FAILED, error_code=code)
            self._set_result(run, ReplyResult(request.request_id, ReplyState.FAILED, error_code=code))
            return

        try:
            await self._publish(run, ReplyEventType.REQUEST_ACCEPTED, ReplyState.ACCEPTED)
            if getattr(self.gateway, "stream_enabled", False):
                result = await asyncio.wait_for(self._consume_stream(run, messages), self.timeout_seconds)
            else:
                response = await asyncio.wait_for(
                    self.gateway.complete(messages, request_id=request.request_id),
                    self.timeout_seconds,
                )
                result = await self._complete_response(run, response)
            self._set_result(run, result)
        except asyncio.CancelledError:
            await self._publish(run, ReplyEventType.CANCELLED, ReplyState.CANCELLED)
            self._set_result(run, ReplyResult(request.request_id, ReplyState.CANCELLED))
        except asyncio.TimeoutError:
            await self._publish(
                run,
                ReplyEventType.RETRYABLE_ERROR,
                ReplyState.FAILED,
                error_code="LLM_TIMEOUT",
                retryable=True,
            )
            self._set_result(
                run,
                ReplyResult(request.request_id, ReplyState.FAILED, error_code="LLM_TIMEOUT", retryable=True),
            )
        except GatewayError as exc:
            event = ReplyEventType.RETRYABLE_ERROR if exc.retryable else ReplyEventType.TERMINAL_ERROR
            await self._publish(
                run,
                event,
                ReplyState.FAILED,
                error_code=exc.code,
                retryable=exc.retryable,
            )
            self._set_result(
                run,
                ReplyResult(request.request_id, ReplyState.FAILED, error_code=exc.code, retryable=exc.retryable),
            )
        except Exception:
            await self._publish(
                run,
                ReplyEventType.TERMINAL_ERROR,
                ReplyState.FAILED,
                error_code="LLM_INTERNAL",
            )
            self._set_result(run, ReplyResult(request.request_id, ReplyState.FAILED, error_code="LLM_INTERNAL"))

    async def _complete_response(self, run: ReplyRun, response: GatewayResponse) -> ReplyResult:
        if not response.text.strip():
            raise GatewayError("PROVIDER_PROTOCOL", retryable=False)
        await self._publish(
            run,
            ReplyEventType.COMPLETED,
            ReplyState.COMPLETED,
            text=response.text,
        )
        return ReplyResult(run.request.request_id, ReplyState.COMPLETED, text=response.text)

    async def _consume_stream(
        self,
        run: ReplyRun,
        messages: Sequence[Mapping[str, Any]],
    ) -> ReplyResult:
        chunks: list[str] = []
        async for delta in self.gateway.stream(messages, request_id=run.request.request_id):
            if delta.text:
                chunks.append(delta.text)
                await self._publish(
                    run,
                    ReplyEventType.STREAM_DELTA,
                    ReplyState.STREAMING,
                    delta=delta.text,
                )
        text = "".join(chunks).strip()
        if not text:
            raise GatewayError("PROVIDER_PROTOCOL", retryable=False)
        await self._publish(run, ReplyEventType.COMPLETED, ReplyState.COMPLETED, text=text)
        return ReplyResult(run.request.request_id, ReplyState.COMPLETED, text=text)

    async def _publish(
        self,
        run: ReplyRun,
        event: ReplyEventType,
        state: ReplyState,
        *,
        delta: str = "",
        text: str = "",
        error_code: str | None = None,
        retryable: bool = False,
    ) -> None:
        sequence = getattr(run, "_sequence", 0)
        run._sequence = sequence + 1
        if event in {
            ReplyEventType.CANCELLED,
            ReplyEventType.RETRYABLE_ERROR,
            ReplyEventType.TERMINAL_ERROR,
        } and run.queue.full():
            # Terminal visibility wins over a queued delta during cancellation
            # or failure; normal deltas still obey the bounded queue.
            try:
                run.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        await run.queue.put(
            ReplyEvent(
                request_id=run.request.request_id,
                sequence=sequence,
                event=event,
                state=state,
                timestamp=time.time(),
                delta=delta,
                text=text,
                error_code=error_code,
                retryable=retryable,
            )
        )

    @staticmethod
    def _set_result(run: ReplyRun, result: ReplyResult) -> None:
        if not run._result.done():
            run._result.set_result(result)


def _request_fingerprint(request: ReplyRequest) -> str:
    if request.messages is not None:
        payload: Any = list(request.messages)
    else:
        payload = [{"role": "user", "content": request.content or ""}]
    return uuid.uuid5(uuid.NAMESPACE_URL, repr(payload)).hex


__all__ = [
    "ReplyEvent",
    "ReplyEventType",
    "ReplyOrchestrator",
    "ReplyRequest",
    "ReplyResult",
    "ReplyRun",
    "ReplyState",
]
