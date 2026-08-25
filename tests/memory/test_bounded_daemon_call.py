from __future__ import annotations

import threading
import time

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
