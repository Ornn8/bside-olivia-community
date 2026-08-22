"""B10A declarative module manager.

This package deliberately manages manifests, local state, and a tiny mock
service only. It does not implement or install the future LLM, memory, ASR,
TTS, visual-driver, or original-media providers.
"""

from .manager import B10AManager

__all__ = ["B10AManager"]
