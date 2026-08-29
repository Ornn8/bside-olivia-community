"""Compatibility alias for :mod:`runtime.memory.conversation_memory_port`."""

from importlib import import_module as _import_module
import sys as _sys


_module = _import_module("runtime.memory.conversation_memory_port")
_sys.modules[__name__] = _module
