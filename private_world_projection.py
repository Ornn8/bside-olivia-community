"""Compatibility alias for the canonical PrivateWorld projection module."""

import sys

from runtime.memory import private_world_projection as _implementation

sys.modules[__name__] = _implementation
