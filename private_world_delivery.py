"""Compatibility alias for the canonical PrivateWorld delivery module."""

import sys

from runtime.memory import private_world_delivery as _implementation

sys.modules[__name__] = _implementation
