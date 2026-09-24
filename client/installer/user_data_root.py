"""Keep an accidentally nested installation on the original user's data."""
from __future__ import annotations

import json
from pathlib import Path


def _plain(path: Path) -> bool:
    return all(not p.is_symlink() and not (getattr(p.lstat(), 'st_file_attributes', 0) & 0x400)
               for p in (path, *path.parents) if p.exists())


def _history(data: Path) -> int:
    state = data / 'state.json'
    if not state.exists():
        return 0
    if not _plain(state):
        raise ValueError('USER_DATA_LINK_REFUSED')
    value = json.loads(state.read_text(encoding='utf-8-sig'))
    # The native welcome screen is not a persisted user letter.
    return len(value.get('letters', [])) + len(value.get('personal_chats', []))


def resolve_user_data_root(installation: Path) -> Path:
    """Read-only recovery: never move, replace, merge or delete either data tree."""
    root = installation.absolute()
    current = root / 'data'
    if root.name.casefold() != 'install' or root.parent.name.casefold() != 'install':
        return current
    original = root.parent / 'data'
    if not (original / 'state.json').is_file():
        return current
    if not _plain(original) or not _plain(current):
        raise ValueError('USER_DATA_LINK_REFUSED')
    if not _history(original):
        return current
    if _history(current):
        raise ValueError('USER_DATA_TWO_HISTORIES_NEED_MERGE')
    return original
