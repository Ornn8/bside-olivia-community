import pytest
from original_client_letter_contract import serialize_letter_detail, serialize_letter_summary, _published


@pytest.mark.parametrize('state', [None, 'PLANNING', 'GENERATING', 'RETRY_PENDING'])
@pytest.mark.parametrize('mode', ['text_letter', 'voice_reply'])
def test_optional_photo_never_hides_completed_text_or_audio(state, mode):
    row = dict(letter_id='synthetic', letter_status='COMPLETED', reply_mode='text_letter',
               reply_text='completed reply', image_reply_settings={'enabled': True}, image_status=state,
               media_status='COMPLETED', reply_video_enabled=False,
               reply_audio_url='http://127.0.0.1:9000/toy/media/reply.wav')
    row['reply_mode'] = mode
    detail = serialize_letter_detail(row, include_legacy_aliases=True)
    assert detail['letterStatus'] == 4
    assert detail['replyText'] == detail['reply_text'] == 'completed reply'
    assert detail['imageStatus'] == (state or 'PLANNING')
    assert detail['imageRequestId'] == row['letter_id']
    if mode == 'voice_reply':
        assert detail['replyAudioUrl'] == row['reply_audio_url']
        assert serialize_letter_summary(row)['audioRevision'] == row['reply_audio_url']
    assert 'replyImageUrl' not in detail
    assert _published(row, now=None)


def test_optional_photo_initial_query_keeps_polling_before_worker_first_tick():
    row = dict(letter_id='new-reply', letter_status='COMPLETED', reply_mode='text_letter',
               reply_text='completed reply', image_reply_settings={'enabled': True})
    detail = serialize_letter_detail(row)
    assert detail['imageRequestId'] == 'new-reply' and detail['imageStatus'] == 'PLANNING'
    assert 'image_status' not in row  # Read projection does not change the pending worker's state.


@pytest.mark.parametrize('state', [None, 'PLANNING', 'GENERATING', 'RETRY_PENDING'])
def test_primary_image_keeps_scene_draft_hidden_until_image_finishes(state):
    row = dict(letter_id='primary-image', letter_status='COMPLETED', reply_mode='text_letter',
               reply_text='INTERNAL SCENE DRAFT', companion_delivery='image',
               image_reply_settings={'enabled': True}, image_status=state)
    detail = serialize_letter_detail(row, include_legacy_aliases=True)
    assert detail['letterStatus'] == 3
    assert detail['replyText'] == detail['reply_text'] == ''
    assert 'INTERNAL SCENE DRAFT' not in str(detail)
    assert not _published(row, now=None)


@pytest.mark.parametrize('state', ['COMPLETED', 'SKIPPED', 'FAILED'])
def test_terminal_photo_releases_reply_without_hanging(state):
    row = dict(letter_id='synthetic', letter_status='COMPLETED', reply_mode='text_letter',
               reply_text='reply', image_reply_settings={'enabled': True}, image_status=state,
               reply_image_url='http://127.0.0.1:9000/toy/media/photo-test.png')
    detail = serialize_letter_detail(row)
    assert detail['letterStatus'] == 4 and detail['replyText'] == 'reply'
    assert bool(detail.get('replyImageUrl')) == (state == 'COMPLETED')


@pytest.mark.parametrize('state', ['GENERATING', 'RETRY_PENDING'])
def test_pending_optional_photo_releases_send_detail_unread_and_new_letter(monkeypatch, state):
    import asyncio
    from copy import deepcopy
    import local_server as server
    row = dict(letter_id='synthetic', letter_status='COMPLETED', reply_mode='voice_reply',
               reply_text='completed reply', content='hello', is_read=0,
               media_status='COMPLETED', reply_video_enabled=False,
               reply_audio_url='http://127.0.0.1:9000/toy/media/reply.wav',
               image_reply_settings={'enabled': True}, image_status=state,
               image_cloud_task_id='same-paid-image-task', image_receipt_required=True)
    binding = deepcopy(row)
    def no_generation_or_memory(*args, **kwargs):
        raise AssertionError('Publication reads must not generate or commit another reply.')
    monkeypatch.setattr(server.letters_adapter, 'remember_conversation', no_generation_or_memory)
    monkeypatch.setattr(server, '_schedule_media_job', no_generation_or_memory)
    monkeypatch.setattr(server, '_persist_store_state', lambda: None)
    monkeypatch.setattr(server.store, 'letters', [row])
    assert server._send_result_for_letter(row)['data']['status'] == 'COMPLETED'
    assert server._active_undelivered_letter() is None
    async def scenario():
        unread = await server.route('GET', '/toy/letter/unread_count', {}, {})
        assert unread['data']['unread_count'] == 1
        detail = await server.route('GET', '/toy/letter/detail', {}, {'letter_id': 'synthetic'})
        assert detail['data']['reply_text'] == 'completed reply' and row['is_read'] == 1
        waiting = await server.route('GET', '/toy/image/status', {}, {'letter_id': 'synthetic'})
        assert waiting['data']['imageStatus'] == state
        assert 'replyImageUrl' not in waiting['data']
        unread = await server.route('GET', '/toy/letter/unread_count', {}, {})
        assert unread['data']['unread_count'] == 0
        row.update(image_status='COMPLETED', reply_image_url='http://127.0.0.1:9000/toy/media/photo-test.png')
        assert server._send_result_for_letter(row)['data']['status'] == 'COMPLETED'
        ready = await server.route('GET', '/toy/image/status', {}, {'letter_id': 'synthetic'})
        assert ready['data']['replyImageUrl'] == row['reply_image_url']
        assert serialize_letter_detail(row)['replyAudioUrl'] == binding['reply_audio_url']
        unread = await server.route('GET', '/toy/letter/unread_count', {}, {})
        assert unread['data']['unread_count'] == 0  # Attachment readiness does not reannounce the letter.
        assert row['image_cloud_task_id'] == binding['image_cloud_task_id']
        assert row['image_receipt_required'] is True
        assert row['reply_text'] == binding['reply_text']
    asyncio.run(scenario())


@pytest.mark.parametrize('status,error', [('FAILED','LLM_USAGE_PENDING'), ('FAILED','REPLY_QUALITY_BLOCKED'),
                                        ('PROCESSING',None), ('SKIPPED',None)])
def test_photo_decoupling_never_publishes_failed_or_incomplete_reply(status, error):
    row = dict(letter_id='blocked', letter_status=status, error_code=error, reply_mode='voice_reply',
               reply_text='UNAPPROVED DRAFT', media_status='COMPLETED', reply_video_enabled=False,
               reply_audio_url='http://127.0.0.1:9000/toy/media/reply.wav',
               image_reply_settings={'enabled': True}, image_status='GENERATING')
    detail = serialize_letter_detail(row)
    assert not _published(row, now=None)
    assert detail['replyText'] == '' and not detail.get('replyAudioUrl')
    assert not detail.get('imageRequestId') and not detail.get('replyImageUrl')
    assert 'UNAPPROVED DRAFT' not in str(detail)


def test_optional_photo_does_not_override_reply_delay_or_unfinished_audio():
    base = dict(letter_id='waiting', letter_status='COMPLETED', reply_mode='voice_reply',
                reply_text='completed reply', reply_video_enabled=False,
                image_reply_settings={'enabled': True}, image_status='GENERATING')
    for waiting in (dict(base, media_status='PROCESSING'), dict(base, media_status='COMPLETED', reply_not_before=120)):
        assert not _published(waiting, now=60)
        assert serialize_letter_detail(waiting, now=60)['replyText'] == ''
