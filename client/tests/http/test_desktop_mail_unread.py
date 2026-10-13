"""Desktop mail notifications follow the existing mailbox publication contract."""
from __future__ import annotations

import asyncio
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from original_client_server import create_original_client_server_runtime


def _letter(**overrides):
    return {
        "letter_id": "synthetic-desktop-mail",
        "content": "Synthetic user letter",
        "letter_status": "COMPLETED",
        "reply_text": "Synthetic delivered reply",
        "reply_mode": "text_letter",
        "media_status": "NOT_REQUESTED",
        "reply_not_before": 0,
        "is_read": 0,
        **overrides,
    }


@pytest.mark.parametrize(("overrides", "expected"), [
    ({}, 1),
    ({"is_read": 1}, 0),
    ({"letter_status": "DRAFT"}, 0),
    ({"letter_status": "PENDING"}, 0),
    ({"letter_status": "AUDITING"}, 0),
    ({"letter_status": "PROCESSING"}, 0),
    ({"letter_status": "FAILED"}, 0),
    ({"letter_status": "CANCELED"}, 0),
    ({"letter_status": "SKIPPED"}, 0),
    ({"reply_not_before": 9_999_999_999}, 0),
    ({"reply_text": "", "reply_mode": "musical_video", "media_status": "COMPLETED",
      "reply_video_url": "http://127.0.0.1:8899/toy/media/synthetic.mp4"}, 1),
    ({"letter_status": 4}, 1),
    ({"origin": "proactive"}, 1),
    ({"origin": "proactive", "letter_status": "PROCESSING"}, 0),
    ({"reply_mode": "voice_reply", "media_status": "PROCESSING"}, 0),
    ({"reply_mode": "voice_reply", "media_status": "COMPLETED"}, 1),
    ({"reply_mode": "voice_reply", "media_status": "FAILED"}, 1),
    ({"reply_mode": "musical_video", "media_status": "PROCESSING"}, 1),
    ({"reply_mode": "musical_video", "media_status": "FAILED"}, 1),
    ({"reply_mode": "voice_song_video", "reply_video_enabled": False,
      "media_status": "PENDING"}, 0),
    ({"image_reply_settings": {"enabled": True}, "companion_delivery": "image",
      "image_status": "PROCESSING"}, 0),
    ({"image_reply_settings": {"enabled": True}, "companion_delivery": "image",
      "image_status": "COMPLETED"}, 1),
    ({"image_reply_settings": {"enabled": True}, "companion_delivery": "image",
      "image_status": "FAILED"}, 1),
    ({"image_reply_settings": {"enabled": True}, "image_status": "PROCESSING"}, 1),
])
def test_desktop_count_only_includes_delivered_unread_replies(monkeypatch, overrides, expected):
    import local_server

    letters = [_letter(**overrides)]
    monkeypatch.setattr(local_server, "_letter_collection", lambda _scope: letters)

    async def scenario():
        runtime = create_original_client_server_runtime(
            local_server.handler, letter_collection=local_server._letter_collection,
        )
        async with TestClient(TestServer(runtime.app, access_log=None)) as client:
            for path in ("/letter/unread_count", "/toy/letter/unread_count"):
                direct = await local_server.route("GET", path, {}, {})
                assert direct["data"]["unread_count"] == expected
                response = await client.get(path)
                assert response.status == 200
                data = (await response.json())["data"]
                assert data["unreadCount"] == data["unread_count"] == expected

    asyncio.run(scenario())


@pytest.mark.parametrize(("read_fields", "expected"), [
    ({"isRead": 0}, True), ({"isRead": 1}, False), ({}, False),
])
def test_unread_reply_preserves_wire_read_marker_and_default(read_fields, expected):
    from original_client_letter_contract import is_unread_reply

    letter = _letter(**read_fields)
    letter.pop("is_read")
    assert is_unread_reply(letter) is expected


@pytest.mark.parametrize("origin", ["user", "proactive"])
def test_desktop_count_changes_on_publication_and_read_only(monkeypatch, tmp_path, origin):
    import local_server

    letter = _letter(origin=origin, reply_not_before=time.time() + 3600)
    isolated_store = local_server.Store()
    isolated_store.letters.append(letter)
    monkeypatch.setattr(local_server, "store", isolated_store)
    monkeypatch.setattr(local_server, "_state_root", lambda: tmp_path)
    monkeypatch.setattr(local_server, "_refresh_proactive_context", lambda: None)
    monkeypatch.setattr(local_server, "_letter_collection", lambda _scope: [letter])

    async def scenario():
        runtime = create_original_client_server_runtime(
            local_server.handler, letter_collection=local_server._letter_collection,
        )
        async with TestClient(TestServer(runtime.app, access_log=None)) as client:
            async def unread():
                response = await client.get("/letter/unread_count")
                assert response.status == 200
                return (await response.json())["data"]["unreadCount"]

            assert await unread() == 0
            pending = await client.get("/letter/detail", params={"letterId": letter["letter_id"]})
            assert (await pending.json())["data"]["replyText"] == ""
            assert letter["is_read"] == 0
            letter["reply_not_before"] = 0
            assert await unread() == 1
            listing = await client.get("/letter/list")
            row = (await listing.json())["data"]["list"][0]
            assert row["letterStatus"] == 4 and row["isRead"] == 0
            assert await unread() == 1
            detail = await client.get("/letter/detail", params={"letterId": letter["letter_id"]})
            assert (await detail.json())["data"]["replyText"] == letter["reply_text"]
            assert letter["is_read"] == 1
            assert await unread() == 0
            assert await unread() == 0

    asyncio.run(scenario())
