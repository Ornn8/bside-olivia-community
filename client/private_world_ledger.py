"""Compatibility alias for :mod:`runtime.private_world.ledger`."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module("runtime.private_world.ledger")
