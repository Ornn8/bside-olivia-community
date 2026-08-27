"""Compatibility alias for the canonical song-content planner."""

import sys

from runtime.media import song_content as _implementation

sys.modules[__name__] = _implementation
