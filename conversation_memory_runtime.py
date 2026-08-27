"""Process-local runtime for optional canonical conversation-memory delivery.

When Mem0 is configured, the old SQLite memory adapter remains the read-only
Archive owner but its legacy conversation-write path is disabled.  One daemon
thread then scans the atomically persisted letter state through the durable
content-free outbox.  The runtime is optional, singleton, and never blocks or
changes canonical reply persistence.
"""

from __future__ import annotations

import asyncio
import atexit
from dataclasses import dataclass
import os
from pathlib import Path
import re
import threading
from typing import Mapping

from runtime.memory.conversation_memory_delivery import ConversationMemoryDeliveryCommitter
from runtime.memory.conversation_memory_outbox import CanonicalMemoryOutbox
from conversation_memory_port import ConversationMemoryPort
from memory_port import MemoryPort


_ERROR_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")
_RUNTIME_LOCK = threading.Lock()
_RUNTIME: "ConversationMemoryRuntime | None" = None
_RUNTIME_KEY: tuple[str, str] | None = None
_ATEXIT_REGISTERED = False


@dataclass(frozen=True)
class ConversationMemoryRuntimeStatus:
    status: str
    enabled: bool
    provider: str
    worker_running: bool
    reason_code: str | None = None
    terminal_count: int = 0
    pending_count: int = 0
    attempt_count: int = 0

    def __post_init__(self) -> None:
        if self.status not in {"available", "degraded", "unavailable", "disabled"}:
            raise ValueError("conversation memory runtime status is invalid")
        if type(self.enabled) is not bool or type(self.worker_running) is not bool:
            raise ValueError("runtime flags must be boolean")
        if not isinstance(self.provider, str) or not self.provider:
            raise ValueError("runtime provider is invalid")
        if self.reason_code is not None and not _ERROR_RE.fullmatch(self.reason_code):
            raise ValueError("runtime reason code is invalid")
        for name in ("terminal_count", "pending_count", "attempt_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "enabled": self.enabled,
            "provider": self.provider,
            "worker_running": self.worker_running,
            "terminal_count": self.terminal_count,
            "pending_count": self.pending_count,
            "attempt_count": self.attempt_count,
        }
        if self.reason_code is not None:
            payload["reason_code"] = self.reason_code
        return payload


class ConversationMemoryRuntime:
    """Own one daemon poller over a configured canonical-memory outbox."""

    def __init__(
        self,
        outbox: CanonicalMemoryOutbox,
        *,
        interval_seconds: float = 5.0,
        memory_lifecycle: object | None = None,
    ) -> None:
        if not isinstance(outbox, CanonicalMemoryOutbox):
            raise TypeError("a canonical memory outbox is required")
        if not 0.25 <= interval_seconds <= 3600:
            raise ValueError("memory outbox interval is invalid")
        self.outbox = outbox
        self.interval_seconds = float(interval_seconds)
        self.memory_lifecycle = memory_lifecycle
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

    def start(self) -> bool:
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="olivia-conversation-memory-outbox",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self, *, join_timeout_seconds: float = 2.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(join_timeout_seconds)))

    def scan_once(self):
        """Synchronous maintenance seam for tests and future diagnostics."""

        return asyncio.run(self.outbox.scan_once())

    def status(self) -> ConversationMemoryRuntimeStatus:
        health = self.outbox.health()
        worker_running = bool(self._thread and self._thread.is_alive())
        raw_status = str(health.get("status", "unavailable"))
        status = raw_status if raw_status in {"available", "degraded", "unavailable"} else "unavailable"
        reason_code = (
            str(health["reason_code"])
            if isinstance(health.get("reason_code"), str)
            and _ERROR_RE.fullmatch(str(health["reason_code"]))
            else None
        )
        if status == "available" and not worker_running:
            status = "degraded"
            reason_code = "MEMORY_OUTBOX_WORKER_NOT_RUNNING"
        if self.memory_lifecycle is not None:
            try:
                if bool(self.memory_lifecycle.is_paused()):
                    status = "degraded"
                    reason_code = "MEMORY_ADMIN_PAUSED"
            except Exception:
                status = "unavailable"
                reason_code = "MEMORY_ADMIN_AUDIT_UNAVAILABLE"
        return ConversationMemoryRuntimeStatus(
            status=status,
            enabled=True,
            provider="mem0-outbox",
            worker_running=worker_running,
            reason_code=reason_code,
            terminal_count=_count(health.get("terminal_count")),
            pending_count=_count(health.get("pending_count")),
            attempt_count=_count(health.get("attempt_count")),
        )

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                asyncio.run(self.outbox.scan_once())
            except Exception:
                # Optional memory delivery never terminates the letter runtime.
                pass
            if self._stop_event.wait(self.interval_seconds):
                break


