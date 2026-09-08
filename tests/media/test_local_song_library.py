from pathlib import Path

import pytest

from runtime.media.local_song_library import LocalSongLibrary, LocalSongError


@pytest.fixture
def library(tmp_path, monkeypatch):
    library = LocalSongLibrary(tmp_path / 'data', {})
    monkeypatch.setattr(library, '_prepare', lambda source, output: (output.write_bytes(source.read_bytes()), 12.5)[1])
    return library


def test_import_duplicate_rename_restart_delete_preserves_original(library, tmp_path):
    source = tmp_path / '演奏.mp4'
    source.write_bytes(b'video-one')
    assert library.import_path(str(source))['added'] == 1
    copied = tmp_path / 'renamed.mp4'
    copied.write_bytes(source.read_bytes())
    assert library.import_path(str(copied))['skipped'] == 1
    song = library.songs()[0]
    library.rename(song['id'], '晚安曲')
    restored = LocalSongLibrary(library.root.parent, {})
    assert restored.songs()[0]['name'] == '晚安曲'
    assert restored.media_path(song['id']).read_bytes() == b'video-one'
    restored.delete(song['id'])
    assert restored.songs() == []
    assert source.read_bytes() == copied.read_bytes() == b'video-one'


def test_restore_folder_preserves_manifest_title(library, tmp_path):
    folder = tmp_path / 'backup' / 'midi_10000_123'
    folder.mkdir(parents=True)
    (folder / 'a.mp4').write_bytes(b'a')
    (folder / 'b.mp4').write_bytes(b'b')
    (folder.parent / 'user_midi_manifest.json').write_text(
        '{"songs":[{"mediaId":"midi_10000_123","name":"旧曲"}]}', encoding='utf-8')
    result = library.import_path(str(folder.parent))
    assert result['added'] == 1
    assert all(song['name'].startswith('旧曲') for song in library.songs())


def test_failed_conversion_does_not_publish_or_destroy_source(library, tmp_path, monkeypatch):
    source = tmp_path / 'broken.mp4'
    source.write_bytes(b'broken')
    def fail(source, output):
        output.write_bytes(b'partial')
        raise LocalSongError('LOCAL_SONG_VIDEO_INVALID')
    monkeypatch.setattr(library, '_prepare', fail)
    result = library.import_path(str(source))
    assert result['failed'] == 1
    assert library.songs() == []
    assert not list(library.root.glob('*.mp4'))
    assert source.read_bytes() == b'broken'


@pytest.mark.parametrize('value', ['../outside', '', 'https://example.com/a.mp4'])
def test_paths_and_ids_are_bounded(library, value):
    with pytest.raises(LocalSongError):
        library.import_path(value)
    with pytest.raises(LocalSongError):
        library.delete(value)


def test_midi_notes_are_not_pretended_to_be_video(library, tmp_path):
    source = tmp_path / 'notes.mid'
    source.write_bytes(b'MThd')
    with pytest.raises(LocalSongError, match='LOCAL_SONG_FORMAT_UNSUPPORTED'):
        library.import_path(str(source))


def test_invalid_catalog_does_not_silently_reset(library):
    library.root.mkdir(parents=True, exist_ok=True)
    (library.root / 'catalog.json').write_text('broken')
    with pytest.raises(LocalSongError, match='LOCAL_SONG_CATALOG_INVALID'):
        library.songs()
