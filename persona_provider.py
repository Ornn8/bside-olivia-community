"""Compatibility alias for :mod:`runtime.persona.persona_provider`."""

from importlib import import_module as _import_module
import sys as _sys


_module = _import_module("runtime.persona.persona_provider")
_sys.modules[__name__] = _module
