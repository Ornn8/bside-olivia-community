"""A letter that only waits for its generated voice or song must not hold up writing.

Video replies already behave this way (their text is delivered first). Audio replies keep
their delivery rule: the text and the voice or song arrive together, so the letter stays
unpublished until the audio exists. What changes is only that the next letter may be sent.
"""

import asyncio
from pathlib import Path
import time

import pytest

import local_server
from original_client_letter_contract import (
    serialize_letter_detail,
    serialize_letter_list,
    serialize_letter_summary,
)

AUDIO_MODES = ("voice_reply", "singing_video", "voice_song_video")
PENDING_STATES = ("PENDING", "QUEUED", "PROCESSING")


def audio_letter(**changes):
    row = dict(letter_id="song-wait", content="synthetic cover request", reply_text="written reply",
               letter_status="COMPLETED", reply_mode="singing_video", reply_video_enabled=False,
               media_status="PROCESSING", reply_not_before=0.0, created_at=time.time(), material={},
               is_read=1)
    row.update(changes)
    return row


@pytest.mark.parametrize("mode", AUDIO_MODES)
@pytest.mark.parametrize("state", PENDING_STATES)
def test_audio_wait_keeps_its_delivery_but_is_marked_media_only(monkeypatch, mode, state):
    row = audio_letter(reply_mode=mode, media_status=state)
    for serialize in (serialize_letter_summary, serialize_letter_detail):
        result = serialize(row)
        # Delivery is unchanged: still unpublished, no text, the same in-progress status.
        assert result["letterStatus"] == 3
        assert result["replyType"] == 0
        assert result["audioStatus"] == state
        assert result["mediaWaitOnly"] is True
        if serialize is serialize_letter_detail:
            assert result["replyText"] == ""
    # The server never counted this wait as an undelivered letter.
    monkeypatch.setattr(local_server.store, "letters", [row])
    assert local_server._active_undelivered_letter() is None


@pytest.mark.parametrize("mode", AUDIO_MODES)
@pytest.mark.parametrize("status", ["PENDING", "PROCESSING"])
def test_a_reply_still_being_written_is_never_media_only(monkeypatch, mode, status):
    row = audio_letter(reply_mode=mode, letter_status=status, reply_text="")
    result = serialize_letter_summary(row)
    assert result["letterStatus"] == 1
    assert "mediaWaitOnly" not in result
    monkeypatch.setattr(local_server.store, "letters", [row])
    assert local_server._active_undelivered_letter() is row


def test_media_only_flag_is_absent_everywhere_else(monkeypatch):
    photo = audio_letter(image_reply_settings={"enabled": True}, companion_delivery="image",
                         image_status="GENERATING", reply_mode="voice_reply")
    cases = {
        "text letter": audio_letter(reply_mode="text_letter", reply_video_enabled=False, media_status="NOT_REQUESTED"),
        "video wait": audio_letter(reply_mode="musical_video", reply_video_enabled=True),
        "delivered": audio_letter(media_status="COMPLETED"),
        "failed media": audio_letter(media_status="UNAVAILABLE"),
        "photo attachment": photo,
        "failed letter": audio_letter(letter_status="FAILED"),
    }
    for name, row in cases.items():
        assert "mediaWaitOnly" not in serialize_letter_summary(row), name
    # Failed or finished audio publishes the text, as before.
    assert serialize_letter_summary(cases["delivered"])["letterStatus"] == 4
    assert serialize_letter_summary(cases["failed media"])["letterStatus"] == 4
    # The photo wait stays single-flight on the server, so it must keep holding the card.
    monkeypatch.setattr(local_server.store, "letters", [photo])
    assert local_server._active_undelivered_letter() is photo


def _stub_send(monkeypatch, letters):
    monkeypatch.setattr(local_server.store, "letters", letters)
    monkeypatch.setattr(local_server, "_persist_store_state", lambda: None)
    scheduled = []
    monkeypatch.setattr(local_server, "_schedule_reply_job", lambda *args, **kwargs: scheduled.append(args))
    monkeypatch.setattr(local_server, "_conversation_memory_ready_for_reply", lambda: False)
    monkeypatch.setattr(local_server, "_proactive_busy", False)
    return scheduled


@pytest.mark.parametrize("mode", AUDIO_MODES)
@pytest.mark.parametrize("state", PENDING_STATES)
def test_a_second_letter_is_accepted_while_the_audio_is_still_being_made(monkeypatch, mode, state):
    first = audio_letter(reply_mode=mode, media_status=state)
    frozen = dict(first)
    scheduled = _stub_send(monkeypatch, [first])

    async def scenario():
        sent = await local_server.route("POST", "/toy/letter/send", {"content": "a different letter"}, {})
        assert sent["code"] == 0
        listed = await local_server.route("GET", "/toy/letter/list", {}, {})
        return sent, listed

    sent, listed = asyncio.run(scenario())
    assert len(local_server.store.letters) == 2
    new = local_server.store.letters[0]
    assert new["letter_id"] != first["letter_id"] and new["content"] == "a different letter"
    assert [args[0] for args in scheduled] == [new["letter_id"]]
    # The waiting letter is untouched: same status, same text, same media state.
    assert first == frozen
    # The remaining-letter count does not depend on pending letters.
    assert listed["data"]["remaining_today"] == 99


