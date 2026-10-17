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


def test_old_draft_is_not_sent_or_merged_into_new_pending_batch():
    async def scenario():
        old = PersonalMessage('qq', '100', '200', 'old', 'old synthetic input')
        new = PersonalMessage('qq', '100', '200', 'new', 'current synthetic input')
        generated, sent = [], []
        async def generate(event, row):
            generated.append(event.text)
            return 'current reply'
        async def commit(row):
            pass
        async def send(text):
            sent.append(text)
            return 'receipt'
        service = PersonalChatService([], lambda: None, generate, commit, {'qq': ('100', '200')})
        await service.ingest(old)
        service.rows[0].update(created_at=1, delivery_status='GENERATED', reply_text='obsolete draft')
        await service.ingest(new)
        await service.handle(old, send)
        assert service.rows[0]['delivery_status'] == 'SKIPPED'
        assert not generated and not sent
        await service.handle(new, send)
        assert generated == ['current synthetic input']
        assert sent == ['current reply']
        await asyncio.gather(*service.consumer_tasks.values())
    asyncio.run(scenario())


def test_slow_generation_does_not_merge_a_later_conversation():
    async def scenario():
        old = PersonalMessage('qq', '100', '200', 'old', 'old synthetic input')
        new = PersonalMessage('qq', '100', '200', 'new', 'current synthetic input')
        generated, sent = [], []
        async def generate(event, row):
            generated.append(event.text)
            if event.message_id == 'old':
                await service.ingest(new)
                row['created_at'] -= 600
                return 'obsolete draft'
            return 'current reply'
        async def commit(row):
            pass
        async def send(text):
            sent.append(text)
            return 'receipt'
        service = PersonalChatService([], lambda: None, generate, commit, {'qq': ('100', '200')})
        await service.handle(old, send)
        assert generated == ['old synthetic input', 'current synthetic input']
        assert sent == ['current reply']
        assert service.rows[0]['delivery_status'] == 'SKIPPED'
        await asyncio.gather(*service.consumer_tasks.values())
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
