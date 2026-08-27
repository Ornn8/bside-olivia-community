"""Compatibility entry point for bounded daemon-thread calls."""

from runtime.memory.bounded_daemon_call import BoundedDaemonCall, validate_timeout_seconds

__all__ = ["BoundedDaemonCall", "validate_timeout_seconds"]
