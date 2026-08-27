from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import music_reply
from music_caption import render_minimax_caption
from song_content import (
    PianoTexture,
    SongContentPlan,
    SongDynamicArc,
    SongEmotionArc,
    SongEnding,
    SongSemanticPlan,
    VocalDelivery,
)


def _semantic_plan() -> SongSemanticPlan:
    lyrics = "\n".join(
        (
            "[Intro]",
            "[Verse]",
            "灯还亮着你先坐一会",
            "不用急着把答案说完",
            "窗外的风慢慢停下来",
            "我把声音放得轻一点",
            "今晚先照顾眼前一步",
            "剩下的事明天再想吧",
            "[Chorus]",
            "你不需要立刻变勇敢",
            "难过也有自己的位置",
            "等呼吸重新变得安稳",
            "再把心事一点点收好",
            "我会认真听你说下去",
            "这一晚就先这样过去",
            "[Outro]",
        )
    )
    return SongSemanticPlan(
        emotion_arc=SongEmotionArc.GENTLE_REASSURANCE,
        piano_texture=PianoTexture.TRANSPARENT_BROKEN_CHORDS,
        vocal_delivery=VocalDelivery.CLEAR_LEGATO,
        dynamic_arc=SongDynamicArc.SOFT_GENTLE_RISE_SETTLE,
        ending=SongEnding.COMPLETE_SOFT_CADENCE,
        lyrics=lyrics,
        duration_seconds=40,
    )