def ensure_conversation_memory_runtime(
    archive_memory: MemoryPort,
    conversation_memory: ConversationMemoryPort,
    *,
    environ: Mapping[str, str] | None = None,
    start_background: bool = True,
    memory_lifecycle: object | None = None,
) -> ConversationMemoryRuntimeStatus:
    """Configure at most one outbox worker for the current local data root."""

    environment = environ if environ is not None else os.environ
    provider_status, reason_code = _provider_status(conversation_memory)
    if provider_status == "disabled":
        return ConversationMemoryRuntimeStatus(
            "disabled",
            False,
            "none",
            False,
        )

    _disable_legacy_conversation_writes(archive_memory)

    if provider_status == "unavailable":
        return ConversationMemoryRuntimeStatus(
            "unavailable",
            False,
            "mem0",
            False,
            reason_code=reason_code or "MEM0_INITIALIZATION_FAILED",
        )

    outbox_enabled = _config_bool(conversation_memory, "outbox_enabled")
    if outbox_enabled is None:
        outbox_enabled = _as_bool(environment.get("OLIVIA_MEMORY_OUTBOX_ENABLED", "1"), True)
    if not outbox_enabled:
        return ConversationMemoryRuntimeStatus(
            "disabled",
            False,
            "mem0-outbox",
            False,
        )

    root, root_error = _state_root(conversation_memory, environment)
    if root is None:
        return ConversationMemoryRuntimeStatus(
            "unavailable" if root_error == "MEMORY_OUTBOX_DATA_ROOT_INVALID" else "degraded",
            root_error != "MEMORY_OUTBOX_DATA_ROOT_INVALID",
            "mem0",
            False,
            reason_code=root_error,
        )

    user_id = _user_id(conversation_memory, environment)
    timeout = _config_duration(
        conversation_memory,
        "write_timeout_seconds",
        maximum=300.0,
    )
    if timeout is None:
        timeout = _float(
            environment.get("OLIVIA_MEMORY_WRITE_TIMEOUT_SECONDS"),
            default=30.0,
            minimum=0.1,
            maximum=300.0,
        )
    interval = _config_duration(
        conversation_memory,
        "outbox_interval_seconds",
        maximum=3600.0,
    )
    if interval is None:
        interval = _float(
            environment.get("OLIVIA_MEMORY_OUTBOX_INTERVAL_SECONDS"),
            default=5.0,
            minimum=0.25,
            maximum=3600.0,
        )
    key = (str(root.resolve()), user_id)

    global _RUNTIME, _RUNTIME_KEY, _ATEXIT_REGISTERED
    with _RUNTIME_LOCK:
        if _RUNTIME is None or _RUNTIME_KEY != key:
            if _RUNTIME is not None:
                _RUNTIME.stop()
            try:
                committer = ConversationMemoryDeliveryCommitter(
                    conversation_memory,
                    timeout_seconds=timeout,
                    memory_lifecycle=memory_lifecycle,
                )
                outbox = CanonicalMemoryOutbox(
                    root / "state.json",
                    root / "memory" / "mem0" / "delivery.sqlite3",
                    committer,
                    user_id=user_id,
                )
                _RUNTIME = ConversationMemoryRuntime(
                    outbox,
                    interval_seconds=interval,
                    memory_lifecycle=memory_lifecycle,
                )
                _RUNTIME_KEY = key
            except (OSError, RuntimeError, TypeError, ValueError):
                _RUNTIME = None
                _RUNTIME_KEY = None
                return ConversationMemoryRuntimeStatus(
                    "unavailable",
                    False,
                    "mem0-outbox",
                    False,
                    reason_code="MEMORY_OUTBOX_INITIALIZATION_FAILED",
                )
        runtime = _RUNTIME
        if not _ATEXIT_REGISTERED:
            atexit.register(stop_conversation_memory_runtime)
            _ATEXIT_REGISTERED = True

    if runtime is None:
        return ConversationMemoryRuntimeStatus(
            "unavailable",
            False,
            "mem0-outbox",
            False,
            reason_code="MEMORY_OUTBOX_INITIALIZATION_FAILED",
        )
    if start_background:
        runtime.start()
    return runtime.status()


