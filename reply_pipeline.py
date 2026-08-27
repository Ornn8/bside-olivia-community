"""Compatibility alias for :mod:`runtime.reply.reply_pipeline`."""

from __future__ import annotations

import sys

from runtime.reply import reply_pipeline as _implementation


sys.modules[__name__] = _implementation
