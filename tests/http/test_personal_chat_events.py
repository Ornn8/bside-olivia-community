from runtime.personal_chat.events import owner_message
import pytest


def qq(**changes):
    return {"post_type": "message", "message_type": "private", "self_id": 100,
            "user_id": 200, "message_id": 1, "message": [{"type": "text", "data": {"text": "晚饭\n吃什么？"}}], **changes}


def wechat(**changes):
    return {"message_type": 1, "message_state": 2, "from_user_id": "owner", "to_user_id": "bot",
            "message_id": 1, "item_list": [{"type": 1, "text_item": {"text": "晚饭\n吃什么？"}}], **changes}


@pytest.mark.parametrize("changes", [{"user_id": 300}, {"message_type": "group"},
    {"post_type": "message_sent"}, {"self_id": 101}, {"message_id": None},
    {"message": [{"type": "image", "data": {"file": "private.png"}}]}])
def test_qq_ignores_everything_except_owner_text(changes):
    assert owner_message("qq", qq(**changes), account_id="100", owner_id="200") is None


@pytest.mark.parametrize("changes", [{"from_user_id": "stranger"}, {"to_user_id": "other"},
    {"group_id": "group"}, {"message_type": 2}, {"message_state": 1}])
def test_wechat_ignores_foreign_group_and_partial_messages(changes):
    assert owner_message("wechat", wechat(**changes), account_id="bot", owner_id="owner") is None


def test_exact_content_and_stable_platform_scoped_id():
    a = owner_message("qq", qq(), account_id="100", owner_id="200")
    b = owner_message("wechat", wechat(), account_id="bot", owner_id="owner")
    assert a.text == b.text == "晚饭\n吃什么？"
    assert a.exchange_id != b.exchange_id
    assert a.exchange_id == owner_message("qq", qq(), account_id="100", owner_id="200").exchange_id
