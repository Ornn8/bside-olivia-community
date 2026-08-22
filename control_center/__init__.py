"""Local management surface for the Companion Control Center."""

from .app import create_control_app
from .auth import ControlSessionManager

__all__ = ["ControlSessionManager", "create_control_app"]
