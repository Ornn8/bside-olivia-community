import asyncio

import pytest

from runtime import piano


CAPS = {'enabled': True, 'price_ranges_cents': {'midi': [7, 13], 'original': [11, 29]},
        'max_seconds': 120, 'max_midi_bytes': 524288, 'original': True, 'future': 'ignored'}


def test_capabilities_are_optional_and_extensible():
    assert piano.capability({}) is None
    assert piano.capability({'piano': {'enabled': False}}) is None
    assert piano.capability({'piano': {**CAPS, 'enabled': 1}}) is None
    assert piano.capability({'piano': CAPS})['price_ranges_cents'] == CAPS['price_ranges_cents']


@pytest.mark.parametrize('name,data,limit', [('x.txt', b'MThd', 100), ('x.mid', b'junk', 100),
                                          ('x.midi', b'MThd123', 4), ('x.mid', b'', 100)])
def test_rejected_midi(name, data, limit):
    with pytest.raises(ValueError):
        piano.validate_midi(name, data, limit)


def test_midi_and_request():
    piano.validate_midi('曲子.MIDI', b'MThd1234', CAPS['max_midi_bytes'])
    assert piano.task_input({'source': 'original', 'piece_hint': '夜曲', 'seconds': 30}, CAPS) == {
        'source': 'original', 'mood': '夜曲', 'title': '夜曲', 'seconds': 30, 'section': '开头'}
    with pytest.raises(ValueError):
        piano.task_input({'source': 'library', 'piece_hint': 'unknown', 'seconds': 30}, CAPS)
    with pytest.raises(ValueError):
        piano.task_input({'source': 'midi', 'piece_hint': '', 'seconds': 30}, CAPS)
    with pytest.raises(ValueError):
        piano.validate_input({'source': 'original', 'mood': 'x', 'title': 'x', 'seconds': True})


def test_writer_knows_plan_but_not_completion():
    note = piano.writer_instruction({'source': 'original', 'title': '夜曲', 'seconds': 30}, CAPS)
    assert '夜曲' in note and '30' not in note and '一整首' in note
    assert '不声称已经录好' in note and '一两句' in note
    assert '以完成结果为准' in note


def test_metadata_and_memory_are_grounded():
    meta = {'title': '夜曲', 'composer': None, 'key': None, 'tempo_bpm': 72, 'seconds': 30, 'section': '开头'}
    assert '《夜曲》的开头' in piano.completion_text(meta)
    assert piano.validate_meta({**meta, 'future': True}) == meta
    assert piano.validate_meta({**meta, 'composer': '', 'key': ''})['composer'] == ''
    with pytest.raises(ValueError):
        piano.validate_meta({**meta, 'seconds': float('nan')})


def test_native_and_qq_records_are_visible_only_after_completion():
    from types import SimpleNamespace
    meta = dict(title='自作曲', composer=None, key=None, tempo_bpm=80, seconds=30, section='中段')
    server = SimpleNamespace(store=SimpleNamespace(midi_jobs=[dict(job_id='native', piano_meta=meta)], personal_chats=[]),
                             _persist_store_state=lambda: None)
    assert piano.recent_context(server, 'owner') == ''
    asyncio.run(piano.remember(server, 'native', meta))
    assert '客户端上传 MIDI' in piano.recent_context(server, 'owner')
    row = dict(letter_id='qq', channel='qq', binding_id='owner', piano_meta=meta,
               piano_completed_at='2026-10-01T00:00:00+00:00', piano_delivery_status='UNKNOWN')
    assert list(piano.memory_rows(row)) == []
    row['piano_delivery_status'] = 'DELIVERED'
    assert '《自作曲》的中段' in list(piano.memory_rows(row))[0]['reply_text']


@pytest.mark.parametrize('source,seconds,limit,ok', [
    ('original',156,480,True), ('original',480,480,True), ('original',481,480,False),
    ('original',241,240,False), ('midi',120,480,True), ('midi',121,480,False),
    ('original',float('nan'),480,False), ('original',float('inf'),480,False)])
def test_source_specific_result_limit(source, seconds, limit, ok):
    meta = dict(title='雨夜', composer=None, key=None, tempo_bpm=72, seconds=seconds, section='完整一曲')
    if ok:
        assert piano.validate_meta(meta, source=source, original_max_seconds=limit) == meta
    else:
        with pytest.raises(ValueError, match='PIANO_META_INVALID'):
            piano.validate_meta(meta, source=source, original_max_seconds=limit)


@pytest.mark.parametrize('source,seconds,limit,ok', [
    ('original',485,480,True), ('original',485.1,480,False),
    ('original',245,240,True), ('original',246,240,False),
    ('midi',125,480,True), ('midi',126,480,False)])
def test_source_specific_download_limit(monkeypatch, source, seconds, limit, ok):
    from runtime.personal_chat.piano import validate_audio
    monkeypatch.setattr('runtime.media.music_reply._media_duration_seconds', lambda *a, **k: seconds)
    if ok:
        validate_audio(None, source=source, original_max_seconds=limit)
    else:
        with pytest.raises(ValueError, match='PIANO_AUDIO_INVALID'):
            validate_audio(None, source=source, original_max_seconds=limit)


def test_full_length_capability_and_memory():
    from types import SimpleNamespace
    caps = piano.capability({'piano': dict(CAPS, original_full_length=True, original_max_seconds=240)})
    assert caps['original_full_length'] is True and caps['original_max_seconds'] == 240
    assert piano.capability({'piano': CAPS})['original_max_seconds'] == 480
    assert piano.capability({'piano': dict(CAPS, original_max_seconds=True)}) is None
    meta = dict(title='雨夜', composer=None, key=None, tempo_bpm=72, seconds=156, section='完整一曲')
    row = dict(job_id='test', input={'source':'original'}, piano_original_max_seconds=240)
    server = SimpleNamespace(store=SimpleNamespace(midi_jobs=[row]), _persist_store_state=lambda:None)
    asyncio.run(piano.remember(server, 'test', meta))
    text = list(piano.memory_rows(row))[0]['reply_text']
    assert '一整首我自己写的《雨夜》' in text and '156' in text and '开头' not in text
