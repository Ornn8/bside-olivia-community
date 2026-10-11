"""Optional sticker packs the user downloads from the community page.

Only the 108 line-art illustrations ship with Olivia. The other styles are
offered as packs on the download page; the user unzips a pack into
``<data>/sticker-packs`` and Linli can use it in QQ from the next reply on.
A file counts only when its name, size and SHA-256 match the bundled image
catalog, so a renamed or damaged image is never sent.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from functools import lru_cache
from pathlib import Path

FOLDER = 'sticker-packs'
# style -> (display name, first id, last id); the ids are the catalog's stable ids.
PACKS = {
    'sketchbook': ('手绘', 109, 140),
    'quiet_blue': ('静蓝', 141, 172),
    'vinyl': ('唱片', 173, 204),
    'dramatic_manga': ('戏剧漫画', 205, 228),
    'jojo_chat': ('奇想漫画', 229, 252),
    'animated': ('动图', 253, 272),
}
_SCAN_DEPTH = 3
_LOCK = threading.Lock()
_VERIFIED: dict[tuple[str, int, int], bool] = {}


@lru_cache(maxsize=1)
def _expected() -> dict[str, tuple[str, int, str]]:
    """filename -> (sticker id, size, sha256) for every pack sticker."""
    path = Path(__file__).resolve().parents[1] / 'image_assets.json'
    assets = json.loads(path.read_text(encoding='utf-8'))['assets']['stickers']
    pack_ids = {f'linli-{i:03d}' for _, first, last in PACKS.values() for i in range(first, last + 1)}
    return {entry['filename']: (key, entry['size_bytes'], entry['sha256'])
            for key, entry in assets.items() if key in pack_ids}


def folder(data_root) -> Path | None:
    return Path(data_root) / FOLDER if data_root is not None else None


def _verified(path: Path, size: int, digest: str) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    if stat.st_size != size:
        return False
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _LOCK:
        if key in _VERIFIED:
            return _VERIFIED[key]
    try:
        with path.open('rb') as stream:
            ok = hashlib.file_digest(stream, 'sha256').hexdigest() == digest
    except OSError:
        return False
    with _LOCK:
        if len(_VERIFIED) > 4096:
            _VERIFIED.clear()
        _VERIFIED[key] = ok
    return ok


def installed(data_root) -> dict[str, Path]:
    """sticker id -> verified local file. A missing folder simply means no packs."""
    root = folder(data_root)
    if root is None or not root.is_dir():
        return {}
    expected, found = _expected(), {}
    base_depth = len(root.parts)
    for directory, dirs, names in os.walk(root, followlinks=False):
        current = Path(directory)
        if len(current.parts) - base_depth >= _SCAN_DEPTH:
            dirs[:] = []
        dirs[:] = [name for name in dirs if not (current / name).is_symlink()]
        for name in names:
            spec = expected.get(name)
            if spec is None or spec[0] in found:
                continue
            path = current / name
            if not path.is_symlink() and _verified(path, spec[1], spec[2]):
                found[spec[0]] = path
    return found


def status(data_root) -> list[dict[str, object]]:
    present = installed(data_root)
    return [{'pack': style, 'name': name, 'total': last - first + 1,
             'installed': sum(f'linli-{i:03d}' in present for i in range(first, last + 1))}
            for style, (name, first, last) in PACKS.items()]


def open_folder(data_root) -> Path:
    """Create the pack folder if needed and show it in Explorer."""
    root = folder(data_root)
    if root is None:
        raise OSError('STICKER_PACK_FOLDER_UNAVAILABLE')
    root.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        import subprocess
        explorer = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'explorer.exe'
        subprocess.Popen([str(explorer), str(root)], shell=False)
    return root
