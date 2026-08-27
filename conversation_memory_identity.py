"""Compatibility alias for the canonical conversation-memory identity module."""

import sys

from runtime.memory import conversation_memory_identity as _implementation

sys.modules[__name__] = _implementation
