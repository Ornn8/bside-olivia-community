from __future__ import annotations
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest

import video_reply_settings as legacy_video_reply_settings
from runtime.video_reply_settings import VideoReplySettingsError, VideoReplySettingsStore

def test_legacy_module_reexports_canonical_settings() -> None:
    assert legacy_video_reply_settings.VideoReplySettingsStore is VideoReplySettingsStore

@pytest.mark.parametrize("legacy", [False, True])
def test_fresh_store_is_disabled_and_valid_legacy_store_keeps_compatibility(tmp_path, legacy):
    if legacy:
        (tmp_path / "video_reply_settings.json").write_text(
            json.dumps({"schema_version": 1, "settings": {}, "ledger": {}}), encoding="utf-8"
        )
    store = VideoReplySettingsStore(tmp_path) if legacy else VideoReplySettingsStore.initialize(tmp_path)
    assert store.snapshot().to_dict() == {
        "state": "available",
        "enabled": legacy,
    }
    if not legacy:
        assert (tmp_path / "video_reply_settings.initialized").is_file()
def test_open_missing_store_is_unavailable_until_explicit_initialize(tmp_path):
    assert VideoReplySettingsStore(tmp_path).snapshot().state == "unavailable"
    assert not (tmp_path / "video_reply_settings.json").exists()
    assert VideoReplySettingsStore.initialize(tmp_path).snapshot().to_dict() == {"state": "available", "enabled": False}
def test_mutation_is_atomic_namespaced_and_replayed_after_restart(tmp_path):
    store = VideoReplySettingsStore.initialize(tmp_path)
    first = store.mutate("video_reply_setting:same", False)
    assert first.to_dict() == {"request_id": "video_reply_setting:same", "status": "NOOP", "enabled": False}
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
        if calls > 2:
            raise OSError("synthetic")
        path.write_bytes(payload)
    store = VideoReplySettingsStore.initialize(tmp_path, writer=writer)
    with pytest.raises(VideoReplySettingsError) as failed:
        store.mutate("video_reply_setting:write", False)
    assert failed.value.code == "VIDEO_REPLY_SETTING_UNAVAILABLE"
    assert store.snapshot().to_dict() == {"state": "unavailable", "reason_code": "VIDEO_REPLY_SETTING_UNAVAILABLE"}
    assert store.receive_snapshot().enabled is False
    assert VideoReplySettingsStore(tmp_path).snapshot().to_dict() == {"state": "available", "enabled": False}
    store.reload()
    assert store.snapshot().to_dict() == {"state": "available", "enabled": False}
    (tmp_path / "video_reply_settings.json").write_text("not-json", encoding="utf-8")
    assert VideoReplySettingsStore(tmp_path).snapshot().to_dict() == {"state": "unavailable", "reason_code": "VIDEO_REPLY_SETTING_UNAVAILABLE"}

