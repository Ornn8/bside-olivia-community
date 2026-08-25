from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import threading
import time

import pytest

from conversation_memory_delivery import (
    CanonicalMemoryDelivery,
    CanonicalMemoryDeliveryError,
    CanonicalMemoryDeliveryStatus,
    ConversationMemoryDeliveryCommitter,
)
from conversation_memory_port import (
    ConversationMemoryStatus,
    MemoryWriteResult,
    MemoryWriteStatus,
)


NOW = datetime(2026, 8, 23, 5, 0, tzinfo=timezone.utc)


class FakeMemory:
    enabled = True

    def __init__(
        self,
        *,
        provider_status: str = "available",
        result_status: MemoryWriteStatus = MemoryWriteStatus.WRITTEN,
        error_code: str | None = None,
        delay_seconds: float = 0.0,
        raises: bool = False,
    ) -> None:
        self.provider_status = provider_status
        self.result_status = result_status
        self.error_code = error_code
        self.delay_seconds = delay_seconds
        self.raises = raises
        self.calls: list[dict[str, object]] = []

    def status(self) -> ConversationMemoryStatus:
        enabled = self.provider_status not in {"disabled", "unavailable"}
        return ConversationMemoryStatus(
            self.provider_status,
            enabled,
            "mem0" if enabled else "none",
            "qdrant-local" if enabled else "none",
            reason_code=self.error_code,
        )

    def remember_exchange(self, **kwargs: object) -> MemoryWriteResult:
        self.calls.append(dict(kwargs))
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.raises:
            raise RuntimeError("private provider detail")
        source_id = str(kwargs["source_id"])
        if self.result_status is MemoryWriteStatus.UNAVAILABLE:
            return MemoryWriteResult(
                self.result_status,
                source_id,
                error_code=self.error_code or "MEM0_WRITE_FAILED",
            )
        ids = ("memory-1", "memory-2") if self.result_status is MemoryWriteStatus.WRITTEN else ()
        return MemoryWriteResult(self.result_status, source_id, ids)


class MalformedMemory(FakeMemory):
    def remember_exchange(self, **kwargs: object):
        self.calls.append(dict(kwargs))
        return object()


def _delivery(**changes: object) -> CanonicalMemoryDelivery:
    values: dict[str, object] = {
        "letter_id": "letter-1",
        "revision": 2,
        "user_message": "我现在住在东京。",
        "assistant_message": "嗯，我记住这件事了。",
        "occurred_at": NOW,
        "user_id": "local-user",
    }
    values.update(changes)
    return CanonicalMemoryDelivery(**values)  # type: ignore[arg-type]


def test_available_provider_receives_exact_canonical_exchange_in_worker_thread() -> None:
    async def scenario() -> None:
        memory = FakeMemory()
        result = await ConversationMemoryDeliveryCommitter(memory).commit(_delivery())

        assert result.status is CanonicalMemoryDeliveryStatus.WRITTEN
        assert result.source_id == "reply:letter-1:2"
        assert result.memory_count == 2
        assert result.error_code is None
        assert memory.calls == [
            {
                "user_message": "我现在住在东京。",
                "assistant_message": "嗯，我记住这件事了。",
                "occurred_at": NOW,
                "source_id": "reply:letter-1:2",
                "user_id": "local-user",
            }
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("provider_result", "expected"),
    [
        (MemoryWriteStatus.DUPLICATE, CanonicalMemoryDeliveryStatus.DUPLICATE),
        (MemoryWriteStatus.SKIPPED, CanonicalMemoryDeliveryStatus.SKIPPED),
        (MemoryWriteStatus.UNAVAILABLE, CanonicalMemoryDeliveryStatus.UNAVAILABLE),
    ],
)
def test_provider_results_are_mapped_without_private_content(
    provider_result: MemoryWriteStatus,
    expected: CanonicalMemoryDeliveryStatus,
) -> None:
    async def scenario() -> None:
        memory = FakeMemory(
            result_status=provider_result,
            error_code=(
                "MEM0_WRITE_FAILED"
                if provider_result is MemoryWriteStatus.UNAVAILABLE
                else None
            ),
        )
        result = await ConversationMemoryDeliveryCommitter(memory).commit(_delivery())

        assert result.status is expected
        payload = result.to_dict()
        encoded = repr(payload)
        assert "我现在住在东京" not in encoded
        assert "我记住这件事" not in encoded
        if expected is CanonicalMemoryDeliveryStatus.UNAVAILABLE:
            assert result.error_code == "MEM0_WRITE_FAILED"
        else:
            assert result.error_code is None

    asyncio.run(scenario())


def test_disabled_and_unavailable_ports_do_not_receive_messages() -> None:
    async def scenario() -> None:
        disabled = FakeMemory(provider_status="disabled")
        skipped = await ConversationMemoryDeliveryCommitter(disabled).commit(_delivery())
        assert skipped.status is CanonicalMemoryDeliveryStatus.SKIPPED
        assert disabled.calls == []

        unavailable = FakeMemory(
            provider_status="unavailable",
            error_code="MEM0_IMPORT_FAILED",
        )
        failed = await ConversationMemoryDeliveryCommitter(unavailable).commit(_delivery())
        assert failed.status is CanonicalMemoryDeliveryStatus.UNAVAILABLE
        assert failed.error_code == "MEM0_IMPORT_FAILED"
        assert unavailable.calls == []

    asyncio.run(scenario())


