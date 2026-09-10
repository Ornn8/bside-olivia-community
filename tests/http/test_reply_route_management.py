import asyncio
from types import SimpleNamespace

import pytest

from letter_triage import TriageResult, restrict_reply_route
from runtime.video_reply_settings import REPLY_ROUTES, VideoReplySettingsStore, VideoReplySettingsError


def test_independent_preferences_persist_and_old_master_switch_remains_compatible(tmp_path):
    settings = VideoReplySettingsStore.initialize(tmp_path)
    assert settings.routes_snapshot() == dict.fromkeys(REPLY_ROUTES, False)
    settings.mutate("video_reply_setting:legacy", True)
    assert all(settings.routes_snapshot().values())
    routes = dict.fromkeys(REPLY_ROUTES, False); routes["voice_reply"] = True
    settings.mutate_routes("video_reply_setting:new", routes)
    restored = VideoReplySettingsStore(tmp_path)
    assert restored.routes_snapshot() == routes
    assert restored.mutate_routes("video_reply_setting:new", routes)["status"] == "DUPLICATE"
    with pytest.raises(VideoReplySettingsError):
        restored.mutate_routes("video_reply_setting:new", dict.fromkeys(REPLY_ROUTES, True))
    restored.mutate("video_reply_setting:off", False)
    assert not any(restored.routes_snapshot().values())


def test_video_flags_are_independent_persistent_and_part_of_idempotency(tmp_path):
    settings = VideoReplySettingsStore.initialize(tmp_path)
    routes = dict.fromkeys(REPLY_ROUTES, True)
    videos = dict.fromkeys(REPLY_ROUTES, False)
    settings.mutate_routes("video_reply_setting:audio", routes, videos)
    assert VideoReplySettingsStore(tmp_path).videos_snapshot() == videos
    assert settings.routes_snapshot() == routes
    with pytest.raises(VideoReplySettingsError):
        settings.mutate_routes("video_reply_setting:audio", routes, {**videos, "voice_reply": True})
    with pytest.raises(VideoReplySettingsError):
        settings.mutate_routes("video_reply_setting:invalid", routes, {**videos, "voice_reply": "true"})


@pytest.mark.parametrize("route", REPLY_ROUTES)
def test_automatic_routes_never_bypass_individual_switches(route):
    decision = TriageResult("low", route, "synthetic", "completed", True)
    assert restrict_reply_route(decision, {route: True}) is decision
    assert restrict_reply_route(decision, {route: False}).reply_mode == "text_letter"


