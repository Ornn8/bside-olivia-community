"""Compatibility entry point for deterministic MiniMax caption rendering."""

from runtime.media.music_caption import (
    MINIMAX_CAPTION_VERSION,
    render_minimax_caption,
    validate_minimax_caption,
)

__all__ = [
    "MINIMAX_CAPTION_VERSION",
    "render_minimax_caption",
    "validate_minimax_caption",
]
