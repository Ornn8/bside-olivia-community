"""Compatibility alias for :mod:`runtime.memory.memory`."""

from importlib import import_module as _import_module
import sys as _sys


_module = _import_module("runtime.memory.memory")
_sys.modules[__name__] = _module
