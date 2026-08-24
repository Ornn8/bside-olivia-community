from __future__ import annotations

import pytest

from music_duration import MUSIC_DURATION_OPTIONS, normalize_music_duration
from music_reply import MusicReplyError, render_musical_reply
from reply_delivery import plan_reply_delivery
from reply_media import ReplyMediaError, render_reply_video


def test_delivery_plan_preserves_spoken_text_and_duration_options():
    plan = plan_reply_delivery("我听见了。慢慢来，好吗？")
    assert plan.spoken_text == "我听见了。慢慢来，好吗？"
    assert plan.duration_target_seconds == (40.0, 50.0)
    assert MUSIC_DURATION_OPTIONS == (40, 60)
    assert normalize_music_duration(40) == 40
    assert normalize_music_duration(60) == 60


def test_media_workers_fail_closed_without_external_provider(tmp_path):
    with pytest.raises(ReplyMediaError):
        render_reply_video("测试回信", tmp_path / "reply.mp4", tts_config_path=tmp_path / "missing.json", visual_config_path=tmp_path / "missing.json", worker_path=tmp_path / "missing.py")
    with pytest.raises(MusicReplyError):
        render_musical_reply(
            "测试来信",
            "测试回信",
            tmp_path / "music.mp4",
            normal_video_path=tmp_path / "normal.mp4",
            song_video_path=tmp_path / "song.mp4",
            tts_config_path=tmp_path / "missing.json",
            visual_config_path=tmp_path / "missing.json",
            worker_path=tmp_path / "missing.py",
            performance_video_path=tmp_path / "performance.mp4",
            duration_seconds=40,
        )