def test_a_letter_whose_text_is_still_being_written_still_blocks_sending(monkeypatch):
    _stub_send(monkeypatch, [audio_letter(letter_status="PROCESSING", reply_text="")])
    result = asyncio.run(local_server.route("POST", "/toy/letter/send", {"content": "another"}, {}))
    assert result["code"] == 409 and result["message"] == "LETTER_IN_PROGRESS"
    assert len(local_server.store.letters) == 1


def test_the_same_request_sent_again_meanwhile_does_not_start_a_second_recording(monkeypatch):
    first = audio_letter(created_at=time.time() - 5)
    scheduled = _stub_send(monkeypatch, [first])
    again = asyncio.run(local_server.route(
        "POST", "/toy/letter/send", {"content": first["content"], "material": first["material"]}, {}))
    assert again["code"] == 0
    assert again["data"]["letter_id"] == first["letter_id"]
    assert len(local_server.store.letters) == 1 and scheduled == []
    # A later, deliberate resend of the same words is a new letter again.
    first["created_at"] = time.time() - local_server.LETTER_RETRY_DEDUP_SECONDS - 5
    later = asyncio.run(local_server.route("POST", "/toy/letter/send", {"content": first["content"]}, {}))
    assert later["code"] == 0 and later["data"]["letter_id"] != first["letter_id"]
    assert len(local_server.store.letters) == 2
    # And once the voice is delivered, an identical letter is accepted at any time.
    local_server.store.letters[:] = [dict(first, media_status="COMPLETED", created_at=time.time() - 1)]
    done = asyncio.run(local_server.route("POST", "/toy/letter/send", {"content": first["content"]}, {}))
    assert done["data"]["letter_id"] != first["letter_id"]


def _audio_environment(monkeypatch, tmp_path, rendered):
    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(local_server, "_persist_media_state", lambda: None)
    monkeypatch.setattr(local_server, "_record_published_media", lambda *args, **kwargs: None)
    monkeypatch.setattr("runtime.remote_pipeline.enabled", lambda env: True)

    async def plan(*args):
        return None

    async def off_loop(fn, *args, **kwargs):
        assert fn is local_server.render_reply_audio
        name = Path(args[1]).name
        rendered.append(("start", name))
        await gates[name].wait()
        Path(args[1]).write_bytes(b"audio")
        rendered.append(("end", name))
        return {"duration_seconds": 4}

    gates = {}
    monkeypatch.setattr(local_server, "_voice_plan_for_letter", plan)
    monkeypatch.setattr(local_server.asyncio, "to_thread", off_loop)
    return gates


def test_two_waiting_letters_are_made_in_order_and_each_audio_lands_on_its_own_letter(monkeypatch, tmp_path):
    first = audio_letter(letter_id="first-song", reply_mode="voice_reply", content="first", reply_text="first reply",
                         media_status="PENDING", created_at=time.time() - 30)
    second = audio_letter(letter_id="second-song", reply_mode="voice_reply", content="second", reply_text="second reply",
                          media_status="PENDING", created_at=time.time() - 5)
    # A text letter sent in between is answered at once, ahead of both voices.
    text = audio_letter(letter_id="plain-text", reply_mode="text_letter", content="third", reply_text="third reply",
                        media_status="NOT_REQUESTED", created_at=time.time())
    monkeypatch.setattr(local_server.store, "letters", [text, second, first])
    rendered = []
    gates = _audio_environment(monkeypatch, tmp_path, rendered)
    gates.update({"first-song.wav": asyncio.Event(), "second-song.wav": asyncio.Event()})

    async def scenario():
        monkeypatch.setattr(local_server, "media_semaphore", asyncio.Semaphore(1))
        monkeypatch.setattr(local_server, "video_media_semaphore", asyncio.Semaphore(1), raising=False)
        jobs = [asyncio.create_task(local_server._render_media_job(
            row["letter_id"], row["content"], row["reply_text"], row["reply_mode"])) for row in (first, second)]
        await asyncio.sleep(0.05)
        # While the first voice is made, the second waits its turn and the text letter is already readable.
        assert rendered == [("start", "first-song.wav")]
        assert second["media_status"] == "PENDING"
        listed = serialize_letter_list([text, second, first], remaining_today=99, scope="current")["list"]
        assert [(item["letterId"], item["letterStatus"], item.get("mediaWaitOnly", False)) for item in listed] == [
            ("plain-text", 4, False), ("second-song", 3, True), ("first-song", 3, True)]
        gates["first-song.wav"].set()
        await asyncio.sleep(0.05)
        assert first["media_status"] == "COMPLETED" and second["media_status"] == "PROCESSING"
        assert rendered[-1] == ("start", "second-song.wav")
        gates["second-song.wav"].set()
        await asyncio.gather(*jobs)

    asyncio.run(scenario())
    assert rendered == [("start", "first-song.wav"), ("end", "first-song.wav"),
                        ("start", "second-song.wav"), ("end", "second-song.wav")]
    for row, name in ((first, "first-song"), (second, "second-song")):
        detail = serialize_letter_detail(row)
        assert detail["letterStatus"] == 4 and "mediaWaitOnly" not in detail
        assert detail["replyText"] == row["reply_text"]
        assert detail["replyAudioUrl"].endswith(f"/{name}.wav")
