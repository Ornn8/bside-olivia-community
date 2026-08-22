from __future__ import annotations

import asyncio
import json

from letter_triage import LetterEmotionTriage


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text


class _Gateway:
    def __init__(self, response: str) -> None:
        self.response = response

    async def complete(self, _messages, *, request_id=None):
        return _Response(self.response)


def test_high_user_emotion_selects_video_mode():
    result = asyncio.run(
        LetterEmotionTriage(_Gateway(json.dumps({"emotion_level": "high", "reason_code": "loss"}))).classify(
            "我一直害怕被替代，已经很久不敢靠近别人。"
        )
    )
    assert result.reply_mode == "video"
    assert result.emotion_level == "high"


def test_invalid_triage_fails_closed_to_text():
    result = asyncio.run(LetterEmotionTriage(_Gateway("not-json")).classify("普通聊天"))
    assert result.reply_mode == "text"
    assert result.status == "unavailable"