def test_initialize_marker_failure_and_deleted_state_stay_unavailable(tmp_path):
    calls = 0
    def writer(path, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("marker unavailable")
        path.write_bytes(payload)
    with pytest.raises(VideoReplySettingsError):
        VideoReplySettingsStore.initialize(tmp_path, writer=writer)
    assert VideoReplySettingsStore(tmp_path).snapshot().state == "unavailable"
    store = VideoReplySettingsStore.initialize(tmp_path / "ok")
    store.mutate("video_reply_setting:off", False)
    (tmp_path / "ok" / "video_reply_settings.json").unlink()
    assert VideoReplySettingsStore(tmp_path / "ok").snapshot().state == "unavailable"
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
    assert list(validator.iter_errors({"state": "available", "enabled": True}))
    dependency = {
        "id": "cosyvoice",
        "label": "语音合成（CosyVoice 3）",
        "state": "missing",
        "install_mode": "manual",
        "source_summary": "国内：ModelScope；备用：GitHub / Hugging Face",
        "sources": [
            {
                "id": "domestic",
                "label": "国内源（ModelScope）",
            },
            {
                "id": "official",
                "label": "官方源（Hugging Face）",
            },
        ],
    }
    assert not list(validator.iter_errors({
        "state": "available",
        "enabled": True,
        "effective_enabled": False,
        "ready": False,
        "dependencies": [dependency],
    }))
    assert not list(validator.iter_errors({
        "code": 409,
        "message": "missing",
        "data": {
            "status": "FAILED",
            "error_code": "VIDEO_REPLY_DEPENDENCIES_MISSING",
            "retryable": False,
            "missing_dependencies": ["cosyvoice"],
        },
    }))
    assert not list(validator.iter_errors({"state": "unavailable", "reason_code": "SETTING_UNAVAILABLE"}))
    assert list(validator.iter_errors({"state": "available", "enabled": True, "reason_code": "X"}))
    assert not list(validator.iter_errors({"request_id": "video_reply_setting:x", "enabled": False}))
    assert not list(validator.iter_errors({"request_id": "video_reply_setting:x", "status": "DUPLICATE", "enabled": False}))
    assert not list(validator.iter_errors({"capability": "cosyvoice", "source": "domestic"}))
    assert not list(validator.iter_errors({"status": "OPENED", "capability": "cosyvoice", "source": "domestic"}))
    for value in ({"request_id": "letter:x", "enabled": False}, {"request_id": "video_reply_setting:x", "enabled": False, "extra": 1}, {"request_id": "video_reply_setting:x", "status": "FAILED", "enabled": False}, {"code": 400, "message": "conflict", "data": {"status": "FAILED", "error_code": "VIDEO_REPLY_SETTING_REQUEST_CONFLICT", "retryable": False}}, {"code": 503, "message": "unavailable", "data": {"status": "UNAVAILABLE", "error_code": "VIDEO_REPLY_SETTING_UNAVAILABLE", "retryable": False}}):
        assert list(validator.iter_errors(value))
@pytest.mark.parametrize("value", ["bare", "letter:shared", "memory:shared"])
def test_request_namespace_is_enforced(tmp_path, value):
    with pytest.raises(VideoReplySettingsError) as invalid:
        VideoReplySettingsStore.initialize(tmp_path).mutate(value, False)
    assert invalid.value.code == "VIDEO_REPLY_SETTING_REQUEST_ID_INVALID"
def test_handler_errors_match_video_reply_schema(monkeypatch, tmp_path):
    import local_server
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from jsonschema import Draft202012Validator
    settings = VideoReplySettingsStore.initialize(tmp_path)
    monkeypatch.setattr(local_server, "video_reply_settings_store", settings)
    validator = Draft202012Validator(json.loads(Path("contracts/video_reply_settings.schema.json").read_text(encoding="utf-8")))
    async def calls():
        app = web.Application(); app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app)) as client:
            requests = [
                client.put("/toy/settings/video-reply"),
                client.post("/toy/settings/video-reply", data="{"),
                client.post("/toy/settings/video-reply", json=[]),
                client.post("/toy/settings/video-reply", json={}),
                client.post("/toy/settings/video-reply", json={"enabled": False}),
                client.post("/toy/settings/video-reply", json={"enabled": False, "request_id": "letter:shared"}),
            ]
            responses = []; preflight = await client.options("/toy/settings/video-reply", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "X-Olivia-Companion-Action"})
            for request in requests:
                response = await request
                responses.append((response.status, await response.json()))
            return responses, preflight
    responses, preflight = asyncio.run(calls())
    assert preflight.status == 204 and "x-olivia-companion-action" in preflight.headers["Access-Control-Allow-Headers"].lower()
    assert [status for status, _ in responses] == [405, 400, 400, 400, 400, 400]
    assert [response["data"]["error_code"] for _, response in responses] == ["METHOD_NOT_ALLOWED", "INVALID_JSON", "INVALID_BODY", "MISSING_FIELD", "MISSING_FIELD", "VIDEO_REPLY_SETTING_REQUEST_ID_INVALID"]
    for status, response in responses:
        assert not list(validator.iter_errors(response)), response
def test_factory_init_failure_returns_stable_unavailable(monkeypatch):
    import local_server
    def fail(_root):
        raise VideoReplySettingsError("VIDEO_REPLY_SETTING_UNAVAILABLE")
    monkeypatch.setattr(local_server.VideoReplySettingsStore, "initialize", fail)
    store = local_server._create_video_reply_settings_store()
    assert store.snapshot().to_dict() == {"state": "unavailable", "reason_code": "VIDEO_REPLY_SETTING_UNAVAILABLE"}
def test_factory_path_resolution_failure_is_fail_closed(monkeypatch):
    import local_server
    monkeypatch.setattr(local_server, "_state_root", lambda: (_ for _ in ()).throw(OSError("path")))
    assert local_server._create_video_reply_settings_store().snapshot().state == "unavailable"
def test_route_and_receive_snapshot_off_are_server_enforced(tmp_path, monkeypatch):
    import local_server
    from reply_orchestrator import ReplyState
    from runtime.reply.reply_pipeline import PipelineResult
    settings = VideoReplySettingsStore.initialize(tmp_path)
    monkeypatch.setattr(local_server, "video_reply_settings_store", settings)
    async def route_check():
        result = await local_server.route("POST", "/toy/settings/video-reply", {"enabled": False, "request_id": "video_reply_setting:ui"}, {})
        assert result["data"]["status"] == "NOOP"
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


