from __future__ import annotations

import asyncio

import local_server


def test_text_delay_records_deadline_without_blocking(monkeypatch):
    monkeypatch.setenv("OLIVIA_REPLY_DELAY_ENABLED", "1")
    monkeypatch.setenv("OLIVIA_REPLY_DELAY_MINUTES_MIN", "5")
    monkeypatch.setenv("OLIVIA_REPLY_DELAY_MINUTES_MAX", "10")
    monkeypatch.setattr(
        local_server.random,
        "uniform",
        lambda _minimum, _maximum: 5.0,
    )
    letter = {}
    local_server._schedule_text_reply_delay(letter, "text_letter")
    assert letter["reply_delay_minutes"] == 5.0
    assert letter["reply_not_before"] > 0


def test_both_video_modes_skip_letter_delay_and_render_off_loop(monkeypatch):
    monkeypatch.setenv("OLIVIA_REPLY_DELAY_ENABLED", "1")
    for mode in ("spoken_video", "musical_video"):
        letter = {}
        local_server._schedule_text_reply_delay(letter, mode)
        assert letter["reply_delay_minutes"] == 0.0
        assert letter["reply_not_before"] == 0.0
    assert asyncio.iscoroutinefunction(local_server._render_media_job)


def test_exact_modes_keep_legacy_wire_compatibility():
    assert local_server._wire_reply_mode("text_letter") == "text"
    assert local_server._wire_reply_mode("spoken_video") == "video"
    assert local_server._wire_reply_mode("musical_video") == "video"
    assert local_server._exact_reply_mode("video") == "musical_video"
