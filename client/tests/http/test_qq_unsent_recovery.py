import asyncio

import pytest

from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService


def test_generated_reply_resumes_without_regeneration():
    async def scenario():
        event = PersonalMessage('qq', '100', '200', '1', 'hello')
        generated, sent = [], []

        async def generate(event, row):
            generated.append(event.message_id)
            return 'ready reply'

        async def commit(row):
            pass

        async def send(text):
            sent.append(text)
            return 'receipt'

        send.is_available = lambda: False
        service = PersonalChatService([], lambda: None, generate, commit, {'qq': ('100', '200')})
        await service.ingest(event)
        with pytest.raises(RuntimeError, match='PERSONAL_CHAT_CHANNEL_DISCONNECTED'):
            await service.handle(event, send)
        assert service.rows[0]['delivery_status'] == 'GENERATED'
        resumed = PersonalChatService(service.rows, lambda: None, generate, commit, {'qq': ('100', '200')})
        pending = resumed.pending('qq')
        assert len(pending) == 1
        send.is_available = lambda: True
        await resumed.handle(pending[0], send)
        assert generated == ['1']
        assert sent == ['ready reply']
        assert resumed.rows[0]['delivery_status'] == 'DELIVERED'
        assert not resumed.pending('qq')
        await asyncio.gather(*resumed.consumer_tasks.values())

    asyncio.run(scenario())


@pytest.mark.parametrize('state,extra', [
    ('SENDING', {}), ('DELIVERY_UNCONFIRMED', {}), ('FAILED', {}),
    ('GENERATING', {}), ('DELIVERED', {}),
    ('GENERATED', {'send_attempt_id': 'attempt'}),
    ('GENERATED', {'delivery_receipt': {'confirmed': True}}),
])
def test_pending_never_replays_reserved_or_unreviewed_reply(state, extra):
    async def scenario():
        event = PersonalMessage('qq', '100', '200', '1', 'hello')
        service = PersonalChatService([], lambda: None, None, None, {'qq': ('100', '200')})
        await service.ingest(event)
        service.rows[0].update(delivery_status=state, reply_text='ready reply', **extra)
        assert not service.pending('qq')

    asyncio.run(scenario())
