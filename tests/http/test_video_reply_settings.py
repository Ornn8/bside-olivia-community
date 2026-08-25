from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from video_reply_settings import VideoReplySettingsError, VideoReplySettingsStore


@pytest.mark.parametrize("legacy", [False, True])
def test_default_and_valid_legacy_store_are_enabled(tmp_path, legacy):
    if legacy:
        (tmp_path / "video_reply_settings.json").write_text(
            json.dumps({"schema_version": 1, "settings": {}, "ledger": {}}), encoding="utf-8"
        )
    store = VideoReplySettingsStore(tmp_path) if legacy else VideoReplySettingsStore.initialize(tmp_path)
    assert store.snapshot().to_dict() == {"state": "available", "enabled": True}
def test_open_missing_store_is_unavailable_until_explicit_initialize(tmp_path):
    assert VideoReplySettingsStore(tmp_path).snapshot().state == "unavailable"
    assert not (tmp_path / "video_reply_settings.json").exists()
    assert VideoReplySettingsStore.initialize(tmp_path).snapshot().to_dict() == {"state": "available", "enabled": True}
def test_mutation_is_atomic_namespaced_and_replayed_after_restart(tmp_path):
    store = VideoReplySettingsStore.initialize(tmp_path)
    first = store.mutate("video_reply_setting:same", False)
    assert first.to_dict() == {"request_id": "video_reply_setting:same", "status": "APPLIED", "enabled": False}
    assert VideoReplySettingsStore(tmp_path).mutate("video_reply_setting:same", False).to_dict() == first.to_dict()
    with pytest.raises(VideoReplySettingsError) as conflict:
        store.mutate("video_reply_setting:same", True)
    assert (conflict.value.code, conflict.value.status) == ("VIDEO_REPLY_SETTING_REQUEST_CONFLICT", 409)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.mutate("video_reply_setting:concurrent", False), range(4)))
    assert {result.status for result in results} == {"NOOP"}


def test_write_failure_keeps_committed_snapshot_and_corrupt_store_fails_closed(tmp_path):
    calls = 0
    def writer(path, payload):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("synthetic")
        path.write_bytes(payload)
    store = VideoReplySettingsStore.initialize(tmp_path, writer=writer)
    with pytest.raises(VideoReplySettingsError) as failed:
        store.mutate("video_reply_setting:write", False)
    assert failed.value.code == "VIDEO_REPLY_SETTING_UNAVAILABLE"
    assert store.snapshot().to_dict() == {"state": "unavailable", "reason_code": "VIDEO_REPLY_SETTING_UNAVAILABLE"}
    assert store.receive_snapshot().enabled is False
    assert VideoReplySettingsStore(tmp_path).snapshot().to_dict() == {"state": "available", "enabled": True}
    store.reload()
    assert store.snapshot().to_dict() == {"state": "available", "enabled": True}
    (tmp_path / "video_reply_settings.json").write_text("not-json", encoding="utf-8")
    assert VideoReplySettingsStore(tmp_path).snapshot().to_dict() == {"state": "unavailable", "reason_code": "VIDEO_REPLY_SETTING_UNAVAILABLE"}


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_non_boolean_store_is_unavailable(tmp_path, value):
    (tmp_path / "video_reply_settings.json").write_text(
        json.dumps({"schema_version": 1, "settings": {"video_reply_enabled": value}, "ledger": {}}),
        encoding="utf-8",
    )
    assert VideoReplySettingsStore(tmp_path).snapshot().state == "unavailable"


