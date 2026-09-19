from __future__ import annotations

import asyncio
import threading
import time

import pytest

from runtime.memory.bounded_daemon_call import BoundedDaemonCall


def test_permanently_blocked_call_times_out_without_starting_another_worker() -> None:
    release = threading.Event()
    entered = threading.Event()
    existing = set(threading.enumerate())
    call = BoundedDaemonCall(thread_name="mem0-provider-fixture")

    def block() -> str:
        entered.set()
        release.wait()
        return "done"

    try:
        started = time.monotonic()
        assert call.call(block, timeout_seconds=0.02) == ("timeout", None)
        assert time.monotonic() - started < 0.2
        assert entered.is_set()
        assert call.call(lambda: "second", timeout_seconds=0.02) == ("inflight", None)
        workers = [
            thread
            for thread in threading.enumerate()
            if thread.name == "mem0-provider-fixture" and thread not in existing
        ]
        assert len(workers) == 1
        assert workers[0].daemon is True
    finally:
        release.set()
        for thread in threading.enumerate():
            if thread.name == "mem0-provider-fixture" and thread not in existing:
                thread.join(timeout=0.5)


def test_timed_out_call_keeps_its_exact_result_until_settled() -> None:
    release = threading.Event()
    call = BoundedDaemonCall(thread_name="mem0-provider-settle")

    def finish_later() -> str:
        release.wait()
        return "original-result"

    assert call.call(finish_later, timeout_seconds=0.02) == ("timeout", None)
    release.set()
    deadline = time.monotonic() + 0.5
    while call.inflight and time.monotonic() < deadline:
        time.sleep(0.01)

    assert call.call(lambda: "wrong-result", timeout_seconds=0.02) == (
        "inflight",
        None,
    )
    assert call.settle() == ("completed", "original-result")
    assert call.call(lambda: "next-result", timeout_seconds=0.02) == (
        "completed",
        "next-result",
    )


@pytest.mark.parametrize("timeout_seconds", [float("nan"), float("inf"), -float("inf")])
def test_calls_reject_non_finite_timeouts_before_starting_workers(
    timeout_seconds: float,
) -> None:
    call = BoundedDaemonCall(thread_name="mem0-provider-invalid-timeout")
    with pytest.raises(ValueError):
        call.call(lambda: "unexpected", timeout_seconds=timeout_seconds)

    async def scenario() -> None:
        with pytest.raises(ValueError):
            await call.call_async(lambda: "unexpected", timeout_seconds=timeout_seconds)

    asyncio.run(scenario())
    assert call.inflight is False