def test_video_reply_enable_is_blocked_and_receive_fails_closed_when_dependencies_are_missing(
    tmp_path, monkeypatch
):
    import local_server

    settings = VideoReplySettingsStore.initialize(tmp_path)
    settings.mutate("video_reply_setting:previously-enabled", True)
    monkeypatch.setattr(local_server, "video_reply_settings_store", settings)
    monkeypatch.setattr(
        local_server,
        "video_reply_dependency_status",
        lambda _environment, *, performance_video_path: {
            "ready": False,
            "dependencies": [
                {
                    "id": "cosyvoice",
                    "label": "语音合成（CosyVoice 3）",
                    "state": "missing",
                    "install_mode": "manual",
                },
                {
                    "id": "latentsync",
                    "label": "口型视频（LatentSync）",
                    "state": "ready",
                    "install_mode": "manual",
                },
            ],
        },
    )
    monkeypatch.setattr(local_server, "_current_music_performance", lambda _environment: None)

    async def route_check():
        status = await local_server.route("GET", "/toy/settings/video-reply", {}, {})
        blocked = await local_server.route(
            "POST",
            "/toy/settings/video-reply",
            {"enabled": True, "request_id": "video_reply_setting:missing"},
            {},
        )
        disabled = await local_server.route(
            "POST",
            "/toy/settings/video-reply",
            {"enabled": False, "request_id": "video_reply_setting:disable"},
            {},
        )
        monkeypatch.setattr(local_server, "_schedule_reply_job", lambda *_a, **_k: None)
        sent = await local_server.route(
            "POST", "/toy/letter/send", {"content": "synthetic missing media"}, {}, defer_reply=True
        )
        letter = next(
            item for item in local_server.store.letters
            if item["letter_id"] == sent["data"]["letter_id"]
        )
        return status, blocked, disabled, letter

    status, blocked, disabled, letter = asyncio.run(route_check())
    try:
        assert status["data"] == {
            "state": "available",
            "enabled": True,
            "effective_enabled": False,
            "ready": False,
            "dependencies": [
                {
                    "id": "cosyvoice",
                    "label": "语音合成（CosyVoice 3）",
                    "state": "missing",
                    "install_mode": "manual",
                },
                {
                    "id": "latentsync",
                    "label": "口型视频（LatentSync）",
                    "state": "ready",
                    "install_mode": "manual",
                },
            ],
        }
        assert blocked["code"] == 409
        assert blocked["data"] == {
            "status": "FAILED",
            "error_code": "VIDEO_REPLY_DEPENDENCIES_MISSING",
            "retryable": False,
            "missing_dependencies": ["cosyvoice"],
        }
        assert disabled["data"]["enabled"] is False
        assert settings.snapshot().enabled is False
        assert letter["video_reply_enabled"] is False
    finally:
        local_server.store.letters.remove(letter)


def test_video_reply_source_page_is_opened_only_through_the_server_allowlist(monkeypatch):
    import local_server

    opened = []
    monkeypatch.setattr(
        local_server,
        "_open_video_capability_source",
        lambda capability, source: opened.append((capability, source)) or True,
    )

    async def route_check():
        success = await local_server.route(
            "POST",
            "/toy/capabilities/video/source",
            {"capability": "cosyvoice", "source": "domestic"},
            {},
        )
        roformer = await local_server.route(
            "POST",
            "/toy/capabilities/video/source",
            {"capability": "roformer", "source": "domestic"},
            {},
        )
        return success, roformer

    success, roformer = asyncio.run(route_check())
    assert success["data"] == {
        "status": "OPENED",
        "capability": "cosyvoice",
        "source": "domestic",
    }
    assert roformer["data"] == {
        "status": "OPENED",
        "capability": "roformer",
        "source": "domestic",
    }
    assert opened == [("cosyvoice", "domestic"), ("roformer", "domestic")]
def test_recovery_reads_letter_snapshot_and_legacy_defaults_enabled(monkeypatch):
    import local_server
    off = {"letter_id": "off", "content": "x", "reply_text": "r", "letter_status": "COMPLETED", "reply_mode": "spoken_video", "media_status": "QUEUED", "video_reply_enabled": False}
    legacy = {"letter_id": "legacy", "content": "x", "reply_text": "r", "letter_status": "COMPLETED", "reply_mode": "spoken_video", "media_status": "QUEUED"}
    enabled = {**legacy, "letter_id": "enabled", "video_reply_enabled": True}
    malformed = {**legacy, "letter_id": "malformed", "video_reply_enabled": "true"}
    scheduled = []
    monkeypatch.setattr(local_server, "_schedule_media_job", lambda lid, *_a: scheduled.append(lid))
    original = local_server.store.letters[:]
    local_server.store.letters[:] = [off, legacy, enabled, malformed]
    try:
        assert local_server._schedule_pending_media_jobs() == 2 and scheduled == ["legacy", "enabled"]
    finally:
        local_server.store.letters[:] = original
