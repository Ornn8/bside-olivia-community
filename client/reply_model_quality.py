"""Compatibility alias for :mod:`runtime.reply.reply_model_quality`."""

from importlib import import_module as _import_module
import sys as _sys


_module = _import_module("runtime.reply.reply_model_quality")
_sys.modules[__name__] = _module
