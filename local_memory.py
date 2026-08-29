"""Compatibility alias for :mod:`runtime.memory.local_memory`."""

from importlib import import_module as _import_module
import sys as _sys


_module = _import_module("runtime.memory.local_memory")
_sys.modules[__name__] = _module
