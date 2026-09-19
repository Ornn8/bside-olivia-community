"""Build-time safeguards for non-verbatim persona style exemplars."""

from __future__ import annotations

from collections.abc import Iterable
from unicodedata import normalize


_OVERLAP_WIDTH = 7
_MODES = frozenset({"text_letter", "spoken_video", "musical_video", "future_im"})


class StyleExemplarBuildError(ValueError):
    """Raised when a style exemplar cannot satisfy its publication contract."""


def contiguous_overlap_count(
    assistant_text: str,
    source_texts: Iterable[str],
    *,
    width: int = _OVERLAP_WIDTH,
) -> int:
    """Count candidate windows that occur verbatim in any NFC-normalized source."""

    if width < 1:
        raise ValueError("width must be positive")
    candidate = normalize("NFC", assistant_text)
    sources = tuple(normalize("NFC", source) for source in source_texts)
    if len(candidate) < width:
        return 0
    return sum(
        any(candidate[index : index + width] in source for source in sources)
        for index in range(len(candidate) - width + 1)
    )


def build_style_exemplar(
    *,
    exemplar_id: str,
    source_id: str,
    mode: str,
    situation: str,
    user_text: str,
    assistant_text: str,
    source_texts: Iterable[str],
    user_text_is_synthetic: bool,
    derivation: str = "SYNTHETIC",
    rights_status: str = "REDISTRIBUTABLE",
    allowed_public_release: bool = True,
) -> dict[str, object]:
    """Build a schema-ready exemplar after enforcing the non-verbatim boundary."""

    if mode not in _MODES:
        raise StyleExemplarBuildError("STYLE_EXEMPLAR_MODE_INVALID")
    if derivation not in {"SYNTHETIC", "PRIVATE_CORPUS_ABSTRACTION"}:
        raise StyleExemplarBuildError("STYLE_EXEMPLAR_DERIVATION_INVALID")
    if not 1 <= len(user_text) <= 240:
        raise StyleExemplarBuildError("STYLE_EXEMPLAR_USER_TEXT_INVALID")
    if not 1 <= len(assistant_text) <= 400:
        raise StyleExemplarBuildError("STYLE_EXEMPLAR_ASSISTANT_TEXT_INVALID")
    if user_text_is_synthetic is not True:
        raise StyleExemplarBuildError("STYLE_EXEMPLAR_USER_TEXT_NOT_SYNTHETIC")
    sources = tuple(source_texts)
    if contiguous_overlap_count(user_text, sources) or contiguous_overlap_count(
        assistant_text,
        sources,
    ):
        raise StyleExemplarBuildError("STYLE_EXEMPLAR_SOURCE_OVERLAP")

    return {
        "exemplar_id": exemplar_id,
        "source_id": source_id,
        "derivation": derivation,
        "rights_status": rights_status,
        "allowed_public_release": allowed_public_release,
        "mode": mode,
        "situation": situation,
        "user_text": user_text,
        "assistant_text": assistant_text,
        "style_only": True,
        "factual_authority": False,
        "user_text_is_synthetic": user_text_is_synthetic,
        "assistant_text_is_verbatim": False,
    }


__all__ = [
    "StyleExemplarBuildError",
    "build_style_exemplar",
    "contiguous_overlap_count",
]
