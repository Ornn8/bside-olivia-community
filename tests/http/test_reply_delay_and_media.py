from __future__ import annotations

import asyncio

import local_server


def test_text_delay_records_deadline_without_blocking(monkeypatch):
    monkeypatch.setenv("OLIVIA_REPLY_DELAY_ENABLED", "1")
    monkeypatch.setenv("OLIVIA_REPLY_DELAY_MINUTES_MIN", "5")
    monkeypatch.setenv("OLIVIA_REPLY_DELAY_MINUTES_MAX", "10")
    monkeypatch.setattr(local_server.random, "uniform", lambda _minimum, _maximum: 5.0)
    letter = {}
    local_server._schedule_text_reply_delay(letter, "text")
    assert letter["reply_delay_minutes"] == 5.0
    assert letter["reply_not_before"] > 0


def test_video_delay_is_zero_and_media_generation_runs_off_loop(monkeypatch):
    monkeypatch.setenv("OLIVIA_REPLY_DELAY_ENABLED", "1")
    letter = {}
    local_server._schedule_text_reply_delay(letter, "video")
    assert letter["reply_delay_minutes"] == 0.0
    assert letter["reply_not_before"] == 0.0
    assert asyncio.iscoroutinefunction(local_server._render_media_job)
