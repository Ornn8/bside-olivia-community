import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from runtime.personal_chat import backend
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService
from tests.http.test_jev_image_delivery import image_record


@pytest.mark.parametrize('delivery', [None, 'SENDING', 'UNKNOWN', 'DELIVERED'])
def test_primary_image_restart_sends_saved_artifact_once(monkeypatch, delivery):
    async def scenario():
        event = PersonalMessage('qq', '100', '200', '1', 'photo please')
        row = dict(letter_id=event.exchange_id, binding_id=event.binding_id, channel='qq',
                   content=event.text, source_messages=dict(event.sources), reply_text='scene draft',
                   companion_decision=image_record(), companion_delivery='image',
                   delivery_status='MEDIA_PENDING', image_status='COMPLETED',
                   prepared_image='synthetic.png', image_delivery_status=delivery)
        rows, sent = [deepcopy(row)], []
        server = SimpleNamespace(store=SimpleNamespace(personal_chats=rows), _persist_store_state=lambda: None)

        async def prepare(*args):
            assert rows[0]['image_status'] == 'COMPLETED'

        async def noop(*args):
            pass

        monkeypatch.setattr(backend, 'prepare_chat_photo', prepare)
        from runtime import image_understanding
        monkeypatch.setattr(image_understanding, 'commit_image_memory', noop)

        class Send:
            def for_exchange(self, recovered):
                assert recovered.message_id == '1'
                return self

            async def __call__(self, text):
                raise AssertionError('scene draft must not be sent')

            async def image(self, path):
                sent.append(path)
                return 'image-ack'

        service = PersonalChatService(rows, lambda: None, None, noop, {'qq': ('100', '200')},
                                      photo=lambda row, send: backend.deliver_photo(server, row, send))
        service.resume_media('qq', Send())
        service.resume_media('qq', Send())
        await asyncio.gather(*service.photo_tasks.values())
        service.resume_media('qq', Send())
        await asyncio.gather(*service.consumer_tasks.values())
        assert sent == ([] if delivery else ['synthetic.png'])
        if not delivery:
            assert rows[0]['delivery_status'] == 'DELIVERED'
            assert rows[0]['reply_text'] != 'scene draft'

    asyncio.run(scenario())


@pytest.mark.parametrize('state', ['PLANNING', 'GENERATING'])
def test_missing_paid_plan_does_not_start_another_provider_request(state):
    from runtime.image_reply import _prepare_once
    persisted = []
    server = SimpleNamespace(video_reply_settings_store=SimpleNamespace(image_snapshot=lambda: {'enabled': True}),
                             _persist_store_state=lambda: persisted.append(True))
    row = dict(image_status=state, image_recovery_required=True)
    asyncio.run(_prepare_once(server, row, 'photo please', 'scene draft', channel='qq'))
    assert row['image_status'] == 'FAILED' and row['image_error_code'] == 'IMAGE_RECOVERY_CONTEXT_MISSING'
    assert persisted == [True]


def test_pending_generation_requires_saved_receipt_and_binding(monkeypatch):
    async def scenario():
        event = PersonalMessage('qq', '100', '200', '1', 'photo please')
        row = dict(letter_id=event.exchange_id, binding_id=event.binding_id, channel='qq',
                   content=event.text, source_messages=dict(event.sources), reply_text='scene draft',
                   companion_decision=image_record(), companion_delivery='image',
                   delivery_status='MEDIA_PENDING', image_status='GENERATING', image_generation_binding='fingerprint')
        called = []

        async def photo(row, send):
            called.append(row['image_receipt_required'])

        class Send:
            async def image(self, path):
                raise AssertionError('must not submit without receipt')

        service = PersonalChatService([row], lambda: None, None, None, {'qq': ('100', '200')}, photo=photo)
        row['binding_id'] = 'other-binding'
        service.resume_media('qq', Send())
        assert not service.photo_tasks
        row['binding_id'] = event.binding_id
        service.resume_media('qq', Send())
        await asyncio.gather(*service.photo_tasks.values())
        assert called == [True]

    asyncio.run(scenario())


def test_media_outbox_bounds_work_and_shutdown_clears_waiters():
    async def scenario():
        rows, active = [], []
        entered = asyncio.Event()
        for number in range(3):
            event = PersonalMessage('qq', '100', '200', str(number), 'photo please')
            rows.append(dict(letter_id=event.exchange_id, binding_id=event.binding_id, channel='qq',
                             content=event.text, source_messages=dict(event.sources), reply_text='scene draft',
                             companion_decision=image_record(), companion_delivery='image', delivery_status='MEDIA_PENDING'))

        async def photo(row, send):
            active.append(row['letter_id'])
            if len(active) == 2:
                entered.set()
            await asyncio.Event().wait()

        class Send:
            async def image(self, path):
                pass

        service = PersonalChatService(rows, lambda: None, None, None, {'qq': ('100', '200')}, photo=photo)
        service.resume_media('qq', Send())
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0)
        assert len(active) == 2
        tasks = tuple(service.photo_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert not service.photo_tasks

    asyncio.run(scenario())
