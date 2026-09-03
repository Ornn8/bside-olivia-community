"""Single mapping from reply modes to Persona v2 communication modes."""

from __future__ import annotations

from runtime.reply.reply_context import ReplyMode


_PERSONA_MODE_BY_REPLY_MODE = {
    ReplyMode.TEXT_LETTER: "text_letter",
    ReplyMode.SPOKEN_VIDEO: "spoken_video",
    ReplyMode.MUSICAL_VIDEO: "musical_video",
    ReplyMode.FUTURE_IM: "future_im",
}


def persona_mode_for_reply_mode(mode: ReplyMode) -> str:
    """Return the schema mode for one validated reply mode."""

    if not isinstance(mode, ReplyMode):
        raise TypeError("mode must be ReplyMode")
    return _PERSONA_MODE_BY_REPLY_MODE[mode]


__all__ = ["persona_mode_for_reply_mode"]
