from copy import deepcopy

import pytest

from runtime.imports.letter_backup import export_letters, import_letters, validate_backup
from runtime.memory.local_memory import LocalMemoryAdapter


def chat(identity='im-confirmed', **changes):
    return {
        'letter_id': identity, 'channel': 'qq', 'binding_id': 'never-export-binding',
        'content': '橙色围巾上有一只蜗牛。', 'reply_text': '记住这只蜗牛了。',
        'created_at': 1790742900, 'user_sent_at': '2026-09-30T12:34:00+08:00',
        'private_world_occurred_at': '2026-09-30T12:35:00+08:00',
        'delivery_status': 'DELIVERED', 'letter_status': 'COMPLETED',
        'reply_mode': 'future_im', 'source_messages': {'never-export-message-id': 'hello'},
        'api_key': 'never-export-key', 'prepared_audio': 'never-export-path',
        'incoming_images': ['never-export-image'],
        'speech_script': {'spoken_text': 'never-export-story'}, 'speech_delivery_status': 'DELIVERED',
        **changes,
    }


def project(rows):
    from runtime.imports.letter_backup import personal_chat_letters
    return personal_chat_letters(rows)


def test_chat_roundtrip_preserves_channel_source_time_and_exact_text(tmp_path):
    chats = [chat(), chat('im-wechat', channel='wechat', content='  原文\r\n换行。\n')]
    snapshot = deepcopy(chats)
    backup = export_letters(project(chats))
    assert chats == snapshot
    assert 'never-export' not in str(backup)
    assert [(row['channel'], row['delivery_status']) for row in backup['letters']] == [
        ('qq', 'DELIVERED'), ('wechat', 'DELIVERED')]
    assert backup['letters'][0]['source_id'] == 'im-confirmed'
    assert backup['letters'][0]['created_at'] == '2026-09-30T04:34:00+00:00'
    assert backup['letters'][0]['replied_at'] == '2026-09-30T04:35:00+00:00'
    assert backup['letters'][1]['content'] == chats[1]['content']
    with LocalMemoryAdapter(tmp_path / 'archive.sqlite3') as archive:
        assert import_letters(backup, adapter=archive)['inserted'] == 2
        assert import_letters(backup, adapter=archive)['duplicates'] == 2
        assert sorted(export_letters(archive.list_legacy())['letters'], key=lambda row: row['source_id']) == sorted(
            backup['letters'], key=lambda row: row['source_id'])
        assert archive.search('蜗牛')


@pytest.mark.parametrize('state', ['RECEIVED', 'GENERATING', 'GENERATED', 'MEDIA_PENDING',
    'FAILED', 'SENDING', 'DELIVERY_UNCONFIRMED', 'SKIPPED'])
def test_received_text_is_backed_up_without_unconfirmed_assistant_draft(state, tmp_path):
    original = chat(delivery_status=state, reply_text='never-export-draft')
    backup = export_letters(project([original]))
    row, = backup['letters']
    assert row['content'] == original['content']
    assert row['reply_text'] == ''
    assert row['replied_at'] is None
    assert row['channel'] == 'qq' and row['delivery_status'] == 'RECEIVED_ONLY'
    assert 'never-export' not in str(backup)
    with LocalMemoryAdapter(tmp_path / 'archive.sqlite3') as archive:
        import_letters(backup, adapter=archive)
        from runtime.imports.relationship_batches import archive_exchanges
        assert not archive_exchanges(archive.list_legacy())


def test_merged_input_is_not_exported_twice_and_orphan_is_preserved():
    parent = chat(source_messages={'first': 'one', 'second': 'two'}, content='one\ntwo')
    child = chat('im-child', content='two', source_messages={'second': 'two'},
                 delivery_status='SKIPPED', superseded_by=parent['letter_id'])
    rows = export_letters(project([parent, child]))['letters']
    assert len(rows) == 1 and rows[0]['content'] == 'one\ntwo'
    orphan, = export_letters(project([child]))['letters']
    assert orphan['content'] == 'two' and orphan['reply_text'] == ''
    unrelated = chat('im-parent', content='other', source_messages={'first': 'one'})
    child['superseded_by'] = unrelated['letter_id']
    assert len(export_letters(project([unrelated, child]))['letters']) == 2


def test_unsent_proactive_has_no_received_user_message_to_backup():
    pending = chat(content='', origin='proactive', delivery_status='GENERATED',
                   reply_text='never-export-draft')
    assert export_letters(project([pending]))['letters'] == []
    delivered = chat(content='', origin='proactive')
    row, = export_letters(project([delivered]))['letters']
    assert row['origin'] == 'proactive' and row['reply_text'] == delivered['reply_text']


def test_legacy_letter_identity_is_unchanged_by_optional_chat_fields():
    from runtime.imports.letter_backup import identity
    old = {
        'content': 'synthetic', 'reply_text': 'reply', 'title': '', 'origin': 'user',
        'reply_mode': 'text', 'letter_status': 'COMPLETED', 'created_at': None,
        'replied_at': None, 'source_id': 'old-letter',
    }
    assert validate_backup({'schema_version': 'olivia.letters.v1', 'letters': [old]}) == (old,)
    assert identity(export_letters([{'letter_id': 'old-letter', 'content': 'synthetic',
        'reply_text': 'reply'}])['letters'][0]) == identity(old)


@pytest.mark.parametrize('changes', [
    {'channel': 'unsupported'},
    {'delivery_status': 'SENDING'},
    {'delivery_status': 'RECEIVED_ONLY', 'reply_text': 'unconfirmed draft'},
])
def test_invalid_chat_backup_cannot_partially_import(changes, tmp_path):
    backup = export_letters([{'letter_id': 'one', 'content': 'valid original'}])
    invalid = {**backup['letters'][0], 'channel': 'qq', 'delivery_status': 'DELIVERED', **changes}
    backup['letters'].append(invalid)
    with LocalMemoryAdapter(tmp_path / 'archive.sqlite3') as archive:
        with pytest.raises(ValueError):
            import_letters(backup, adapter=archive)
        assert not archive.list_legacy()


def test_same_machine_restore_recognizes_live_chat(tmp_path):
    live = project([chat()])
    with LocalMemoryAdapter(tmp_path / 'archive.sqlite3') as archive:
        result = import_letters(export_letters(live), adapter=archive, existing=live)
        assert result['inserted'] == 0 and result['duplicates'] == 1
