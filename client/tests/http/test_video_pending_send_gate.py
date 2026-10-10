import asyncio

import pytest


@pytest.mark.parametrize("media_status", ["PENDING", "QUEUED", "PROCESSING"])
def test_video_wait_accepts_second_letter_before_media_finishes(monkeypatch, media_status):
    import local_server
    from original_client_letter_contract import serialize_letter_summary

    letter = {"letter_id": "synthetic-video", "letter_status": "COMPLETED",
              "reply_mode": "musical_video", "media_status": media_status,
              "reply_text": "synthetic reply", "reply_not_before": 0.0}
    monkeypatch.setattr(local_server.store, "letters", [letter])
    assert serialize_letter_summary(letter)["videoPending"] is True
    monkeypatch.setattr(local_server, '_persist_store_state', lambda: None)
    monkeypatch.setattr(local_server, '_schedule_reply_job', lambda *args, **kwargs: None)
    monkeypatch.setattr(local_server, '_conversation_memory_ready_for_reply', lambda: False)
    monkeypatch.setattr(local_server, '_proactive_busy', False)
    result = asyncio.run(local_server.route("POST", "/toy/letter/send", {"content": "synthetic next letter"}, {}))
    assert result['code'] == 0
    assert len(local_server.store.letters) == 2
    local_server.store.letters[:] = [letter]
    for terminal in ("COMPLETED", "FAILED", "CANCELLED"):
        letter["media_status"] = terminal
        assert local_server._active_undelivered_letter() is None
        assert serialize_letter_summary(letter)["letterStatus"] == 4
    # A failed original letter with stale video metadata must remain retryable.
    letter.update(letter_status="FAILED", media_status="PROCESSING")
    assert local_server._active_undelivered_letter() is None
