import pytest
from original_client_letter_contract import serialize_letter_detail, _published


@pytest.mark.parametrize('state', [None, 'PLANNING', 'GENERATING', 'RETRY_PENDING'])
def test_photo_and_letter_are_hidden_until_photo_finishes(state):
    row = dict(letter_id='synthetic', letter_status='COMPLETED', reply_mode='text_letter',
               reply_text='private reply', image_reply_settings={'enabled': True}, image_status=state)
    detail = serialize_letter_detail(row, include_legacy_aliases=True)
    assert detail['letterStatus'] == 3
    assert detail['replyText'] == detail['reply_text'] == ''
    assert 'private reply' not in str(detail)
    assert 'replyImageUrl' not in detail
    assert not _published(row, now=None)


@pytest.mark.parametrize('state', ['COMPLETED', 'SKIPPED', 'FAILED'])
def test_terminal_photo_releases_reply_without_hanging(state):
    row = dict(letter_id='synthetic', letter_status='COMPLETED', reply_mode='text_letter',
               reply_text='reply', image_reply_settings={'enabled': True}, image_status=state,
               reply_image_url='http://127.0.0.1:9000/toy/media/photo-test.png')
    detail = serialize_letter_detail(row)
    assert detail['letterStatus'] == 4 and detail['replyText'] == 'reply'
    assert bool(detail.get('replyImageUrl')) == (state == 'COMPLETED')


def test_pending_photo_gates_raw_send_detail_and_unread(monkeypatch):
    import asyncio
    import local_server as server
    row = dict(letter_id='synthetic', letter_status='COMPLETED', reply_mode='text_letter',
               reply_text='private reply', content='photo please', is_read=0,
               image_reply_settings={'enabled': True}, image_status='GENERATING')
    monkeypatch.setattr(server.store, 'letters', [row])
    assert server._send_result_for_letter(row)['data']['status'] == 'PENDING'
    assert server._active_undelivered_letter() is row
    async def scenario():
        detail = await server.route('GET', '/toy/letter/detail', {}, {'letter_id': 'synthetic'})
        assert detail['data']['reply_text'] == '' and row['is_read'] == 0
        unread = await server.route('GET', '/toy/letter/unread_count', {}, {})
        assert unread['data']['unread_count'] == 0
        row['image_status'] = 'COMPLETED'
        assert server._send_result_for_letter(row)['data']['status'] == 'COMPLETED'
        unread = await server.route('GET', '/toy/letter/unread_count', {}, {})
        assert unread['data']['unread_count'] == 1
    asyncio.run(scenario())
