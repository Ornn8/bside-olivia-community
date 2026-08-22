"""Local ASR contracts and provider implementations for B05.

The package deliberately keeps the text-input fallback separate from native
speech recognition.  Importing this package never downloads a model, starts a
process, or makes a network request.
"""

from .config import (
    MODEL_FILENAME,
    MODEL_REPO,
    MODEL_REVISION,
    RUNTIME_REPO,
    RUNTIME_REVISION,
    AsrConfig,
)
from .contracts import AsrEvent, EventClock
from .errors import AsrError

__all__ = [
    "AsrConfig",
    "AsrError",
    "AsrEvent",
    "EventClock",
    "MODEL_FILENAME",
    "MODEL_REPO",
    "MODEL_REVISION",
    "RUNTIME_REPO",
    "RUNTIME_REVISION",
]
