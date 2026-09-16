"""Delivery modes must not change the character's knowledge or repair behavior."""
import json
import re

import pytest

from llm_gateway import GatewayConfig
from local_server import LetterAdapter
from runtime.reply.reply_context import ReplyMode
from runtime.media.song_content import _planning_messages
from runtime.personal_chat.presentation import CURRENT


def participation(messages):
    match = re.search(r'<character_participation>(.*?)</character_participation>',
                      messages[0]['content'], re.S)
    assert match, 'Shared knowledge and correction behavior must reach generation'
    return json.loads(match[1])


@pytest.mark.parametrize('mode', list(ReplyMode))
def test_all_reply_modes_keep_same_participation(mode):
    adapter = LetterAdapter(GatewayConfig(provider='mock'))
    query = '智能优化方法。你刚才怎么突然开始讲课了？'
    baseline = participation(adapter.reply_context_messages(query, mode=ReplyMode.TEXT_LETTER))
    actual = participation(adapter.reply_context_messages(query, mode=mode))
    assert actual == baseline


@pytest.mark.parametrize('channel,proactive', [('qq', False), ('qq', True), ('wechat', False), ('wechat', True)])
def test_social_presentation_and_song_keep_same_participation(channel, proactive):
    adapter = LetterAdapter(GatewayConfig(provider='mock'))
    query = '你刚才说的不像你'
    expected = participation(adapter.reply_context_messages(query, mode=ReplyMode.TEXT_LETTER))
    token = CURRENT.set({'channel': channel, 'proactive': proactive, 'structured': True})
    try:
        assert participation(adapter.reply_context_messages(query, mode=ReplyMode.FUTURE_IM)) == expected
        assert participation(_planning_messages(query, 110, adapter.config, reply_adapter=adapter)) == expected
    finally:
        CURRENT.reset(token)
