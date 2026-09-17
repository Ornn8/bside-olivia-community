"""Durable channel choice made from the local setup surface."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


_CHOICES = {
    "qq": ("qq",),
    "wechat": ("wechat",),
    "both": ("qq", "wechat"),
}


def _path(root: Path) -> Path:
    return Path(root) / "personal-chat" / "channel-choice.json"


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load(root: Path) -> dict[str, str]:
    try:
        value = json.loads(_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if (
        not isinstance(value, dict)
        or set(value) != {"invitation_id", "choice"}
        or not isinstance(value.get("invitation_id"), str)
        or value.get("choice") not in _CHOICES
    ):
        return {}
    return {"invitation_id": value["invitation_id"], "choice": value["choice"]}


def save(root: Path, invitation_id: str, choice: str) -> tuple[str, ...]:
    if not isinstance(invitation_id, str) or not invitation_id or choice not in _CHOICES:
        raise ValueError("PERSONAL_CHAT_CHANNEL_CHOICE_INVALID")
    _atomic_text(
        _path(root),
        json.dumps(
            {"invitation_id": invitation_id, "choice": choice},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
    )
    return _CHOICES[choice]


def channels(root: Path, invitation_id: str) -> tuple[str, ...]:
    value = load(root)
    if value.get("invitation_id") != invitation_id:
        return ()
    return _CHOICES.get(value.get("choice", ""), ())


__all__ = ["channels", "load", "save"]
