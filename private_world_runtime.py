"""Compatibility alias for the canonical PrivateWorld runtime module."""

import sys

from runtime.memory import private_world_runtime as _implementation

sys.modules[__name__] = _implementation
