"""The interface language the user chose in the desktop UI, as the backend sees it.

The injected UI keeps the choice in the browser (localStorage ``olivia.ui.language``) and reports it with
``POST /api/ui-language``. This module holds the value for the whole backend process, keeps it in
``config/ui-language.json`` next to the other saved settings, and answers three questions:

* ``current()`` is ``"zh-cn"`` or ``"en"``; it is ``"zh-cn"`` until English is chosen.
* ``headers()`` / ``with_header()`` give the header that tells our own cloud services the interface language. The
  header exists only for English. In Chinese nothing is added, so requests stay exactly as they were.
* ``add_listener()`` lets a module react when the language changes (for example to refresh a cached policy).

Subprocesses started later inherit the language through ``OLIVIA_UI_LANGUAGE``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import os
from pathlib import Path
import threading

HEADER = "X-Olivia-UI-Language"
CHINESE = "zh-cn"
ENGLISH = "en"
LANGUAGES = (CHINESE, ENGLISH)
ENVIRONMENT_NAME = "OLIVIA_UI_LANGUAGE"
FILE_NAME = "ui-language.json"
_SCHEMA_VERSION = 1

_lock = threading.RLock()
_value: str | None = None
_path: Path | None = None
_listeners: list[Callable[[str], None]] = []


def normalize(value: object) -> str | None:
    """Return ``"zh-cn"`` or ``"en"`` for an accepted value, otherwise ``None``."""

    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text if text in LANGUAGES else None


def current() -> str:
    """The language chosen in the UI; Chinese unless English was chosen."""

    with _lock:
        if _value is not None:
            return _value
    return normalize(os.environ.get(ENVIRONMENT_NAME)) or CHINESE


def is_english() -> bool:
    return current() == ENGLISH


def headers() -> dict[str, str]:
    """The cloud language header, or nothing at all when the interface is Chinese."""

    return {HEADER: ENGLISH} if is_english() else {}


def with_header(existing: Mapping[str, str] | None = None) -> Mapping[str, str] | None:
    """``existing`` plus the language header in English; ``existing`` itself, untouched, in Chinese."""

    if not is_english():
        return existing
    merged = dict(existing or {})
    merged[HEADER] = ENGLISH
    return merged


def add_listener(listener: Callable[[str], None]) -> None:
    with _lock:
        if listener not in _listeners:
            _listeners.append(listener)


def remove_listener(listener: Callable[[str], None]) -> None:
    with _lock:
        if listener in _listeners:
            _listeners.remove(listener)


def _publish(language: str) -> None:
    if language == ENGLISH:
        os.environ[ENVIRONMENT_NAME] = ENGLISH
    else:
        os.environ.pop(ENVIRONMENT_NAME, None)


def read_saved(path: Path | str | None) -> str | None:
    """The language saved in ``path``, or ``None`` when the file is missing, damaged or has another version.

    Processes that do not run the backend (the launcher) use this to pick the language of their messages.
    """

    if path is None:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if isinstance(payload, dict) and payload.get("schema_version") == _SCHEMA_VERSION:
        return normalize(payload.get("language"))
    return None


def configure(path: Path | str | None) -> str:
    """Use ``path`` for persistence and load the saved choice (missing or damaged files mean Chinese)."""

    global _value, _path
    saved = read_saved(path)
    resolved = Path(path) if path is not None else None
    with _lock:
        _path = resolved
        _value = saved or CHINESE
        _publish(_value)
        return _value


def _persist(language: str) -> bool:
    with _lock:
        target = _path
    if target is None:
        return False
    staging = target.with_suffix(target.suffix + ".staging")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging.write_text(
            json.dumps({"schema_version": _SCHEMA_VERSION, "language": language}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(target)
    except OSError:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def set_language(value: object) -> tuple[str, bool]:
    """Remember the chosen language. Returns ``(language, saved_to_disk)``; raises ``ValueError`` for other values."""

    global _value
    language = normalize(value)
    if language is None:
        raise ValueError("UI_LANGUAGE_INVALID")
    with _lock:
        previous = current()
        _value = language
        _publish(language)
        listeners = list(_listeners)
        unchanged = previous == language
    # The page reports its language on every start; write the file only when it differs from what is saved.
    saved = True if unchanged and _saved_language() == language else _persist(language)
    if not unchanged:
        for listener in listeners:
            try:
                listener(language)
            except Exception:
                pass
    return language, saved


def _saved_language() -> str | None:
    with _lock:
        target = _path
    return read_saved(target)


def reset() -> None:
    """Forget the stored value, the file and the listeners (tests and process re-initialisation)."""

    global _value, _path
    with _lock:
        _value = None
        _path = None
        _listeners.clear()
    os.environ.pop(ENVIRONMENT_NAME, None)


__all__ = [
    "CHINESE", "ENGLISH", "ENVIRONMENT_NAME", "FILE_NAME", "HEADER", "LANGUAGES",
    "add_listener", "configure", "current", "headers", "is_english", "normalize",
    "read_saved", "remove_listener", "reset", "set_language", "with_header",
]