def test_schema_rejects_mixed_variant_and_accepts_both_closed_variants():
    from jsonschema import Draft202012Validator
    schema = json.loads(Path("contracts/video_reply_settings.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    assert not list(validator.iter_errors({"state": "available", "enabled": True}))
    assert not list(validator.iter_errors({"state": "unavailable", "reason_code": "SETTING_UNAVAILABLE"}))
    assert list(validator.iter_errors({"state": "available", "enabled": True, "reason_code": "X"}))


@pytest.mark.parametrize("value", ["bare", "letter:shared", "memory:shared"])
def test_request_namespace_is_enforced(tmp_path, value):
    with pytest.raises(VideoReplySettingsError) as invalid:
        VideoReplySettingsStore.initialize(tmp_path).mutate(value, False)
    assert invalid.value.code == "VIDEO_REPLY_SETTING_REQUEST_ID_INVALID"
def test_route_accepts_only_body_request_id_and_surfaces_unavailable(monkeypatch, tmp_path):
    import local_server
    settings = VideoReplySettingsStore.initialize(tmp_path)
    monkeypatch.setattr(local_server, "video_reply_settings_store", settings)
    async def calls():
        alias = await local_server.route("POST", "/toy/settings/video-reply", {"enabled": False, "idempotency_key": "letter:shared"}, {"request_id": "video_reply_setting:query"})
        bad = await local_server.route("POST", "/toy/settings/video-reply", {"enabled": "false", "request_id": "video_reply_setting:type"}, {})
        await local_server.route("POST", "/toy/settings/video-reply", {"enabled": False, "request_id": "video_reply_setting:conflict"}, {})
        conflict = await local_server.route("POST", "/toy/settings/video-reply", {"enabled": True, "request_id": "video_reply_setting:conflict"}, {})
        monkeypatch.setattr(local_server, "video_reply_settings_store", VideoReplySettingsStore(tmp_path / "missing"))
        unavailable = await local_server.route("POST", "/toy/settings/video-reply", {"enabled": False, "request_id": "video_reply_setting:unavailable"}, {})
        assert (await local_server.route("GET", "/toy/settings/video-reply", {}, {}))["data"]["state"] == "unavailable" and local_server._health_result()["data"]["capabilities"]["settings.video_reply"]["status"] == "unavailable"
        return alias, bad, conflict, unavailable
    alias, bad, conflict, unavailable = asyncio.run(calls())
    assert [alias["code"], bad["code"], conflict["code"], unavailable["code"]] == [400, 400, 409, 503]
def test_route_and_receive_snapshot_off_are_server_enforced(tmp_path, monkeypatch):
    import local_server
    from reply_orchestrator import ReplyState
    from reply_pipeline import PipelineResult
    settings = VideoReplySettingsStore.initialize(tmp_path)
    monkeypatch.setattr(local_server, "video_reply_settings_store", settings)
    async def route_check():
        result = await local_server.route("POST", "/toy/settings/video-reply", {"enabled": False, "request_id": "video_reply_setting:ui"}, {})
        assert result["data"]["status"] == "APPLIED"
        monkeypatch.setattr(local_server, "_schedule_reply_job", lambda *_a, **_k: None)
        sent = await local_server.route("POST", "/toy/letter/send", {"content": "synthetic"}, {}, defer_reply=True)
        return next(item for item in local_server.store.letters if item["letter_id"] == sent["data"]["letter_id"])
    letter = asyncio.run(route_check())
    class Router:
        async def classify(self, *_a):
            raise AssertionError("router called")
    class Pipeline:
        async def run(self, _request, context):
            assert context.mode.value == "text_letter"
            return PipelineResult(letter["letter_id"], ReplyState.COMPLETED, text="text")
    monkeypatch.setattr(local_server, "emotion_triage", Router())
    monkeypatch.setattr(local_server, "reply_pipeline", Pipeline())
    monkeypatch.setattr(local_server.letters_adapter, "remember_conversation", lambda *_: None)
    try:
        assert asyncio.run(local_server.generate_reply(letter["letter_id"], letter["content"]))
        assert letter["video_reply_enabled"] is False and "media_status" not in letter
    finally:
        local_server.store.letters.remove(letter)


def test_recovery_reads_letter_snapshot_and_legacy_defaults_enabled(monkeypatch):
    import local_server
    off = {"letter_id": "off", "content": "x", "reply_text": "r", "letter_status": "COMPLETED", "reply_mode": "spoken_video", "media_status": "QUEUED", "video_reply_enabled": False}
    legacy = {"letter_id": "legacy", "content": "x", "reply_text": "r", "letter_status": "COMPLETED", "reply_mode": "spoken_video", "media_status": "QUEUED"}
    scheduled = []
    monkeypatch.setattr(local_server, "_schedule_media_job", lambda lid, *_a: scheduled.append(lid))
    original = local_server.store.letters[:]
    local_server.store.letters[:] = [off, legacy]
    try:
        assert local_server._schedule_pending_media_jobs() == 1 and scheduled == ["legacy"]
    finally:
        local_server.store.letters[:] = original
