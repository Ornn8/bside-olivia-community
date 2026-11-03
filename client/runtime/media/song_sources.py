"""Songs received in a chat, kept as cover sources until they are used or have gone unused for a few days.

Each song is a normalised stereo WAV at ``cover-inputs/<id>/source.wav`` (the layout the cover upload uses, so
``cover_upload.source_path`` finds it) and one row in ``cover-inputs/chat-sources.json``:
{id, key, title, singer, origin, identity, seconds, created_at, used, used_at}.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

KEEP_SECONDS = 3 * 86400
MAX_KEPT = 20
INDEX_NAME = 'chat-sources.json'
_LOCK = threading.Lock()


class SongSources:
    def __init__(self, data_root: Path):
        self.root = Path(data_root) / 'cover-inputs'
        self.index = self.root / INDEX_NAME

    # --- storage -----------------------------------------------------------------------------------------------

    def _read(self) -> list[dict]:
        try:
            rows = json.loads(self.index.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return []
        return [row for row in rows if isinstance(row, dict) and re.fullmatch(r'[a-f0-9]{32}', str(row.get('id', '')))] \
            if isinstance(rows, list) else []

    def _write(self, rows: list[dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.index.with_name(INDEX_NAME + '.tmp')
        temporary.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, self.index)

    def folder(self, source_id: str) -> Path:
        return self.root / source_id

    def new_id(self) -> str:
        return uuid.uuid4().hex

    def _present(self, row) -> bool:
        return (self.folder(row['id']) / 'source.wav').is_file()

    # --- the rows ----------------------------------------------------------------------------------------------

    def add(self, source_id: str, *, key: str, title: str, singer: str, origin: str, identity: str,
            seconds: float, now: float | None = None) -> dict:
        """Index a song whose WAV is already in place."""
        row = {'id': source_id, 'key': key, 'title': title[:80], 'singer': singer[:60], 'origin': origin,
               'identity': identity, 'seconds': round(float(seconds), 1), 'created_at': now if now is not None else time.time(),
               'used': False}
        with _LOCK:
            rows = [item for item in self._read() if item['id'] != source_id]
            rows.append(row)
            self._write(rows)
        return row

    def get(self, source_id) -> dict | None:
        if not isinstance(source_id, str):
            return None
        with _LOCK:
            return next((row for row in self._read() if row['id'] == source_id and self._present(row)), None)

    def find_key(self, key: str, now: float | None = None) -> dict | None:
        """A song kept from the same share or file, still usable."""
        now = now if now is not None else time.time()
        with _LOCK:
            return next((row for row in reversed(self._read())
                         if row.get('key') == key and self._present(row) and now - self._touched(row) < KEEP_SECONDS), None)

    def unused(self, *, limit: int = 5, now: float | None = None) -> list[dict]:
        """The newest songs not yet covered, at most ``limit``."""
        now = now if now is not None else time.time()
        with _LOCK:
            rows = [row for row in self._read() if not row.get('used') and self._present(row)
                    and now - row.get('created_at', 0) < KEEP_SECONDS]
        return sorted(rows, key=lambda row: row.get('created_at', 0), reverse=True)[:limit]

    def mark_used(self, source_id: str, now: float | None = None) -> bool:
        with _LOCK:
            rows = self._read()
            for row in rows:
                if row['id'] == source_id:
                    row.update(used=True, used_at=now if now is not None else time.time())
                    self._write(rows)
                    return True
        return False

    @staticmethod
    def _touched(row) -> float:
        return max(row.get('created_at', 0), row.get('used_at', 0))

    def remove(self, source_id: str) -> None:
        with _LOCK:
            self._write([row for row in self._read() if row['id'] != source_id])
        shutil.rmtree(self.folder(source_id), ignore_errors=True)

    def cleanup(self, now: float | None = None) -> list[str]:
        """Songs unused (or used) for KEEP_SECONDS are deleted, then the oldest unused beyond MAX_KEPT. Returns the ids."""
        now = now if now is not None else time.time()
        gone = []
        with _LOCK:
            rows = self._read()
            keep = []
            for row in rows:
                if now - self._touched(row) >= KEEP_SECONDS or not self._present(row):
                    gone.append(row['id'])
                else:
                    keep.append(row)
            idle = sorted((row for row in keep if not row.get('used')), key=lambda row: row.get('created_at', 0))
            for row in idle[:max(0, len(idle) - MAX_KEPT)]:
                gone.append(row['id'])
                keep.remove(row)
            if gone:
                self._write(keep)
        for source_id in gone:
            shutil.rmtree(self.folder(source_id), ignore_errors=True)
        return gone
