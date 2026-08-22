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
            "[Interlude]",
            "[Verse]",
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
        duration_seconds=90,
    )


def _write(path: Path, data: bytes = b"synthetic") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _value_after(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


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

    monkeypatch.setattr(music_reply, "_ffmpeg", lambda: "ffmpeg")
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
    assert "trim=start=41.000000:end=43.000000" in filters
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
    assert _value_after(final, "-frames:v") == "350"
    assert _value_after(final, "-t") == "14.000000"


def test_concat_videos_without_transition_preserves_two_segment_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normal = _write(tmp_path / "normal.mp4")
    song = _write(tmp_path / "song.mp4")
    output = tmp_path / "final.mp4"
    frames = {normal: 125, song: 250}
    commands: list[list[str]] = []

    monkeypatch.setattr(music_reply, "_ffmpeg", lambda: "ffmpeg")
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
    transition = _write(tmp_path / "official-transition.mp4")
    order: list[str] = []
    observed: dict[str, object] = {}

    monkeypatch.setenv("OLIVIA_MINIMAX_COMFY_PYTHON", "synthetic-python")
    monkeypatch.setenv("OLIVIA_MINIMAX_COMFY_ROOT", "synthetic-root")
    monkeypatch.setenv("OLIVIA_MINIMAX_WORKER", "synthetic-worker")

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

    def fake_separate(source, destination):
        order.append("separate")
        observed["separate"] = (Path(source), Path(destination))
        _write(Path(destination), b"vocals")

    def fake_face(base, vocals, full_song, destination):
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
    monkeypatch.setattr(music_reply, "render_reply_video", fake_normal)
    monkeypatch.setattr(music_reply.MiniMaxMusic3Worker, "generate", fake_generate)
    monkeypatch.setattr(music_reply, "separate_vocals", fake_separate)
    monkeypatch.setattr(music_reply, "render_full_face_performance", fake_face)
    monkeypatch.setattr(music_reply, "_official_transition_reference", lambda: transition)
    monkeypatch.setattr(music_reply, "concat_videos", fake_concat)

    result = music_reply.render_musical_reply(
        "synthetic letter",
        "synthetic canonical reply",
        output,
        normal_video_path=normal,
        normal_scene_path=tmp_path / "scene.mp4",
        song_video_path=song_video,
        tts_config_path=tmp_path / "tts.json",
        visual_config_path=tmp_path / "visual.json",
        worker_path=tmp_path / "visual-worker.py",
        performance_video_path=tmp_path / "base-performance.mp4",
        duration_seconds=90,
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
    assert observed["concat"] == (
        normal,
        song_video,
        output,
        {"transition_video_path": transition},
    )
    assert output.read_bytes() == b"final"
    assert result["reply_structure"] == (
        "normal_video_then_official_transition_then_song_video"
    )
    assert result["transition_duration_seconds"] == 2.0
    assert result["lyrics_sha256"] == hashlib.sha256(
        semantic.lyrics.encode("utf-8")
    ).hexdigest()
    assert result["caption_sha256"] == hashlib.sha256(
        caption.encode("utf-8")
    ).hexdigest()
    assert result["spoken_stage"] == "completed"
    assert result["music_stage"] == "completed"
    assert result["performance_stage"] == "completed"
