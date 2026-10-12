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
def test_photo_preparation_never_sends_canned_progress(monkeypatch, attach):
    async def scenario():
        events = []
        async def prepare(server, row, text, reply, *, channel, on_ready=None):
            assert channel == 'qq'
            events.append('plan')
            if attach:
                assert on_ready is None
                events.append('generate')
        monkeypatch.setattr('runtime.image_reply.prepare', prepare)
        async def send(text):
            events.append('notice')
            return 'ack'
        await prepare_chat_photo(SimpleNamespace(_persist_store_state=lambda: None),
                                 {'reply_text': '正文'}, send)
        assert events == (['plan', 'generate'] if attach else ['plan'])
    asyncio.run(scenario())


@pytest.mark.parametrize('primary', [False, True])
def test_photo_unknown_ack_does_not_announce_failure_or_resend_image(monkeypatch, primary):
    monkeypatch.setattr('runtime.image_reply.is_companion_image', lambda row: primary)
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
        assert messages == []
        assert 'image_failure_notice' not in row
        assert row['image_error_code'] == 'PERSONAL_CHAT_DELIVERY_UNCONFIRMED'
        if primary:
            assert row['delivery_status'] == 'DELIVERY_UNCONFIRMED'
            assert row['letter_status'] == 'PROCESSING'
    asyncio.run(scenario())


@pytest.mark.parametrize('primary', [False, True])
def test_only_a_requested_photo_announces_that_it_failed(monkeypatch, primary):
    monkeypatch.setattr('runtime.image_reply.is_companion_image', lambda row: primary)
    async def scenario():
        messages = []
        async def photo(row, send):
            row['image_status'] = 'FAILED'
        async def send(text):
            messages.append(text)
            return 'ack'
        send.image = lambda path: None
        row = dict(letter_id='synthetic-fail', channel='qq', delivery_status='DELIVERED')
        service = PersonalChatService([row], lambda: None, None, None, {}, photo=photo)
        service._schedule_photo(row, send)
        await asyncio.gather(*service.photo_tasks.values())
        # A casual snapshot that did not come out is simply not sent.
        assert messages == (['照片这次没生成成功，没能发给你。稍后再试一下。'] if primary else [])
    asyncio.run(scenario())


@pytest.mark.parametrize(('error_code', 'expected'), [
    ('GPU_INSUFFICIENT_BALANCE', '照片这次没生成成功，没能发给你。Olivia 可用余额不足，请充值后重试。'),
    ('IMAGE_GENERATION_FAILED', '照片这次没生成成功，没能发给你。稍后再试一下。'),
])
def test_requested_photo_failure_tells_the_user_to_top_up_only_for_a_refused_balance(monkeypatch, error_code, expected):
    monkeypatch.setattr('runtime.image_reply.is_companion_image', lambda row: True)
    async def scenario():
        messages = []
        async def photo(row, send):
            row.update(image_status='FAILED', image_error_code=error_code)
        async def send(text):
            messages.append(text)
            return 'ack'
        send.image = lambda path: None
        row = dict(letter_id='synthetic-balance', channel='qq', delivery_status='DELIVERED')
        service = PersonalChatService([row], lambda: None, None, None, {}, photo=photo)
        service._schedule_photo(row, send)
        await asyncio.gather(*service.photo_tasks.values())
        assert messages == [expected]
    asyncio.run(scenario())
