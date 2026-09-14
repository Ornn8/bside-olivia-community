import pytest
from runtime.imports.letter_backup import export_letters, import_letters, validate_backup
from runtime.memory.local_memory import LocalMemoryAdapter


def test_roundtrip_preserves_exact_text_time_and_identity(tmp_path):
    letters = [{'letter_id': 'local-1', 'content': '  杯底画了一只蜗牛。\r\n围巾是橙色的。\n',
                'reply_text': '记下了。\n\n林离', 'created_at': 1788000000,
                'replied_at': 1788000100, 'reply_mode': 'voice',
                'api_key': 'never-export', 'media_path': 'never-export'}]
    payload = export_letters(letters)
    assert 'never-export' not in str(payload)
    with LocalMemoryAdapter(tmp_path/'memory.sqlite3') as adapter:
        assert import_letters(payload, adapter=adapter)['inserted'] == 1
        assert import_letters(payload, adapter=adapter)['duplicates'] == 1
        copied = export_letters(adapter.list_legacy())
        assert copied['letters'] == payload['letters']
        assert copied['letters'][0]['content'] == letters[0]['content']
        assert import_letters(copied, adapter=adapter)['duplicates'] == 1


def test_same_machine_restore_does_not_duplicate_live_letters(tmp_path):
    letters = [{'letter_id':'live-1', 'content':'原信', 'reply_text':'回信'}]
    with LocalMemoryAdapter(tmp_path/'memory.sqlite3') as adapter:
        result = import_letters(export_letters(letters), adapter=adapter, existing=letters)
        assert result['inserted'] == 0 and result['duplicates'] == 1


def test_invalid_late_record_cannot_partially_import(tmp_path):
    payload = export_letters([{'letter_id':'one', 'content':'完整原信'}])
    payload['letters'].append({'content': 42})
    with LocalMemoryAdapter(tmp_path/'memory.sqlite3') as adapter:
        with pytest.raises(ValueError):
            import_letters(payload, adapter=adapter)
        assert not adapter.list_legacy()


def test_unknown_dates_stay_unknown_and_invalid_schema_is_rejected():
    payload = export_letters([{'content':'日期未知的旧信'}])
    assert validate_backup(payload)[0]['created_at'] is None
    with pytest.raises(ValueError):
        validate_backup({'schema_version':'other','letters':[]})
