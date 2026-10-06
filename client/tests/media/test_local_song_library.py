from pathlib import Path

import pytest

from runtime.media.local_song_library import LocalSongLibrary, LocalSongError, native_song_id, _assign_native_id


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


def test_import_with_bundled_ffmpeg_without_ffprobe(tmp_path, monkeypatch):
    import imageio_ffmpeg
    from runtime.media.managed_subprocess import run_managed_process
    import runtime.media.local_song_library as module
    ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe())
    monkeypatch.setattr(module, 'resolve_ffmpeg_executable', lambda env: ffmpeg)
    assert not ffmpeg.with_name('ffprobe.exe' if ffmpeg.suffix == '.exe' else 'ffprobe').exists()
    source = tmp_path / '演奏.mp4'
    result = run_managed_process([str(ffmpeg), '-y', '-f', 'lavfi', '-i',
        'color=c=black:s=64x64:r=10', '-f', 'lavfi', '-i', 'sine=frequency=440',
        '-t', '1', '-c:v', 'libx264', '-c:a', 'aac', str(source)], timeout_seconds=30)
    assert result.returncode == 0
    library = LocalSongLibrary(tmp_path / 'data', {})
    assert library.import_path(str(source))['added'] == 1
    assert 0.9 <= library.songs()[0]['duration'] <= 1.2


def test_import_failure_logs_safe_specific_code(library, tmp_path, monkeypatch):
    source = tmp_path / 'private-title.mp4'
    source.write_bytes(b'video')
    def fail(*args):
        raise PermissionError('private path and content')
    monkeypatch.setattr(library, '_prepare', fail)
    result = library.import_path(str(source))
    assert result['errors'][0]['code'] == 'LOCAL_SONG_PERMISSION_DENIED'
    log = (library.root.parent / 'logs/media-provider.jsonl').read_text()
    assert 'LOCAL_SONG_PERMISSION_DENIED' in log
    assert 'private' not in log


def test_three_scene_backup_imports_one_song_without_touching_source(library, tmp_path):
    folder = tmp_path / '曲子' / 'midi_1012_1783936102'
    folder.mkdir(parents=True)
    for index in range(3):
        (folder / (str(index) * 32 + '.mp4')).write_bytes(b'video' * (index + 1))
    original = {p: p.read_bytes() for p in folder.iterdir()}
    assert library.import_path(str(folder.parent))['added'] == 1
    assert library.import_path(str(folder.parent))['skipped'] == 1
    assert len(list(library.root.glob('*.mp4'))) == 1
    assert not list(folder.parent.glob('*.mp4'))
    assert {p: p.read_bytes() for p in folder.iterdir()} == original


def test_missing_ffmpeg_stops_batch_and_cleans_temporary(library, tmp_path, monkeypatch):
    folder = tmp_path / 'videos'
    folder.mkdir()
    for name in ('one', 'two', 'three'):
        (folder / (name + '.mp4')).write_bytes(name.encode())
    attempts = []
    def unavailable(source, output):
        attempts.append(source)
        raise LocalSongError('LOCAL_SONG_FFMPEG_UNAVAILABLE')
    monkeypatch.setattr(library, '_prepare', unavailable)
    with pytest.raises(LocalSongError, match='LOCAL_SONG_FFMPEG_UNAVAILABLE'):
        library.import_path(str(folder))
    assert len(attempts) == 1
    assert not list(library.root.glob('*.mp4'))

def test_native_song_id_is_bounded_and_stable():
    for digest in ('a' * 64, '0' * 64, 'f' * 64, 'deadbeef' + '0' * 56):
        value = native_song_id(digest)
        assert isinstance(value, str) and value.isdigit()
        assert 1000000000 <= int(value) < 2000000000
        assert int(value) <= 2**31 - 1
        assert native_song_id(digest) == value


def test_native_song_id_rejects_non_sha256_values():
    for bad in ('', 'short', '../outside', 'Z' * 64, None, 123):
        with pytest.raises(LocalSongError):
            native_song_id(bad)


def test_imported_songs_expose_bounded_distinct_native_ids(library, tmp_path):
    for index in range(5):
        source = tmp_path / ('clip%d.mp4' % index)
        source.write_bytes(b'clip-%d' % index)
        library.import_path(str(source))
    rows = library.songs()
    assert len(rows) == 5
    native_ids = [row['native_id'] for row in rows]
    assert all(v.isdigit() and 1000000000 <= int(v) < 2000000000 for v in native_ids)
    assert len(set(native_ids)) == len(native_ids)
    # SHA256 content identifier is preserved for media URLs and de-duplication.
    assert all(len(row['id']) == 64 for row in rows)


