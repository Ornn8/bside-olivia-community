import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from runtime import piano, piano_collection as collection
from tests.media.test_piano import CAPS


@pytest.fixture
def owner(tmp_path):
    env = {'OLIVIA_GPU_API_URL': 'https://media.test', 'OLIVIA_GPU_API_KEY': 'test-account-a'}
    return SimpleNamespace(_os=SimpleNamespace(environ=env), _local_data_root=lambda: tmp_path)


def test_original_deduplicated_with_metadata_and_account_isolation(owner):
    raw = b'MThdsynthetic-original'
    first = collection.save(owner, raw, 'Evening', 'qq', 1024)
    second = collection.save(owner, raw, 'Evening revised', 'client', 1024)
    assert first['sha256'] == hashlib.sha256(raw).hexdigest() == second['sha256']
    assert first['uploaded_at'] == second['uploaded_at']
    assert second['source'] == 'qq'
    assert len(collection.pieces(owner)) == 1
    assert (collection.root(owner) / (first['sha256'] + '.mid')).read_bytes() == raw
    assert 'Evening revised' in collection.context(owner)
    owner._os.environ['OLIVIA_GPU_API_KEY'] = 'test-account-b'
    assert collection.pieces(owner) == []
    assert collection.context(owner) == ''


def test_replay_exact_title_section_and_ambiguity(owner):
    first = collection.save(owner, b'MThdA', 'Evening', 'client', 1024)
    request = dict(source='midi', piece_hint='《Evening》', seconds=30, section='中段')
    payload = piano.task_input(request, CAPS, pieces=collection.pieces(owner))
    assert payload['midi_asset'] == first['sha256'] and payload['section'] == '中段'
    for hint in ('Unknown', 'Even'):
        with pytest.raises(ValueError, match='PIANO_PIECE_UNKNOWN'):
            piano.task_input(dict(request, piece_hint=hint), CAPS, pieces=collection.pieces(owner))
    collection.save(owner, b'MThdB', 'Evening', 'qq', 1024)
    with pytest.raises(ValueError, match='PIANO_PIECE_UNKNOWN'):
        piano.task_input(request, CAPS, pieces=collection.pieces(owner))


def test_upload_deduplicates_and_checks_original_even_when_receipt_exists(owner):
    piece = collection.save(owner, b'MThdA', 'Evening', 'client', 1024)
    calls = []
    async def upload(path):
        calls.append(path)
        await asyncio.sleep(0)
        return piece['sha256']
    api = SimpleNamespace(upload=upload)
    async def run():
        assert await asyncio.gather(*(collection.upload(owner, api, piece, 1024) for _ in range(2))) == [piece['sha256']] * 2
        assert len(calls) == 1
        calls[0].write_bytes(b'MThdchanged')
        with pytest.raises(ValueError, match='PIANO_FILE_CHANGED'):
            await collection.upload(owner, api, piece, 1024)
    asyncio.run(run())


def test_account_change_during_upload_cannot_authorize_new_account(owner):
    piece = collection.save(owner, b'MThdA', 'Evening', 'client', 1024)
    async def upload(path):
        owner._os.environ['OLIVIA_GPU_API_KEY'] = 'test-account-b'
        return piece['sha256']
    with pytest.raises(ValueError, match='PIANO_ACCOUNT_CHANGED'):
        asyncio.run(collection.upload(owner, SimpleNamespace(upload=upload), piece, 1024))


@pytest.mark.parametrize('ranges', [None, {}, {'midi': [1, 2]}, {'midi': [2, 1], 'original': [1, 2]}, {'midi': [True, 2], 'original': [1, 2]}])
def test_missing_or_invalid_price_ranges_fail_closed(ranges):
    assert piano.capability({'piano': dict(CAPS, price_ranges_cents=ranges)}) is None


@pytest.mark.parametrize('section', ['开头', '中段', '结尾'])
def test_section_survives_intent_and_task(section):
    intent = dict(source='original', piece_hint='Evening', seconds=30, section=section)
    assert piano.task_input(intent, CAPS)['section'] == section
    with pytest.raises(ValueError):
        piano.validate_intent(dict(intent, section='unknown'))


def test_letter_context_contains_titles_only(owner, monkeypatch):
    collection.save(owner, b'MThdA', 'Evening', 'client', 1024)
    for key, value in owner._os.environ.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(owner._local_data_root()))
    note = collection.environment_context()
    assert 'Evening' in note and 'MThdA' not in note and 'test-account-a' not in note


def test_decision_request_negotiates_piano():
    from runtime.reply.companion_decision import JevDecisionPort
    port = JevDecisionPort('http://127.0.0.1:8097/v1/companion/decide', token='synthetic')
    assert port.request_headers('0' * 64)['X-Olivia-Piano'] == '1'
