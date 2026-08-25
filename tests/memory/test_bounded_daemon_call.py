from __future__ import annotations

import asyncio
import threading
import time

import pytest

from bounded_daemon_call import BoundedDaemonCall


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
