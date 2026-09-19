"""Compatibility alias for :mod:`runtime.memory.companion_memory_context`."""

from importlib import import_module as _import_module
import sys as _sys


_module = _import_module("runtime.memory.companion_memory_context")
_sys.modules[__name__] = _module
