"""Standalone local text-to-speech contracts and providers for B06.

The package is intentionally independent from the B03/B05 HTTP and event
pipelines.  It can be exercised through ``tools/tts_cli.py`` without making
the native HTTP capability claim that B02 deliberately keeps unavailable.
"""

from .contracts import (
    AudioChunk,
    DIRECTED_DELIVERY_ERROR_CODES,
    TTSConfig,
    TTSResult,
    TTSRun,
    TTSStreamEvent,
    TTSRequest,
    TTSError,
    TTSUnavailable,
)
from .profiles import TTSProfileManager
from .registry import TTSProviderRegistry, default_registry
from .service import TTSService

__all__ = [
    "AudioChunk",
    "DIRECTED_DELIVERY_ERROR_CODES",
    "TTSConfig",
    "TTSProfileManager",
    "TTSProviderRegistry",
    "TTSRequest",
    "TTSResult",
    "TTSRun",
    "TTSService",
    "TTSStreamEvent",
    "TTSError",
    "TTSUnavailable",
    "default_registry",
]
