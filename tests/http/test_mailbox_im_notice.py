from __future__ import annotations

from pathlib import Path

import pytest


BASE = {
    "origin": "proactive",
    "letter_status": "COMPLETED",
    "is_read": 0,
}


def test_pending_letter_uses_newest_unread_normal_proactive_letter() -> None:
    from runtime.personal_chat.mailbox_notice import pending_letter

    rows = [
        {**BASE, "letter_id": "old", "published_at": 10},
        {**BASE, "letter_id": "invite", "published_at": 40, "proactive_kind": "contact_invitation"},
        {**BASE, "letter_id": "read", "published_at": 50, "is_read": 1},
        {**BASE, "letter_id": "mentioned", "published_at": 60, "im_notice_delivered_at": 61},
        {**BASE, "letter_id": "new", "published_at": 30},
    ]

    assert pending_letter(rows)["letter_id"] == "new"


def test_notice_is_carried_by_user_im_reply_not_standalone_proactive_im() -> None:
    from runtime.personal_chat.mailbox_notice import NOTICE_TEXT, attach_notice

    letters = [{**BASE, "letter_id": "letter-1", "published_at": 10}]
    inbound = {"letter_id": "im-1", "channel": "wechat"}
    proactive = {"letter_id": "im-2", "channel": "wechat", "origin": "proactive"}

    result = attach_notice(letters, inbound, "我知道啦")
    assert result == "我知道啦\n\n" + NOTICE_TEXT
    assert "回复" not in NOTICE_TEXT
    assert inbound["mailbox_notice_letter_id"] == "letter-1"

    assert attach_notice(letters, proactive, "突然想到一件事") == "突然想到一件事"
    assert "mailbox_notice_letter_id" not in proactive


def test_delivered_notice_is_owner_wide_across_qq_and_weixin() -> None:
    from runtime.personal_chat.mailbox_notice import NOTICE_TEXT, attach_notice, commit_notice

    letters = [{**BASE, "letter_id": "letter-1", "published_at": 10}]
    qq = {
        "letter_id": "qq-exchange",
        "channel": "qq",
        "delivery_status": "DELIVERED",
    }
    calls: list[str] = []

    assert attach_notice(letters, qq, "QQ回复").endswith(NOTICE_TEXT)
    assert commit_notice(letters, qq, lambda: calls.append("persist"), now=20) is True
    assert calls == ["persist"]
    assert letters[0]["im_notice_delivered_at"] == 20
    assert letters[0]["im_notice_channel"] == "qq"
    assert letters[0]["im_notice_exchange_id"] == "qq-exchange"

    wechat = {"letter_id": "wx-exchange", "channel": "wechat"}
    assert attach_notice(letters, wechat, "微信回复") == "微信回复"
    assert "mailbox_notice_letter_id" not in wechat


def test_notice_is_not_committed_before_delivery_or_after_letter_read() -> None:
    from runtime.personal_chat.mailbox_notice import commit_notice

    letters = [{**BASE, "letter_id": "letter-1", "published_at": 10}]
    row = {
        "letter_id": "im-1",
        "channel": "wechat",
        "delivery_status": "SENDING",
        "mailbox_notice_letter_id": "letter-1",
    }
    assert commit_notice(letters, row, lambda: None, now=20) is False
    assert "im_notice_delivered_at" not in letters[0]

    row["delivery_status"] = "DELIVERED"
    letters[0]["is_read"] = 1
    assert commit_notice(letters, row, lambda: None, now=20) is False
    assert "im_notice_delivered_at" not in letters[0]


def test_notice_marker_rolls_back_if_store_persistence_fails() -> None:
    from runtime.personal_chat.mailbox_notice import commit_notice

    letters = [{**BASE, "letter_id": "letter-1", "published_at": 10}]
    row = {
        "letter_id": "im-1",
        "channel": "wechat",
        "delivery_status": "DELIVERED",
        "mailbox_notice_letter_id": "letter-1",
    }

    def fail() -> None:
        raise OSError("synthetic persistence failure")

    with pytest.raises(OSError):
        commit_notice(letters, row, fail, now=20)
    assert "im_notice_delivered_at" not in letters[0]
    assert "im_notice_channel" not in letters[0]
    assert "im_notice_exchange_id" not in letters[0]


def test_backend_wires_notice_after_generation_and_after_confirmed_delivery() -> None:
    source = (Path(__file__).resolve().parents[2] / "runtime" / "personal_chat" / "backend.py").read_text(
        encoding="utf-8"
    )
    assert "text = attach_notice(getattr(server.store, 'letters', []), row, text)" in source
    assert "for consume in (_commit_mailbox_notice, _commit_world" in source
    assert "commit_notice(getattr(server.store, 'letters', []), row, server._persist_store_state)" in source
