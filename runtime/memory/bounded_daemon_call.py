"""Bound one provider operation to one daemon worker at a time."""

from __future__ import annotations

import asyncio
import math
from numbers import Real
import threading
import time
from typing import Callable


def validate_timeout_seconds(value: object) -> float:
    """Return a finite provider timeout within the shared lifecycle bound."""
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(value)
        or not 0 < value <= 300
    ):
        raise ValueError("timeout_seconds is invalid")
    return float(value)


class BoundedDaemonCall:
    """Run a blocking provider call without accumulating executor threads."""

    def __init__(self, *, thread_name: str) -> None:
        if not isinstance(thread_name, str) or not thread_name:
            raise ValueError("thread_name is required")
        self._thread_name = thread_name
        self._lock = threading.Lock()
        self._inflight = False

    @property
    def inflight(self) -> bool:
        with self._lock:
            return self._inflight

    def call(
        self,
        operation: Callable[[], object],
        *,
        timeout_seconds: float,
    ) -> tuple[str, object | None]:
        timeout_seconds = validate_timeout_seconds(timeout_seconds)
        pending = self._start(operation)
        if pending is None:
            return "inflight", None
        done, outcome = pending
        return self._result(outcome) if done.wait(timeout_seconds) else ("timeout", None)

    async def call_async(
        self,
        operation: Callable[[], object],
        *,
        timeout_seconds: float,
    ) -> tuple[str, object | None]:
        timeout_seconds = validate_timeout_seconds(timeout_seconds)
        pending = self._start(operation)
        if pending is None:
            return "inflight", None
        done, outcome = pending
        deadline = time.monotonic() + timeout_seconds
        while not done.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", None
            await asyncio.sleep(min(0.01, remaining))
        return self._result(outcome)

    def _start(
        self,
        operation: Callable[[], object],
    ) -> tuple[threading.Event, dict[str, object]] | None:
        with self._lock:
            if self._inflight:
                return None
            self._inflight = True
        done, outcome = threading.Event(), {}

        def run() -> None:
            try:
                outcome["value"] = operation()
            except BaseException:
                outcome["failed"] = True
            finally:
                with self._lock:
                    self._inflight = False
                done.set()

        threading.Thread(target=run, name=self._thread_name, daemon=True).start()
        return done, outcome

    @staticmethod
    def _result(outcome: dict[str, object]) -> tuple[str, object | None]:
        return ("failed", None) if outcome.get("failed") else ("completed", outcome.get("value"))