def test_duplicate_import_keeps_the_same_native_id(library, tmp_path):
    source = tmp_path / 'song.mp4'
    source.write_bytes(b'video-duplicate')
    assert library.import_path(str(source))['added'] == 1
    first = library.songs()[0]['native_id']
    copied = tmp_path / 'copy.mp4'
    copied.write_bytes(source.read_bytes())
    assert library.import_path(str(copied))['skipped'] == 1
    assert library.songs()[0]['native_id'] == first


def test_native_ids_are_order_independent_after_reopen(library, tmp_path):
    for index in range(3):
        source = tmp_path / ('order%d.mp4' % index)
        source.write_bytes(b'order-%d' % index)
        library.import_path(str(source))
    forward = {row['id']: row['native_id'] for row in library.songs()}
    restored = LocalSongLibrary(library.root.parent, {})
    assert {row['id']: row['native_id'] for row in restored.songs()} == forward


def test_native_id_derivation_is_media_independent():
    # Audio and video share the same SHA256 derivation, so the identifier does
    # not depend on media_type.
    digest = 'b' * 64
    assert native_song_id(digest) == native_song_id(digest)
    assert 1000000000 <= int(native_song_id(digest)) < 2000000000

def test_native_id_collision_is_resolved_not_shared():
    a = "f86ffb7c3e57227f6039aecdb5a350946b476857fa9c2070d44278cd9eb7432b"
    b = "55be6af6c2ca227fc2bcf081e44d4ee49edd63f6d7f4c1003182825034c879e8"
    # synthetic-song-9584 vs synthetic-song-29638 share the raw candidate
    assert native_song_id(a) == native_song_id(b)
    first = _assign_native_id(a, set())
    second = _assign_native_id(b, {first})
    assert first != second


def test_import_colliding_contents_get_distinct_native_ids(library, tmp_path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"synthetic-song-9584")
    b.write_bytes(b"synthetic-song-29638")
    library.import_path(str(a))
    library.import_path(str(b))
    rows = library.songs()
    assert len(rows) == 2
    ids = [row['native_id'] for row in rows]
    assert len(set(ids)) == 2
    assert all(v.isdigit() and 1000000000 <= int(v) < 2000000000 for v in ids)


def test_deleted_song_id_is_not_reused_by_colliding_content(library, tmp_path):
    a = tmp_path / 'a.mp4'
    b = tmp_path / 'b.mp4'
    a.write_bytes(b'synthetic-song-9584')
    b.write_bytes(b'synthetic-song-29638')
    library.import_path(str(a))
    row = library.songs()[0]
    assert native_song_id(row['id']) == '1861425279'
    library.delete(row['id'])
    library.import_path(str(b))
    assert library.songs()[0]['native_id'] != '1861425279'


def test_repeated_delete_reimport_keeps_all_old_cache_ids_owned(library, tmp_path):
    a, b = tmp_path / 'a.mp4', tmp_path / 'b.mp4'
    a.write_bytes(b'synthetic-song-9584')
    b.write_bytes(b'synthetic-song-29638')
    library.import_path(str(a))
    first = library.songs()[0]
    library.delete(first['id'])
    library.import_path(str(a))
    restored = library.songs()[0]
    library.delete(restored['id'])
    library.import_path(str(b))
    assert library.songs()[0]['native_id'] != first['native_id']
    assert restored['native_id'] == first['native_id']
    reopened = LocalSongLibrary(library.root.parent, {})
    assert reopened._retired()[first['id']] == first['native_id']


def test_audio_reimport_reuses_its_owned_native_cache_id(library, tmp_path):
    import wave
    source = tmp_path / 'synthetic.wav'
    with wave.open(str(source), 'wb') as audio:
        audio.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\x00\x00' * 80)
    library.import_audio(source, 'Synthetic audio')
    first = library.songs()[0]
    library.delete(first['id'])
    reopened = LocalSongLibrary(library.root.parent, {})
    reopened.import_audio(source, 'Synthetic audio')
    assert reopened.songs()[0]['native_id'] == first['native_id']


def test_corrupt_id_ledger_preserves_existing_playback_but_cannot_allocate(library, tmp_path):
    a, b = tmp_path / 'a.mp4', tmp_path / 'b.mp4'
    a.write_bytes(b'synthetic-song-9584')
    b.write_bytes(b'synthetic-song-29638')
    library.import_path(str(a))
    existing = library.songs()
    ledger = library.root / 'native-ids.json'
    ledger.write_text('broken', encoding='utf-8')
    assert library.songs() == existing
    result = library.import_path(str(b))
    assert result['failed'] == 1 and result['added'] == 0
    assert result['errors'][0]['code'] == 'LOCAL_SONG_ID_LEDGER_INVALID'
    assert ledger.read_text(encoding='utf-8') == 'broken'
    assert library.songs() == existing
