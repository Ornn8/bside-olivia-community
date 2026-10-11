import asyncio

import pytest

from runtime import piano


CAPS = {'enabled': True, 'library': [{'piece_id': 'nocturne', 'title': '夜曲', 'composer': '作曲者'}],
        'max_seconds': 120, 'max_midi_bytes': 524288, 'original': True, 'future': 'ignored'}


def test_capabilities_are_optional_and_extensible():
    assert piano.capability({}) is None
    assert piano.capability({'piano': {'enabled': False}}) is None
    assert piano.capability({'piano': {**CAPS, 'enabled': 1}}) is None
    assert piano.capability({'piano': CAPS})['library'][0]['piece_id'] == 'nocturne'


@pytest.mark.parametrize('name,data,limit', [('x.txt', b'MThd', 100), ('x.mid', b'junk', 100),
                                          ('x.midi', b'MThd123', 4), ('x.mid', b'', 100)])
def test_rejected_midi(name, data, limit):
    with pytest.raises(ValueError):
        piano.validate_midi(name, data, limit)


def test_midi_and_request():
    piano.validate_midi('曲子.MIDI', b'MThd1234', CAPS['max_midi_bytes'])
    assert piano.task_input({'source': 'library', 'piece_hint': '夜曲', 'seconds': 30}, CAPS) == {
        'source': 'library', 'piece_id': 'nocturne', 'seconds': 30}
    with pytest.raises(ValueError):
        piano.task_input({'source': 'library', 'piece_hint': 'unknown', 'seconds': 30}, CAPS)
    with pytest.raises(ValueError):
        piano.task_input({'source': 'midi', 'piece_hint': '', 'seconds': 30}, CAPS)
    with pytest.raises(ValueError):
        piano.validate_input({'source': 'original', 'mood': 'x', 'title': 'x', 'seconds': True})


def test_writer_knows_plan_but_not_completion():
    note = piano.writer_instruction({'source': 'library', 'piece_id': 'nocturne', 'seconds': 30}, CAPS)
    assert '夜曲' in note and '30' in note
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