def _write(path: Path, data: bytes = b"synthetic") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _value_after(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


def test_prepare_official_spoken_base_uses_only_first_35_seconds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = _write(tmp_path / "official-complete-reply.mp4")
    destination = tmp_path / "stages" / "official-spoken-000-035s.mp4"
    observed: list[tuple[list[str], str]] = []

    monkeypatch.setattr(music_reply, "_ffmpeg", lambda *_args: "ffmpeg")

    def fake_run(command, error_code, *, timeout=900.0):
        del timeout
        observed.append((list(command), error_code))
        _write(Path(command[-1]), b"official-spoken")

    monkeypatch.setattr(music_reply, "_run", fake_run)

    result = music_reply.prepare_official_spoken_base(reference, destination)

    assert result == destination
    assert destination.read_bytes() == b"official-spoken"
    command, error_code = observed[0]
    assert error_code == "MUSIC_REPLY_SPOKEN_REFERENCE_FAILED"
    assert _value_after(command, "-ss") == "0"
    assert _value_after(command, "-t") == "35"
    assert _value_after(command, "-i") == str(reference)
    assert "-an" in command


def test_concat_videos_inserts_silent_transition_between_spoken_and_performance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normal = _write(tmp_path / "normal.mp4")
    song = _write(tmp_path / "song.mp4")
    transition = _write(tmp_path / "transition.mp4")
    output = tmp_path / "final.mp4"
    frames = {normal: 100, song: 200}
    commands: list[tuple[list[str], str]] = []

    monkeypatch.setattr(music_reply, "_ffmpeg", lambda *_args: "ffmpeg")
    monkeypatch.setattr(
        music_reply,
        "_target_frame_count",
        lambda path, fps=25: frames[Path(path)],
    )

    def fake_run(command, error_code, *, timeout=900.0):
        del timeout
        commands.append((list(command), error_code))
        _write(Path(command[-1]), b"rendered")

    monkeypatch.setattr(music_reply, "_run", fake_run)

    music_reply.concat_videos(
        normal,
        song,
        output,
        transition_video_path=transition,
    )

    assert output.read_bytes() == b"rendered"
    assert [code for _command, code in commands] == [
        "MUSIC_REPLY_ASSEMBLY_FAILED",
        "MUSIC_REPLY_CONCAT_FAILED",
    ]
    assembly = commands[0][0]
    filters = _value_after(assembly, "-filter_complex")
    assert "[normal_v][normal_a][transition_v][transition_a][song_v][song_a]" in filters
    assert "concat=n=3:v=1:a=1[joined_v][joined_a]" in filters
    assert "anullsrc=r=44100:cl=stereo" in filters
    assert "trim=start=35.000000:end=43.000000" in filters
    assert assembly[:8] == [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(normal),
        "-i",
    ]
    assert str(song) in assembly
    assert str(transition) in assembly

    final = commands[1][0]
    assert _value_after(final, "-frames:v") == "500"
    assert _value_after(final, "-t") == "20.000000"


def test_concat_videos_without_transition_preserves_two_segment_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normal = _write(tmp_path / "normal.mp4")
    song = _write(tmp_path / "song.mp4")
    output = tmp_path / "final.mp4"
    frames = {normal: 125, song: 250}
    commands: list[list[str]] = []

    monkeypatch.setattr(music_reply, "_ffmpeg", lambda *_args: "ffmpeg")
    monkeypatch.setattr(
        music_reply,
        "_target_frame_count",
        lambda path, fps=25: frames[Path(path)],
    )

    def fake_run(command, error_code, *, timeout=900.0):
        del error_code, timeout
        commands.append(list(command))
        _write(Path(command[-1]), b"rendered")

    monkeypatch.setattr(music_reply, "_run", fake_run)
    music_reply.concat_videos(normal, song, output)

    filters = _value_after(commands[0], "-filter_complex")
    assert "[normal_v][normal_a][song_v][song_a]" in filters
    assert "concat=n=2:v=1:a=1[joined_v][joined_a]" in filters
    assert "fade=t=out:st=13.000000:d=2.000000" in filters
    assert "afade=t=out:st=13.000000:d=2.000000" in filters
    assert "transition_v" not in filters
    assert _value_after(commands[1], "-frames:v") == "375"
    assert _value_after(commands[1], "-t") == "15.000000"


def test_render_musical_reply_keeps_spoken_then_transition_then_performance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    semantic = _semantic_plan()
    caption = render_minimax_caption(semantic)
    compatibility_plan = SongContentPlan(
        emotion=semantic.emotion_arc.value,
        lyrics=semantic.lyrics,
        caption=caption,
        duration_seconds=semantic.duration_seconds,
    )
    output = tmp_path / "final.mp4"
    normal = tmp_path / "spoken.mp4"
    song_video = tmp_path / "performance.mp4"
    official_reference = _write(tmp_path / "official-complete-reply.mp4")
    official_spoken = (
        tmp_path
        / "final-music-v2-40s-stages"
        / "official-spoken-000-035s.mp4"
    )
    transition = _write(tmp_path / "official-transition.mp4")
    order: list[str] = []
    observed: dict[str, object] = {}

    monkeypatch.setenv("OLIVIA_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("OLIVIA_FFMPEG_EXE", str(_write(tmp_path / "ffmpeg.exe")))
    monkeypatch.setenv("OLIVIA_PROVIDER_CACHE_ROOT", str(tmp_path / "provider-cache"))
    monkeypatch.setenv("OLIVIA_MINIMAX_COMFY_PYTHON", "synthetic-python")
    monkeypatch.setenv("OLIVIA_MINIMAX_COMFY_ROOT", "synthetic-root")
    monkeypatch.setenv("OLIVIA_MINIMAX_WORKER", "synthetic-worker")
    monkeypatch.setenv("OLIVIA_LATENTSYNC_PYTHON", "synthetic-python")
    monkeypatch.setenv("OLIVIA_LATENTSYNC_ROOT", "synthetic-root")

    def fake_plan(content, reply_text, duration_seconds):
        order.append("plan")
        observed["plan_inputs"] = (content, reply_text, duration_seconds)
        return compatibility_plan

    def fake_normal(text, destination, **kwargs):
        order.append("spoken")
        observed["spoken"] = (text, Path(destination), kwargs)
        _write(Path(destination), b"spoken")
        return {"spoken_stage": "completed"}

    def fake_generate(self, content, reply_text, destination, **kwargs):
        del self
        order.append("minimax")
        observed["minimax"] = (content, reply_text, Path(destination), kwargs)
        _write(Path(destination), b"song")
        return {"music_stage": "completed"}

    def fake_separate(source, destination, **_kwargs):
        order.append("separate")
        observed["separate"] = (Path(source), Path(destination))
        _write(Path(destination), b"vocals")

    def fake_face(base, vocals, full_song, destination, **_kwargs):
        order.append("performance")
        observed["performance"] = (
            Path(base),
            Path(vocals),
            Path(full_song),
            Path(destination),
        )
        _write(Path(destination), b"performance")
        return {"performance_stage": "completed"}

    def fake_concat(spoken_path, performance_path, destination, **kwargs):
        order.append("concat")
        observed["concat"] = (
            Path(spoken_path),
            Path(performance_path),
            Path(destination),
            kwargs,
        )
        _write(Path(destination), b"final")

    monkeypatch.setattr(music_reply, "plan_song_content", fake_plan)
    monkeypatch.setattr(
        music_reply,
        "prepare_official_spoken_base",
        lambda reference, destination, **_kwargs: (
            observed.setdefault(
                "spoken_reference", (Path(reference), Path(destination))
            )
            and _write(Path(destination), b"official-spoken")
        ),
    )
    monkeypatch.setattr(music_reply, "render_reply_video", fake_normal)
    monkeypatch.setattr(music_reply.MiniMaxMusic3Worker, "generate", fake_generate)
    monkeypatch.setattr(music_reply, "separate_vocals", fake_separate)
    monkeypatch.setattr(music_reply, "render_full_face_performance", fake_face)
    monkeypatch.setattr(
        music_reply, "_official_transition_reference", lambda _environment: transition
    )
    monkeypatch.setattr(music_reply, "concat_videos", fake_concat)

    result = music_reply.render_musical_reply(
        "synthetic letter",
        "synthetic canonical reply",
        output,
        normal_video_path=normal,
        official_reply_reference_path=official_reference,
        song_video_path=song_video,
        tts_config_path=tmp_path / "tts.json",
        visual_config_path=tmp_path / "visual.json",
        worker_path=tmp_path / "visual-worker.py",
        performance_video_path=tmp_path / "base-performance.mp4",
        duration_seconds=40,
    )

    assert order == [
        "plan",
        "spoken",
        "minimax",
        "separate",
        "performance",
        "concat",
    ]
    assert observed["minimax"][3]["lyrics"] == semantic.lyrics
    assert observed["minimax"][3]["caption"] == caption
    assert observed["spoken"][2]["adaptive_delivery"] is True
    assert observed["spoken"][2]["scene_path"] == official_spoken
    assert observed["spoken_reference"] == (official_reference, official_spoken)
    assert observed["concat"] == (
        normal,
        song_video,
        output,
        {
            "transition_video_path": transition,
            "ffmpeg_path": tmp_path / "ffmpeg.exe",
        },
    )
    assert output.read_bytes() == b"final"
    assert result["reply_structure"] == (
        "normal_video_then_official_transition_then_song_video"
    )
    assert result["transition_duration_seconds"] == 8.0
    assert result["lyrics_sha256"] == hashlib.sha256(
        semantic.lyrics.encode("utf-8")
    ).hexdigest()
    assert result["caption_sha256"] == hashlib.sha256(
        caption.encode("utf-8")
    ).hexdigest()
    assert result["spoken_stage"] == "completed"
    assert result["music_stage"] == "completed"
    assert result["performance_stage"] == "completed"


def test_render_musical_reply_fails_closed_without_official_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLIVIA_MINIMAX_COMFY_PYTHON", "synthetic-python")
    monkeypatch.setenv("OLIVIA_MINIMAX_COMFY_ROOT", "synthetic-root")
    monkeypatch.setenv("OLIVIA_MINIMAX_WORKER", "synthetic-worker")
    monkeypatch.setattr(
        music_reply,
        "plan_song_content",
        lambda *_args: SongContentPlan(
            emotion="gentle_reassurance",
            lyrics="[Verse]\nStay here",
            caption="gentle piano ballad",
            duration_seconds=40,
        ),
    )
    monkeypatch.delenv("OLIVIA_OFFICIAL_REPLY_REFERENCE", raising=False)
    official_reference = _write(tmp_path / "official.mp4")

    with pytest.raises(music_reply.MusicReplyError, match="MUSIC_REPLY_TRANSITION_UNAVAILABLE"):
        music_reply.render_musical_reply(
            "letter",
            "reply",
            tmp_path / "final.mp4",
            normal_video_path=tmp_path / "spoken.mp4",
            official_reply_reference_path=official_reference,
            song_video_path=tmp_path / "performance.mp4",
            tts_config_path=tmp_path / "tts.json",
            visual_config_path=tmp_path / "visual.json",
            worker_path=tmp_path / "worker.py",
            performance_video_path=tmp_path / "base-performance.mp4",
            duration_seconds=40,
        )


def test_render_musical_reply_resumes_from_persisted_spoken_and_song_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "letter.mp4"
    normal = tmp_path / "letter-spoken.mp4"
    song_video = tmp_path / "letter-song.mp4"
    old_stage_root = tmp_path / "letter-stages"
    stage_root = tmp_path / "letter-music-v2-40s-stages"
    _write(old_stage_root / "song.flac", b"stale-118-second-song")
    minimax_worker = _write(tmp_path / "minimax-worker.py", b"provider-v1")
    calls = {"spoken": 0, "minimax": 0, "separate": 0}

    monkeypatch.setenv("OLIVIA_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("OLIVIA_FFMPEG_EXE", str(_write(tmp_path / "ffmpeg.exe")))
    monkeypatch.setenv("OLIVIA_PROVIDER_CACHE_ROOT", str(tmp_path / "provider-cache"))
    monkeypatch.setenv("OLIVIA_MINIMAX_COMFY_PYTHON", "synthetic-python")
    monkeypatch.setenv("OLIVIA_MINIMAX_COMFY_ROOT", "synthetic-root")
    monkeypatch.setenv(
        "OLIVIA_MINIMAX_WORKER", minimax_worker.relative_to(tmp_path).as_posix()
    )
    monkeypatch.setenv("OLIVIA_LATENTSYNC_PYTHON", "synthetic-python")
    monkeypatch.setenv("OLIVIA_LATENTSYNC_ROOT", "synthetic-root")
    monkeypatch.setattr(
        music_reply,
        "plan_song_content",
        lambda *_args: SongContentPlan(
            emotion="gentle_reassurance",
            lyrics="[Verse]\nStay here",
            caption="gentle piano ballad",
            duration_seconds=40,
        ),
    )

    def fake_normal(_text, destination, **_kwargs):
        calls["spoken"] += 1
        _write(Path(destination), b"spoken")
        return {"spoken_stage": "completed"}

    def fake_generate(self, _content, _reply_text, destination, **_kwargs):
        del self
        calls["minimax"] += 1
        _write(Path(destination), b"expensive-song")
        return {"music_stage": "completed"}

    def flaky_separate(_source, destination, **_kwargs):
        calls["separate"] += 1
        if calls["separate"] == 1:
            raise music_reply.MusicReplyError("ROFORMER_FAILED")
        _write(Path(destination), b"vocals")

    monkeypatch.setattr(music_reply, "render_reply_video", fake_normal)
    monkeypatch.setattr(music_reply.MiniMaxMusic3Worker, "generate", fake_generate)
    monkeypatch.setattr(music_reply, "separate_vocals", flaky_separate)
    monkeypatch.setattr(
        music_reply,
        "render_full_face_performance",
            lambda _base, _vocals, _song, destination, **_kwargs: (
            _write(Path(destination), b"performance")
            and {"performance_stage": "completed"}
        ),
    )
    transition = _write(tmp_path / "official-transition.mp4")
    monkeypatch.setattr(
        music_reply, "_official_transition_reference", lambda _environment: transition
    )
    monkeypatch.setattr(
        music_reply,
        "concat_videos",
        lambda _normal, _song, destination, **_kwargs: _write(
            Path(destination), b"final"
        ),
    )

    official_reference = _write(tmp_path / "official-complete-reply.mp4")
    monkeypatch.setattr(
        music_reply,
        "prepare_official_spoken_base",
        lambda _reference, destination, **_kwargs: _write(Path(destination), b"official-spoken"),
    )
    kwargs = {
        "normal_video_path": normal,
        "official_reply_reference_path": official_reference,
        "song_video_path": song_video,
        "tts_config_path": tmp_path / "tts.json",
        "visual_config_path": tmp_path / "visual.json",
        "worker_path": tmp_path / "visual-worker.py",
        "performance_video_path": tmp_path / "base-performance.mp4",
        "duration_seconds": 40,
    }
    with pytest.raises(music_reply.MusicReplyError, match="ROFORMER_FAILED"):
        music_reply.render_musical_reply("letter", "reply", output, **kwargs)

    assert normal.read_bytes() == b"spoken"
    assert (stage_root / "song.flac").read_bytes() == b"expensive-song"
    assert (old_stage_root / "song.flac").read_bytes() == b"stale-118-second-song"

    result = music_reply.render_musical_reply("letter", "reply", output, **kwargs)

    assert output.read_bytes() == b"final"
    assert calls == {"spoken": 1, "minimax": 1, "separate": 2}
    assert result["spoken_stage"] == "reused"
    assert result["music_stage"] == "reused"
    assert (stage_root / "manifest.json").is_file()

    music_reply.render_musical_reply("letter", "changed reply", output, **kwargs)

    assert calls == {"spoken": 2, "minimax": 2, "separate": 3}

    minimax_worker.write_bytes(b"provider-v2")
    music_reply.render_musical_reply("letter", "changed reply", output, **kwargs)

    assert calls == {"spoken": 3, "minimax": 3, "separate": 4}


def test_render_musical_reply_rebuilds_downstream_stages_when_song_audio_is_rebuilt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "letter.mp4"
    normal = tmp_path / "letter-spoken.mp4"
    song_video = tmp_path / "letter-song.mp4"
    stage_root = tmp_path / "letter-music-v2-40s-stages"
    minimax_worker = _write(tmp_path / "minimax-worker.py", b"provider-v1")
    calls = {"minimax": 0, "separate": 0, "performance": 0}

    monkeypatch.setenv("OLIVIA_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("OLIVIA_FFMPEG_EXE", str(_write(tmp_path / "ffmpeg.exe")))
    monkeypatch.setenv("OLIVIA_PROVIDER_CACHE_ROOT", str(tmp_path / "provider-cache"))
    monkeypatch.setenv("OLIVIA_MINIMAX_COMFY_PYTHON", "synthetic-python")
    monkeypatch.setenv("OLIVIA_MINIMAX_COMFY_ROOT", "synthetic-root")
    monkeypatch.setenv(
        "OLIVIA_MINIMAX_WORKER", minimax_worker.relative_to(tmp_path).as_posix()
    )
    monkeypatch.setenv("OLIVIA_LATENTSYNC_PYTHON", "synthetic-python")
    monkeypatch.setenv("OLIVIA_LATENTSYNC_ROOT", "synthetic-root")
    monkeypatch.setattr(
        music_reply,
        "plan_song_content",
        lambda *_args: SongContentPlan(
            emotion="gentle_reassurance",
            lyrics="[Verse]\\nStay here",
            caption="gentle piano ballad",
            duration_seconds=40,
        ),
    )
    monkeypatch.setattr(
        music_reply,
        "prepare_official_spoken_base",
        lambda _reference, destination, **_kwargs: _write(Path(destination), b"official-spoken"),
    )
    monkeypatch.setattr(
        music_reply,
        "render_reply_video",
        lambda _text, destination, **_kwargs: (
            _write(Path(destination), b"spoken") and {"spoken_stage": "completed"}
        ),
    )

    def fake_generate(self, _content, _reply_text, destination, **_kwargs):
        del self
        calls["minimax"] += 1
        _write(Path(destination), f"song-{calls['minimax']}".encode())
        return {"music_stage": "completed"}

    def fake_separate(_source, destination, **_kwargs):
        calls["separate"] += 1
        _write(Path(destination), f"vocals-{calls['separate']}".encode())

    def fake_face(_base, _vocals, _song, destination, **_kwargs):
        calls["performance"] += 1
        _write(Path(destination), f"performance-{calls['performance']}".encode())
        return {"performance_stage": "completed"}

    monkeypatch.setattr(music_reply.MiniMaxMusic3Worker, "generate", fake_generate)
    monkeypatch.setattr(music_reply, "separate_vocals", fake_separate)
    monkeypatch.setattr(music_reply, "render_full_face_performance", fake_face)
    transition = _write(tmp_path / "official-transition.mp4")
    monkeypatch.setattr(
        music_reply, "_official_transition_reference", lambda _environment: transition
    )
    monkeypatch.setattr(
        music_reply,
        "concat_videos",
        lambda _normal, _song, destination, **_kwargs: _write(
            Path(destination), b"final"
        ),
    )
    kwargs = {
        "normal_video_path": normal,
        "official_reply_reference_path": _write(tmp_path / "official.mp4"),
        "song_video_path": song_video,
        "tts_config_path": tmp_path / "tts.json",
        "visual_config_path": tmp_path / "visual.json",
        "worker_path": tmp_path / "visual-worker.py",
        "performance_video_path": tmp_path / "base-performance.mp4",
        "duration_seconds": 40,
    }

    music_reply.render_musical_reply("letter", "reply", output, **kwargs)
    (stage_root / "song.flac").unlink()
    music_reply.render_musical_reply("letter", "reply", output, **kwargs)

    assert calls == {"minimax": 2, "separate": 2, "performance": 2}


def test_render_musical_reply_invalidates_cache_when_configured_provider_assets_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "letter.mp4"
    normal = tmp_path / "letter-spoken.mp4"
    song_video = tmp_path / "letter-song.mp4"
    minimax_python = _write(tmp_path / "minimax-python.exe", b"python-v1")
    minimax_worker = _write(tmp_path / "minimax-worker.py", b"worker-v1")
    minimax_root = tmp_path / "minimax-root"
    minimax_entry = _write(minimax_root / "main.py", b"entry-v1")
    minimax_node = _write(
        minimax_root / "comfy_extras" / "nodes_minimax_music.py", b"node-v1"
    )
    minimax_models = [
        _write(
            minimax_root / "models" / "diffusion_models" / "minimax_music3_dit_int8_convrot.safetensors",
            b"unet-v1",
        ),
        _write(
            minimax_root / "models" / "text_encoders" / "minimax_music3_text_encoder_pruned_int8_convrot.safetensors",
            b"text-v1",
        ),
        _write(
            minimax_root / "models" / "vae" / "minimax_music3_dav.safetensors",
            b"vae-v1",
        ),
    ]
    latentsync_root = tmp_path / "latentsync-root"
    latentsync_checkpoint = _write(
        latentsync_root / "checkpoints" / "latentsync_unet.pt", b"checkpoint-v1"
    )
    calls = {"minimax": 0, "separate": 0, "performance": 0}

    monkeypatch.setenv("OLIVIA_MINIMAX_COMFY_PYTHON", str(minimax_python))
    monkeypatch.setenv("OLIVIA_FFMPEG_EXE", str(_write(tmp_path / "ffmpeg.exe")))
    monkeypatch.setenv("OLIVIA_PROVIDER_CACHE_ROOT", str(tmp_path / "provider-cache"))
    monkeypatch.setenv("OLIVIA_MINIMAX_COMFY_ROOT", str(minimax_root))
    monkeypatch.setenv("OLIVIA_MINIMAX_WORKER", str(minimax_worker))
    monkeypatch.setenv("OLIVIA_LATENTSYNC_PYTHON", str(tmp_path / "latentsync-python.exe"))
    monkeypatch.setenv("OLIVIA_LATENTSYNC_ROOT", str(latentsync_root))
    monkeypatch.setattr(
        music_reply,
        "plan_song_content",
        lambda *_args: SongContentPlan(
            emotion="gentle_reassurance",
            lyrics="[Verse]\\nStay here",
            caption="gentle piano ballad",
            duration_seconds=40,
        ),
    )
    monkeypatch.setattr(
        music_reply,
        "prepare_official_spoken_base",
        lambda _reference, destination, **_kwargs: _write(Path(destination), b"official-spoken"),
    )
    monkeypatch.setattr(
        music_reply,
        "render_reply_video",
        lambda _text, destination, **_kwargs: (
            _write(Path(destination), b"spoken") and {"spoken_stage": "completed"}
        ),
    )

    def fake_generate(self, _content, _reply_text, destination, **_kwargs):
        del self
        calls["minimax"] += 1
        _write(Path(destination), f"song-{calls['minimax']}".encode())
        return {"music_stage": "completed"}

    def fake_separate(_source, destination, **_kwargs):
        calls["separate"] += 1
        _write(Path(destination), f"vocals-{calls['separate']}".encode())

    def fake_face(_base, _vocals, _song, destination, **_kwargs):
        calls["performance"] += 1
        _write(Path(destination), f"performance-{calls['performance']}".encode())
        return {"performance_stage": "completed"}

    monkeypatch.setattr(music_reply.MiniMaxMusic3Worker, "generate", fake_generate)
    monkeypatch.setattr(music_reply, "separate_vocals", fake_separate)
    monkeypatch.setattr(music_reply, "render_full_face_performance", fake_face)
    transition = _write(tmp_path / "official-transition.mp4")
    monkeypatch.setattr(
        music_reply, "_official_transition_reference", lambda _environment: transition
    )
    monkeypatch.setattr(
        music_reply,
        "concat_videos",
        lambda _normal, _song, destination, **_kwargs: _write(
            Path(destination), b"final"
        ),
    )
    kwargs = {
        "normal_video_path": normal,
        "official_reply_reference_path": _write(tmp_path / "official.mp4"),
        "song_video_path": song_video,
        "tts_config_path": tmp_path / "tts.json",
        "visual_config_path": tmp_path / "visual.json",
        "worker_path": tmp_path / "visual-worker.py",
        "performance_video_path": tmp_path / "base-performance.mp4",
        "duration_seconds": 40,
    }

    music_reply.render_musical_reply("letter", "reply", output, **kwargs)

    for index, asset in enumerate(
        [minimax_python, minimax_entry, minimax_node, *minimax_models, latentsync_checkpoint],
        start=2,
    ):
        asset.write_bytes(f"provider-v{index}".encode())
        music_reply.render_musical_reply("letter", "reply", output, **kwargs)
        assert calls == {"minimax": index, "separate": index, "performance": index}

    minimax_node.unlink()
    music_reply.render_musical_reply("letter", "reply", output, **kwargs)

    assert calls == {"minimax": 9, "separate": 9, "performance": 9}
