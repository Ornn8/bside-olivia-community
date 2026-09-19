import asyncio
from types import SimpleNamespace

from runtime.personal_chat.stickers import choices, extract
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService
from runtime.letter_stickers.selection import allowed_stickers


def test_stickers_are_occasional_and_illegal_metadata_never_enters_text():
    assert choices([], SimpleNamespace()) == {}
    rows = [dict(channel='wechat', delivery_status='DELIVERED') for _ in range(4)]
    candidates = choices(rows, SimpleNamespace())
    assert len(candidates) == 10
    assert extract('你好[[sticker:../../secret]]', candidates) == ('你好', None)
    key = next(iter(candidates))
    assert extract(f'你好[[sticker:{key}]]', candidates) == ('你好', key)
    rows[-1]['sticker_delivery_status'] = 'UNKNOWN'
    assert choices(rows, SimpleNamespace()) == {}


def test_image_failure_does_not_undo_text_memory_or_resend_on_replay():
    async def run():
        rows, sent, committed, images = [], [], [], []
        async def generate(event, row):
            row['sticker_id'] = 'linli-01'
            return '好呀'
        async def send(text):
            sent.append(text)
        async def image(path):
            assert rows[0]['delivery_status'] == 'DELIVERED'
            images.append(path.name)
            raise RuntimeError('WECHAT_IMAGE_UPLOAD_FAILED')
        send.image = image
        async def commit(row):
            committed.append(row['reply_text'])
        service = PersonalChatService(rows, lambda: None, generate, commit, {'wechat': ('bot', 'owner')})
        event = PersonalMessage('wechat', 'bot', 'owner', '1', '嘿')
        await service.handle(event, send)
        await service.handle(event, send)
        assert sent == ['好呀'] and images == ['linli-01.png']
        assert committed and set(committed) == {'好呀'}
        assert rows[0]['delivery_status'] == 'DELIVERED'
        assert rows[0]['sticker_delivery_status'] == 'UNKNOWN'
    asyncio.run(run())


def test_candidates_never_unlock_assets_and_weights_favor_older_unused(monkeypatch):
    from runtime.personal_chat import stickers
    rows = [dict(channel='wechat', delivery_status='DELIVERED', sticker_id='linli-01',
                 sticker_delivery_status='DELIVERED'),
            dict(channel='wechat', delivery_status='DELIVERED', sticker_id='linli-03',
                 sticker_delivery_status='DELIVERED')]
    rows += [dict(channel='wechat', delivery_status='DELIVERED') for _ in range(4)]
    captured = []
    def pick(population, weights):
        captured.append(dict(zip(population, weights)))
        return [population[0]]
    monkeypatch.setattr(stickers.random, 'choices', pick)
    low = SimpleNamespace()
    result = choices(rows, low)
    assert set(result) <= set(allowed_stickers(low))
    assert captured[0]['linli-04'] > captured[0]['linli-01'] > captured[0]['linli-03']
    assert 'linli-108' not in captured[0]
    high = SimpleNamespace(familiarity='high', trust='high', comfort='high', closeness='high')
    captured.clear()
    choices(rows, high)
    assert 'linli-108' in captured[0]


def test_sticker_rechecks_current_unlock_after_generation():
    async def run():
        rows, sent, images, committed = [], [], [], []
        async def generate(event, row):
            row['sticker_id'] = 'linli-108'
            return '好呀'
        async def send(text):
            sent.append(text)
        async def image(path):
            images.append(path)
        async def commit(row):
            committed.append(row['reply_text'])
        send.image = image
        service = PersonalChatService(rows, lambda: None, generate, commit, {'wechat': ('bot', 'owner')},
                                      sticker_allowed=lambda key: False)
        await service.handle(PersonalMessage('wechat', 'bot', 'owner', '1', 'hi'), send)
        assert sent == committed == ['好呀'] and not images
        assert rows[0]['sticker_delivery_status'] == 'LOCKED'
    asyncio.run(run())