def conversation_memory_runtime_status() -> ConversationMemoryRuntimeStatus:
    with _RUNTIME_LOCK:
        runtime = _RUNTIME
    if runtime is None:
        return ConversationMemoryRuntimeStatus(
            "disabled",
            False,
            "none",
            False,
        )
    return runtime.status()


def stop_conversation_memory_runtime() -> None:
    global _RUNTIME, _RUNTIME_KEY
    with _RUNTIME_LOCK:
        runtime = _RUNTIME
        _RUNTIME = None
        _RUNTIME_KEY = None
    if runtime is not None:
        runtime.stop()


def _disable_legacy_conversation_writes(memory: MemoryPort) -> None:
    if hasattr(memory, "conversation_enabled"):
        try:
            setattr(memory, "conversation_enabled", False)
        except (AttributeError, TypeError, ValueError):
            pass


def _provider_status(
    memory: ConversationMemoryPort,
) -> tuple[str, str | None]:
    try:
        status = memory.status()
    except Exception:
        return "unavailable", "MEM0_STATUS_FAILED"
    raw_status = status.status
    if raw_status not in {"available", "degraded", "unavailable", "disabled"}:
        return "unavailable", "MEM0_STATUS_INVALID"
    reason = status.reason_code
    return raw_status, reason if isinstance(reason, str) and _ERROR_RE.fullmatch(reason) else None


def _state_root(
    memory: ConversationMemoryPort,
    environment: Mapping[str, str],
) -> tuple[Path | None, str]:
    """Resolve the canonical-state root without inheriting host configuration.

    A validated adapter profile wins. When it carries an explicit outbox root,
    that root is used before process settings; otherwise its ``.../memory/mem0``
    root supplies the sibling application state root. Canonical delivery thus
    remains active without ``OLIVIA_LOCAL_DATA_ROOT`` in the process environment.
    """

    adapter_config = getattr(memory, "config", None)
    adapter_outbox_root = getattr(adapter_config, "outbox_data_root", None)
    if isinstance(adapter_outbox_root, Path) and adapter_outbox_root.is_absolute():
        return adapter_outbox_root, ""

    configured = str(
        environment.get(
            "OLIVIA_MEMORY_OUTBOX_DATA_ROOT",
            environment.get("OLIVIA_LOCAL_DATA_ROOT", ""),
        )
    ).strip()
    if configured:
        root = Path(configured).expanduser()
        if root.is_absolute():
            return root, ""
        return None, "MEMORY_OUTBOX_DATA_ROOT_INVALID"

    adapter_root = getattr(adapter_config, "data_root", None)
    if not isinstance(adapter_root, Path) or not adapter_root.is_absolute():
        return None, "MEMORY_OUTBOX_DATA_ROOT_NOT_CONFIGURED"
    memory_root = adapter_root.parent if adapter_root.name.casefold() == "mem0" else adapter_root
    state_root = memory_root.parent if memory_root.name.casefold() == "memory" else memory_root
    return state_root, ""


def _user_id(
    memory: ConversationMemoryPort,
    environment: Mapping[str, str],
) -> str:
    from runtime.memory.conversation_memory_identity import (
        ConversationMemoryIdentityError,
        normalize_conversation_memory_user_id,
    )

    for value in (
        getattr(getattr(memory, "config", None), "user_id", None),
        environment.get("OLIVIA_MEMORY_USER_ID"),
        "local-user",
    ):
        try:
            return normalize_conversation_memory_user_id(value)
        except ConversationMemoryIdentityError:
            continue
    return "local-user"


def _config_bool(memory: ConversationMemoryPort, name: str) -> bool | None:
    value = getattr(getattr(memory, "config", None), name, None)
    return value if type(value) is bool else None


def _config_duration(
    memory: ConversationMemoryPort,
    name: str,
    *,
    maximum: float,
) -> float | None:
    value = getattr(getattr(memory, "config", None), name, None)
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0.1 <= parsed <= maximum else None


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return default


def _float(
    value: object,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if minimum <= parsed <= maximum else default


def _count(value: object) -> int:
    return int(value) if type(value) is int and value >= 0 else 0


__all__ = [
    "ConversationMemoryRuntime",
    "ConversationMemoryRuntimeStatus",
    "conversation_memory_runtime_status",
    "ensure_conversation_memory_runtime",
    "stop_conversation_memory_runtime",
]
