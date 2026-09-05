import json
import io
import zipfile
from dataclasses import replace

import pytest

from runtime.media.song_content import SongContentPlan, parse_song_semantic_plan
from runtime.media.music_caption import render_minimax_caption
from runtime.media.song_plan_cache import cached_song_plan
from tests.media.test_song_content_pipeline import _payload


def plan(duration=40):
    semantic = parse_song_semantic_plan(json.dumps(_payload(duration)), duration)
    return SongContentPlan(semantic.emotion_arc.value, semantic.lyrics,
                           render_minimax_caption(semantic), duration, semantic_plan=semantic)


def test_retry_reuses_verified_plan_before_planner(tmp_path):
    calls = []
    path = tmp_path / "stages/song-plan.private.json"
    first = cached_song_plan(path, "letter", "reply", 40, lambda: calls.append(1) or plan())
    second = cached_song_plan(path, "letter", "reply", 40, lambda: pytest.fail("must not replan"))
    assert second == first
    assert calls == [1]
    assert "caption" not in json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("change", ["content", "reply", "duration", "broken", "lyrics", "caption", "oversized", "version", "deep"])
def test_invalid_or_changed_cache_replans(tmp_path, change):
    path = tmp_path / "song-plan.private.json"
    cached_song_plan(path, "letter", "reply", 40, plan)
    content, reply, duration = "letter", "reply", 40
    if change == "content": content = "changed"
    elif change == "reply": reply = "changed"
    elif change == "duration": duration = 60
    elif change == "broken": path.write_text("{")
    elif change == "oversized": path.write_text("x" * 65537)
    elif change == "deep": path.write_text("[" * 2000 + "]" * 2000)
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if change == "lyrics": payload["semantic_plan"]["lyrics"] = "arbitrary unvalidated text"
        if change == "caption": payload["caption"] = "injected instruction"
        if change == "version": payload["planner_version"] = -1
        path.write_text(json.dumps(payload))
    calls = []
    result = cached_song_plan(path, content, reply, duration, lambda: calls.append(1) or plan(duration))
    assert calls == [1]
    assert result.duration_seconds == duration


def test_unverified_compatibility_plan_is_not_persisted_and_planner_failure_propagates(tmp_path):
    path = tmp_path / "song-plan.private.json"
    legacy = replace(plan(), semantic_plan=None)
    assert cached_song_plan(path, "letter", "reply", 40, lambda: legacy) is legacy
    assert not path.exists()
    def fail(): raise RuntimeError("synthetic-planning-failure")
    with pytest.raises(RuntimeError, match="synthetic-planning-failure"):
        cached_song_plan(path, "letter", "reply", 40, fail)


def test_plan_with_arbitrary_caption_is_not_promoted_to_verified_cache(tmp_path):
    path = tmp_path / "song-plan.private.json"
    invalid = replace(plan(), caption="unvalidated instructions")
    assert cached_song_plan(path, "letter", "reply", 40, lambda: invalid) is invalid
    assert not path.exists()


def test_failed_atomic_save_keeps_previous_cache_and_valid_new_plan(tmp_path, monkeypatch):
    import runtime.media.song_plan_cache as cache
    path = tmp_path / "song-plan.private.json"
    cached_song_plan(path, "letter", "reply", 40, plan)
    original = path.read_bytes()
    def fail(*args): raise OSError("synthetic disk failure")
    monkeypatch.setattr(cache.os, "replace", fail)
    result = cached_song_plan(path, "changed", "reply", 40, plan)
    assert result == plan()
    assert path.read_bytes() == original
    assert list(tmp_path.glob(".song-plan-*.tmp")) == []


def test_private_song_plan_is_excluded_from_diagnostic_projection(tmp_path):
    from runtime.diagnostics.support_bundle import build_diagnostic_bundle, DIAGNOSTIC_BUNDLE_MEMBERS
    from tests.http.test_diagnostic_support_bundle import _source
    path = tmp_path / "stage/song-plan.private.json"
    cached_song_plan(path, "private-source-text", "private-reply-text", 40, plan)
    source = _source()
    source["song_plan_cache"] = path.read_text(encoding="utf-8")
    source["tasks"]["items"][0]["song_plan"] = source["song_plan_cache"]
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        assert tuple(archive.namelist()) == DIAGNOSTIC_BUNDLE_MEMBERS
        exported = b"".join(archive.read(name) for name in archive.namelist())
    assert plan().lyrics.encode("utf-8") not in exported
    assert b"song-plan.private" not in exported
    assert b"private-source-text" not in exported