def test_provider_exception_and_malformed_result_become_stable_failures() -> None:
    async def scenario() -> None:
        raised = await ConversationMemoryDeliveryCommitter(
            FakeMemory(raises=True)
        ).commit(_delivery())
        assert raised.status is CanonicalMemoryDeliveryStatus.UNAVAILABLE
        assert raised.error_code == "MEM0_WRITE_FAILED"

        malformed = await ConversationMemoryDeliveryCommitter(
            MalformedMemory()
        ).commit(_delivery())
        assert malformed.status is CanonicalMemoryDeliveryStatus.UNAVAILABLE
        assert malformed.error_code == "MEM0_WRITE_RESULT_INVALID"

    asyncio.run(scenario())


def test_timeout_returns_without_blocking_canonical_reply_state() -> None:
    async def scenario() -> None:
        started = time.monotonic()
        result = await ConversationMemoryDeliveryCommitter(
            FakeMemory(delay_seconds=0.08),
            timeout_seconds=0.01,
        ).commit(_delivery())
        elapsed = time.monotonic() - started

        assert result.status is CanonicalMemoryDeliveryStatus.UNAVAILABLE
        assert result.error_code == "MEM0_WRITE_TIMEOUT"
        assert elapsed < 0.07

    asyncio.run(scenario())


@pytest.mark.parametrize("blocked_stage", ["status", "write"])
def test_provider_timeout_uses_one_daemon_worker_for_status_and_write(
    blocked_stage: str,
) -> None:
    release = threading.Event()
    entered = threading.Event()
    existing_threads = set(threading.enumerate())

    class BlockingMemory(FakeMemory):
        def __init__(self) -> None:
            super().__init__()
            self.status_calls = 0
            self.write_calls = 0

        def status(self) -> ConversationMemoryStatus:
            self.status_calls += 1
            if blocked_stage == "status":
                entered.set()
                release.wait()
            return super().status()

        def remember_exchange(self, **kwargs: object) -> MemoryWriteResult:
            self.write_calls += 1
            if blocked_stage == "write":
                entered.set()
                release.wait()
            return super().remember_exchange(**kwargs)

    async def scenario() -> None:
        memory = BlockingMemory()
        committer = ConversationMemoryDeliveryCommitter(memory, timeout_seconds=0.02)
        started = time.monotonic()
        results = [
            await committer.commit(_delivery(revision=revision))
            for revision in (2, 3, 4)
        ]
        assert time.monotonic() - started < 0.2
        assert all(result.error_code == "MEM0_WRITE_TIMEOUT" for result in results)
        assert entered.is_set()
        assert (memory.status_calls, memory.write_calls) == (
            (1, 0) if blocked_stage == "status" else (1, 1)
        )

    failure: list[BaseException] = []
    done = threading.Event()

    def run_scenario() -> None:
        try:
            asyncio.run(scenario())
        except BaseException as exc:
            failure.append(exc)
        finally:
            done.set()

    probe = threading.Thread(target=run_scenario, daemon=True)
    try:
        probe.start()
        assert done.wait(0.3)
        assert not failure
        workers = [
            thread
            for thread in threading.enumerate()
            if thread.name == "olivia-memory-delivery" and thread not in existing_threads
        ]
        assert len(workers) == 1
        assert workers[0].daemon is True
    finally:
        release.set()
        probe.join(timeout=0.5)
        for thread in threading.enumerate():
            if thread.name == "olivia-memory-delivery" and thread not in existing_threads:
                thread.join(timeout=0.5)


@pytest.mark.parametrize(
    "changes",
    [
        {"letter_id": "bad id"},
        {"revision": 0},
        {"revision": True},
        {"user_message": ""},
        {"assistant_message": "\x00bad"},
        {"occurred_at": datetime(2026, 8, 23)},
        {"user_id": "bad scope"},
    ],
)
def test_delivery_contract_rejects_invalid_or_ambiguous_inputs(
    changes: dict[str, object],
) -> None:
    with pytest.raises(CanonicalMemoryDeliveryError):
        _delivery(**changes)


@pytest.mark.parametrize(
    "timeout_seconds", [0, 301, True, "0.1", float("nan"), float("inf"), -float("inf")]
)
def test_committer_requires_typed_delivery_and_bounded_timeout(
    timeout_seconds: object,
) -> None:
    memory = FakeMemory()

    async def scenario() -> None:
        with pytest.raises(ValueError):
            ConversationMemoryDeliveryCommitter(memory, timeout_seconds=timeout_seconds)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            await ConversationMemoryDeliveryCommitter(memory).commit(object())  # type: ignore[arg-type]

    asyncio.run(scenario())
