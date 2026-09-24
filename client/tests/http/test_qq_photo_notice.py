import asyncio
from types import SimpleNamespace

import pytest

from runtime.personal_chat.backend import prepare_chat_photo
from runtime.personal_chat.service import PersonalChatService, photo_notice


@pytest.mark.parametrize('fail', [False, True])
def test_notice_attempted_once_even_when_ack_is_unknown(fail):
    async def scenario():
        row, sent, persisted = {}, [], []
        async def send(text):
            sent.append(text)
            if fail:
                raise TimeoutError()
            return 'ack'
        for _ in range(2):
            await photo_notice(row, send, lambda: persisted.append(dict(row)), 'progress', '稍等')
        assert sent == ['稍等']
        assert persisted[0]['image_progress_notice'] == 'SENDING'
        assert row['image_progress_notice'] == ('UNKNOWN' if fail else 'DELIVERED')
    asyncio.run(scenario())


@pytest.mark.parametrize('attach', [False, True])
def test_progress_only_after_photo_plan_is_ready(monkeypatch, attach):
    async def scenario():
        events = []
        async def prepare(server, row, text, reply, *, channel, on_ready):
            assert channel == 'qq'
            events.append('plan')
            if attach:
                await on_ready()
                events.append('generate')
        monkeypatch.setattr('runtime.image_reply.prepare', prepare)
        async def send(text):
            events.append('notice')
            return 'ack'
        await prepare_chat_photo(SimpleNamespace(_persist_store_state=lambda: None),
                                 {'reply_text': '正文'}, send)
        assert events == (['plan', 'notice', 'generate'] if attach else ['plan'])
    asyncio.run(scenario())


def test_photo_send_failure_notifies_without_resending_image():
    async def scenario():
        messages = []
        async def photo(row, send):
            row['image_delivery_status'] = 'UNKNOWN'
            raise TimeoutError()
        async def send(text):
            messages.append(text)
            return 'ack'
        send.image = lambda path: None
        row = dict(letter_id='synthetic', channel='qq', image_status='COMPLETED')
        service = PersonalChatService([row], lambda: None, None, None, {}, photo=photo)
        service._schedule_photo(row, send)
        await asyncio.gather(*service.photo_tasks.values())
        service._schedule_photo(row, send)
        assert not service.photo_tasks
        assert len(messages) == 1 and '没能确认发送成功' in messages[0]
        assert row['image_failure_notice'] == 'DELIVERED'
    asyncio.run(scenario())
