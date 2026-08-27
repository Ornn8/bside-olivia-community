"""Compatibility alias for :mod:`runtime.reply.reply_reviewer`."""

from __future__ import annotations

import sys

from runtime.reply import reply_reviewer as _implementation


sys.modules[__name__] = _implementation
