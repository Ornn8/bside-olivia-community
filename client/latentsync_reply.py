"""Compatibility alias for the canonical LatentSync renderer."""

import sys

from runtime.media import latentsync_reply as _implementation

sys.modules[__name__] = _implementation
