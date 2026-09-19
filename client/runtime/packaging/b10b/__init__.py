"""B10B fail-closed lifecycle for independently managed local modules."""

from .errors import B10BError
from .manager import B10BManager

__all__ = ["B10BError", "B10BManager"]
