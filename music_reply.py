"""Compatibility alias for :mod:`runtime.media.music_reply`."""

from importlib import import_module as _import_module
import sys as _sys


_module = _import_module("runtime.media.music_reply")
_sys.modules[__name__] = _module
