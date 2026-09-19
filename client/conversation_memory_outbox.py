"""Compatibility alias for :mod:`runtime.memory.conversation_memory_outbox`."""

from __future__ import annotations

import sys

from runtime.memory import conversation_memory_outbox as _implementation


sys.modules[__name__] = _implementation
