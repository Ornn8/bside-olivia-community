import json
from pathlib import Path
import subprocess

import pytest

from runtime.media import ace_cover
from runtime.media.cover_upload import source_path


def test_portable_paths_follow_installation_and_ignore_cwd(tmp_path, monkeypatch):
    environment = {"OLIVIA_PROJECT_ROOT": str(tmp_path), "OLIVIA_LOCAL_DATA_ROOT": "install/data"}
    paths = ace_cover.cover_paths(environment)
    assert paths["python"] == tmp_path / "install/data/capabilities/ace-cover/runtime/python.exe"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert ace_cover.cover_paths(environment) == paths
    environment["OLIVIA_PROJECT_ROOT"] = str(elsewhere)
    assert ace_cover.cover_paths(environment)["python"].is_relative_to(elsewhere)


def test_missing_source_never_starts_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(ace_cover, "run_managed_process", lambda *a, **k: pytest.fail("must not generate without audio"))
    with pytest.raises(ace_cover.CoverError, match="COVER_SOURCE_REQUIRED"):
        ace_cover.generate_cover(None, tmp_path / "out.wav", environment={})
    for value in ("../secret", "a" * 31, str(tmp_path), None):
        with pytest.raises(ValueError, match="COVER_SOURCE_REQUIRED"):
            source_path(tmp_path, value)


def test_single_worker_uses_configured_assets_and_no_generation_retry(tmp_path, monkeypatch):
    environment = {"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path)}
    paths = ace_cover.cover_paths(environment)
    monkeypatch.setattr(ace_cover, "cover_configured", lambda env: True)
    source = tmp_path / "source.wav"
    source.write_bytes(b"input")
    calls = []
    def worker(command, **kwargs):
        request = json.loads(Path(command[-1]).read_text(encoding="utf8"))
        calls.append(request)
        assert request["source"] == str(source)
        assert request["voice_lora"] == str(paths["voice_lora"])
        assert Path(kwargs["env"]["TEMP"]).is_relative_to(tmp_path)
        return subprocess.CompletedProcess(command, 1, b"", b"failure")
    monkeypatch.setattr(ace_cover, "run_managed_process", worker)
    with pytest.raises(ace_cover.CoverError, match="COVER_GENERATION_FAILED"):
        ace_cover.generate_cover(source, tmp_path / "out.wav", environment=environment, lyrics="synthetic")
    assert len(calls) == 1
    assert not (tmp_path / "out.wav").exists()


@pytest.mark.parametrize("video,spoken,expected", [(False, False, ["cover"]),
    (True, False, ["cover", "separate_output", "lipsync"]), (True, True, ["cover", "separate_output", "lipsync", "tts_video", "concat"])])
def test_cover_delivery_preserves_selected_stages(tmp_path, monkeypatch, video, spoken, expected):
    from runtime.media import cover_reply
    calls = []
    def generate(source, output, **kwargs):
        calls.append("cover")
        output.write_bytes(b"cover audio")
        return {}
    def face(scene, vocals, mixed, output, **kwargs):
        calls.append("lipsync")
        assert vocals != mixed
        output.write_bytes(b"video")
    monkeypatch.setattr(cover_reply, "generate_cover", generate)
    monkeypatch.setattr(cover_reply, "separate_vocals", lambda *a, **k: calls.append("separate_output"))
    monkeypatch.setattr(cover_reply, "render_full_face_performance", face)
    monkeypatch.setattr(cover_reply, "render_reply_video", lambda *a, **k: calls.append("tts_video"))
    monkeypatch.setattr(cover_reply, "concat_videos", lambda *a, **k: calls.append("concat"))
    scene = tmp_path / "scene.mp4"
    scene.touch()
    cover_reply.render_cover_reply("", "", tmp_path / "out.wav", source_audio=scene,
        environment={}, render_video=video, include_spoken=spoken, normal_video_path=tmp_path / "speech.mp4",
        song_video_path=tmp_path / "song.mp4", official_reply_reference_path=scene,
        tts_config_path=scene, visual_config_path=scene, worker_path=scene,
        performance_video_path=scene, spoken_action_base_path=scene)
    assert calls == expected


def test_upload_checks_confirmation_origin_and_decodes_audio(tmp_path, monkeypatch):
    import asyncio
    import io
    import wave
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    import local_server
    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    data = io.BytesIO()
    with wave.open(data, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 1600)
    async def run():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app)) as client:
            assert (await client.post("/toy/cover/upload", data=data.getvalue())).status == 403
            headers = {"X-Olivia-Companion-Action": "confirmed"}
            blocked = await client.post("/toy/cover/upload", data=data.getvalue(), headers={**headers, "Origin":"https://evil.invalid"})
            assert blocked.status == 403
            response = await client.post("/toy/cover/upload", data=data.getvalue(), headers=headers)
            assert response.status == 200
            result = (await response.json())["data"]
            assert result["duration_seconds"] == pytest.approx(.1)
            assert source_path(tmp_path, result["source_id"]).is_file()
            invalid = await client.post("/toy/cover/upload", data=b"not audio", headers=headers)
            assert invalid.status == 400
    asyncio.run(run())


def test_completed_song_is_reused_after_a_later_stage_failure(tmp_path, monkeypatch):
    environment = {"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path)}
    monkeypatch.setattr(ace_cover, "cover_configured", lambda env: True)
    source = tmp_path / "source.wav"
    source.write_bytes(b"input")
    calls = []
    def worker(command, **kwargs):
        request_path = Path(command[-1])
        request = json.loads(request_path.read_text(encoding="utf8"))
        calls.append(request)
        Path(request["output"]).write_bytes(b"completed audio")
        (request_path.parent / "progress.json").write_text(json.dumps({"stage":"completed"}))
        return subprocess.CompletedProcess(command, 0, b"", b"")
    monkeypatch.setattr(ace_cover, "run_managed_process", worker)
    output = tmp_path / "out.wav"
    ace_cover.generate_cover(source, output, environment=environment, lyrics="synthetic")
    reused = ace_cover.generate_cover(source, output, environment=environment, lyrics="synthetic")
    assert reused["music_stage"] == "reused"
    assert len(calls) == 1
