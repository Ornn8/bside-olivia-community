"""Private, bounded semantic-plan cache; captions are always rendered locally."""

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from runtime.media.music_caption import render_minimax_caption
from runtime.media.song_content import SongContentPlan, SongSemanticPlan, parse_song_semantic_plan

_SCHEMA = "olivia.private-song-plan.v1"
# Bump when planning/semantic or caption contracts change.
_PLANNER_VERSION = 1
_MAX_BYTES = 16384


def _safe(path: Path) -> bool:
    for candidate in (path, *path.parents):
        try:
            value = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
            return False
    return True


def _restore(value, duration):
    semantic = parse_song_semantic_plan(json.dumps(value, ensure_ascii=False), duration)
    return SongContentPlan(semantic.emotion_arc.value, semantic.lyrics,
                           render_minimax_caption(semantic), duration, semantic_plan=semantic)


def cached_song_plan(path: Path, content: str, reply: str, duration: int, planner):
    identity = hashlib.sha256(json.dumps([content, reply, duration],
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    expected = {"schema_version": _SCHEMA, "planner_version": 2 if duration == 110 else _PLANNER_VERSION,
                "input_sha256": identity}
    try:
        if _safe(path) and stat.S_ISREG(path.stat().st_mode) and path.stat().st_size <= _MAX_BYTES:
            with path.open("rb") as source:
                raw = source.read(_MAX_BYTES + 1)
            if len(raw) <= _MAX_BYTES:
                payload = json.loads(raw)
                if (isinstance(payload, dict) and set(payload) == {*expected, "semantic_plan"}
                        and all(type(payload[key]) is type(value) and payload[key] == value
                                for key, value in expected.items())):
                    return _restore(payload["semantic_plan"], duration)
    except (OSError, ValueError, TypeError, RecursionError):
        pass
    # Never hide a real planning failure or try another provider here.
    result = planner()
    if not isinstance(result, SongContentPlan) or not isinstance(result.semantic_plan, SongSemanticPlan):
        return result
    semantic = result.semantic_plan.to_dict()
    semantic.pop("duration_seconds")
    try:
        restored = _restore(semantic, duration)
        if restored != result or result.semantic_plan.duration_seconds != duration:
            return result
        raw = json.dumps({**expected, "semantic_plan": semantic}, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
        if len(raw) > _MAX_BYTES or not _safe(path):
            return result
        path.parent.mkdir(parents=True, exist_ok=True)
        if not _safe(path):
            return result
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".song-plan-", suffix=".tmp", delete=False) as target:
                temporary = Path(target.name)
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        pass  # Optional persistence must not discard a freshly validated plan.
    return result
