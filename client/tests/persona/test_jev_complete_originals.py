"""A bounded writer excerpt is not the only copy of a received original."""
import asyncio
import json

import pytest

from runtime.personal_chat.context import READ_WINDOW, freeze_read_window
from runtime.reply.companion_runtime import prepare_decision
from runtime.reply.conversation_context import conversation_context
from runtime.reply.fact_attribution import prepare_dialogue_messages
from tests.persona.test_jev_pipeline import Port
from tests.persona.test_jev_reply_recovery import NOW


@pytest.mark.parametrize('missing', [None, 'missing', 'revision', 'undelivered'])
def test_clipped_recent_turn_restores_only_its_matching_frozen_original(missing):
    text = '开头原话。' + '完整中间原话。' * 1000 + '结尾更正。'
    rows = [dict(letter_id='synthetic', channel='qq', binding_id='owner', created_at=1,
                 delivery_status='DELIVERED', content='说说近况', reply_text=text, reply_revision=1)]
    window = freeze_read_window(rows, channel='qq', binding_id='owner', current_id='new')
    token = READ_WINDOW.set(window)
    try:
        recent, _ = conversation_context(rows, query='后来呢', now=NOW, max_chars=6000)
        assert json.loads(recent)['letters'][0]['truncated'] is True
        wrapper = json.dumps(dict(fragment_id='chat.recent', text=recent), ensure_ascii=False)
        messages = prepare_dialogue_messages([
            dict(role='system', content='<untrusted_history>' + wrapper + '</untrusted_history>'),
            dict(role='user', content='后来呢')], max_input_chars=16000)
        if missing == 'missing':
            READ_WINDOW.set(())
        elif missing == 'revision':
            READ_WINDOW.set(({**window[0], 'reply_revision': 2},))
        elif missing == 'undelivered':
            READ_WINDOW.set(({**window[0], '_received_only': True},))
        port = Port()
        async def decide():
            return await prepare_decision(port, messages, '后来呢', source_id='reply:new:user',
                input_revision=0, as_of=NOW.isoformat(), kinds=['text'])
        if missing:
            with pytest.raises(RuntimeError, match='^JEV_CONTEXT_UNAVAILABLE$'):
                asyncio.run(decide())
            assert not port.turns
        else:
            result = asyncio.run(decide())
            assert result is not None and len(port.turns) == 1
            actual = port.turns[0].input['messages']
            assert [row['text'] for row in actual] == ['说说近况', text, '后来呢']
            assert '[中间原文省略]' not in str(actual)
        assert window[0]['reply_text'] == text
    finally:
        READ_WINDOW.reset(token)
