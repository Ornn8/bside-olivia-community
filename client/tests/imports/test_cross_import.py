import json
import pytest

from runtime.imports.letter_backup import export_letters, import_letters
from runtime.imports.offline_letter_pairs import apply_offline_letter_pair_recovery_to_adapter
from runtime.memory.local_memory import LocalMemoryAdapter
from runtime.imports.relationship_batches import archive_exchanges


@pytest.mark.parametrize('selected_first', [True, False])
def test_select_file_and_history_directory_share_one_archive_identity(tmp_path, selected_first):
    pairs = [{'content': '记得蓝色杯子吗？', 'reply': '记得，杯底有一只猫。'}]
    path = tmp_path / 'letter_pairs.json'
    path.write_text(json.dumps(pairs, ensure_ascii=False), encoding='utf-8')
    with LocalMemoryAdapter(tmp_path / 'archive.sqlite3') as archive:
        def selected():
            return import_letters(path.read_text(encoding='utf-8'), adapter=archive)
        def history():
            return apply_offline_letter_pair_recovery_to_adapter(path, adapter=archive)
        first, second = (selected, history) if selected_first else (history, selected)
        first()
        second()
        assert len(archive.list_legacy()) == 1
        assert len(archive_exchanges(archive.list_legacy())) == 1


def test_exported_history_then_local_history_does_not_duplicate(tmp_path):
    pairs = [{'content': '原信', 'reply': '原回信'}]
    path = tmp_path / 'letter_pairs.json'
    path.write_text(json.dumps(pairs), encoding='utf-8')
    with LocalMemoryAdapter(tmp_path / 'source.sqlite3') as source:
        apply_offline_letter_pair_recovery_to_adapter(path, adapter=source)
        letters = source.list_legacy()
        for letter in letters:
            letter['created_at'] = None
            letter['replied_at'] = None
        backup = export_letters(letters)
    with LocalMemoryAdapter(tmp_path / 'destination.sqlite3') as destination:
        import_letters(backup, adapter=destination)
        result = apply_offline_letter_pair_recovery_to_adapter(path, adapter=destination)
        assert result.inserted == 0
        assert len(destination.list_legacy()) == 1