@pytest.mark.parametrize("requested,context", [
    ("voice_reply", "explicit_voice_reply_request"),
    ("singing_video", "explicit_performance_or_adaptation_request"),
    ("voice_song_video", "explicit_voice_and_song_request"),
])
def test_explicit_disabled_route_requires_confirmation_and_freezes_once(tmp_path, monkeypatch, requested, context):
    import local_server as server
    settings = VideoReplySettingsStore.initialize(tmp_path)
    settings.mutate_routes("video_reply_setting:off", dict.fromkeys(REPLY_ROUTES, False))
    monkeypatch.setattr(server, "video_reply_settings_store", settings)
    monkeypatch.setattr(server.store, "letters", [])
    monkeypatch.setattr(server.store, "request_keys", {})
    monkeypatch.setattr(server, "_reply_route_previews", {})
    monkeypatch.setattr(server, "_persist_store_state", lambda: None)
    monkeypatch.setattr(server, "_schedule_reply_job", lambda *a, **k: None)
    monkeypatch.setattr(server, "_video_reply_dependencies_ready", lambda: True)
    monkeypatch.setattr(server, "_route_readiness", lambda videos=None: dict.fromkeys(REPLY_ROUTES, True))
    calls = []
    async def classify(content):
        calls.append(content)
        return TriageResult("low", requested, "explicit_media_requested", "completed", True, (context,))
    monkeypatch.setattr(server, "emotion_triage", SimpleNamespace(classify=classify))
    async def run():
        preview = await server.route("POST", "/toy/letter/route-preview", {"content": "synthetic"}, {})
        assert preview["data"]["needs_confirmation"] is True
        token = preview["data"]["token"]
        original_videos = settings.videos_snapshot()
        settings.mutate_routes("video_reply_setting:change", settings.routes_snapshot(), dict.fromkeys(REPLY_ROUTES, True))
        stale = await server.route("POST", "/toy/letter/send", {"content":"synthetic", "material":{"route_preview_token":token}}, {}, defer_reply=True)
        assert stale["data"]["error_code"] == "REPLY_ROUTE_PREVIEW_EXPIRED"
        settings.mutate_routes("video_reply_setting:restore", settings.routes_snapshot(), original_videos)
        body = {"content": "synthetic", "material": {"route_preview_token": token}, "idempotency_key": "synthetic-send"}
        rejected = await server.route("POST", "/toy/letter/send", body, {}, defer_reply=True)
        assert rejected["data"]["error_code"] == "REPLY_ROUTE_CONFIRM_REQUIRED"
        assert not server.store.letters
        altered = {**body, "content": "different input"}
        assert (await server.route("POST", "/toy/letter/send", altered, {}, defer_reply=True))["code"] == 409
        body["material"]["route_allow_once"] = requested
        accepted = await server.route("POST", "/toy/letter/send", body, {}, defer_reply=True)
        assert accepted["code"] == 0, accepted
        letter = server.store.letters[0]
        assert letter["music_provider"] == "ace_step_xl_original"
        assert "cover_source_id" not in letter["material"]
        assert letter["route_preflight"]["reply_mode"] == requested
        assert letter["reply_routes"] == {key: key == requested for key in REPLY_ROUTES}
        assert "route_preview_token" not in letter["material"]
        assert not any(settings.routes_snapshot().values())
        assert len(calls) == 1
        again = await server.route("POST", "/toy/letter/send", body, {}, defer_reply=True)
        assert again["data"]["letter_id"] == letter["letter_id"]
        assert len(server.store.letters) == 1
    asyncio.run(run())


def test_explicit_video_requires_once_consent_when_speech_mode_is_audio(tmp_path, monkeypatch):
    import local_server as server
    settings = VideoReplySettingsStore.initialize(tmp_path)
    settings.mutate_routes("video_reply_setting:enabled", dict.fromkeys(REPLY_ROUTES, True))
    monkeypatch.setattr(server, "video_reply_settings_store", settings)
    monkeypatch.setattr(server.store, "letters", [])
    monkeypatch.setattr(server.store, "request_keys", {})
    monkeypatch.setattr(server, "_reply_route_previews", {})
    monkeypatch.setattr(server, "_persist_store_state", lambda: None)
    monkeypatch.setattr(server, "_schedule_reply_job", lambda *a, **k: None)
    monkeypatch.setattr(server, "_video_reply_dependencies_ready", lambda: True)
    monkeypatch.setattr(server, "_route_readiness", lambda videos=None: dict.fromkeys(REPLY_ROUTES, True))
    async def classify(content):
        return TriageResult("normal", "voice_reply", "explicit_media_requested", "completed", True,
                            ("explicit_voice_reply_request", "explicit_video_output_request"))
    monkeypatch.setattr(server, "emotion_triage", SimpleNamespace(classify=classify))
    async def run():
        preview = (await server.route("POST", "/toy/letter/route-preview", {"content":"synthetic"}, {}))["data"]
        assert preview["needs_confirmation"] is False
        assert preview["needs_video_confirmation"] is True
        body = {"content":"synthetic", "material":{"route_preview_token":preview["token"]}}
        blocked = await server.route("POST", "/toy/letter/send", body, {}, defer_reply=True)
        assert blocked["data"]["error_code"] == "REPLY_VIDEO_CONFIRM_REQUIRED"
        assert not server.store.letters
        body["material"]["route_video_once"] = "singing_video"
        assert (await server.route("POST", "/toy/letter/send", body, {}, defer_reply=True))["code"] == 400
        body["material"]["route_video_once"] = "voice_reply"
        assert (await server.route("POST", "/toy/letter/send", body, {}, defer_reply=True))["code"] == 0
        assert server.store.letters[0]["reply_route_videos"]["voice_reply"] is True
        assert settings.videos_snapshot()["voice_reply"] is False
    asyncio.run(run())
