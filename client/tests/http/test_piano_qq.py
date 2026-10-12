import asyncio
import base64
import hashlib
from types import SimpleNamespace

import pytest

from runtime import piano
from runtime.personal_chat import piano as qq
from runtime.personal_chat.events import PersonalMessage
from tests.media.test_piano import CAPS

META = dict(title='夜曲', composer='作曲者', key='降E大调', tempo_bpm=72, seconds=30, section='开头')


@pytest.mark.parametrize('enabled,name,data,expected', [
    (True, 'demo.mid', b'MThd1234', 'ok'), (True, 'demo.midi', b'wrong', 'error'),
    (True, 'demo.mid', b'MThd' + b'x' * 524288, 'error'),
    (False, 'demo.mid', b'MThd1234', 'ordinary'), (True, 'notes.pdf', b'%PDF-', 'ordinary')],
    ids=['valid', 'bad-header', 'oversized', 'disabled', 'ordinary'])
def test_current_turn_midi_intake(tmp_path, monkeypatch, enabled, name, data, expected):
    monkeypatch.setenv('OLIVIA_GPU_API_URL','http://127.0.0.1:1')
    monkeypatch.setenv('OLIVIA_GPU_API_KEY','synthetic')
    row = {}; calls = []
    async def caps(env): return CAPS if enabled else None
    class API:
        def __init__(self, *args): pass
        async def upload(self, path):
            calls.append(path.read_bytes())
            return hashlib.sha256(path.read_bytes()).hexdigest()
    async def resolve(kind, reference):
        assert (kind, reference) == ('file', 'opaque')
        return {'base64': base64.b64encode(data).decode()}
    monkeypatch.setattr(piano, 'capabilities', caps)
    monkeypatch.setattr('runtime.remote_generation.RemoteGeneration', API)
    from runtime.incoming_media import MEDIA_RESOLVER
    token = MEDIA_RESOLVER.set(resolve)
    try:
        server = SimpleNamespace(_state_root=lambda: tmp_path, _persist_store_state=lambda: None)
        result = asyncio.run(qq.receive_midi(server, PersonalMessage('qq', 'a', 'b', 'c', '[文件]'), row, 'c', 'opaque', name))
    finally:
        MEDIA_RESOLVER.reset(token)
    assert result == (expected != 'ordinary')
    if expected == 'ok':
        assert row['piano_midi']['title'] == 'demo'
        assert '发来了一份 MIDI' in row['piano_file_notice']
        assert calls == [data]
    else:
        assert not calls and 'piano_midi' not in row
        if expected == 'error': assert '重发' in row['piano_file_notice']


@pytest.mark.parametrize('seconds', [30, 156, 480])
@pytest.mark.parametrize('failure', [None, TimeoutError, 'rejected'])
def test_mp3_delivery_ack_memory_and_no_uncertain_replay(tmp_path, monkeypatch, failure, seconds):
    from runtime.personal_chat.qq import QQFileRejected
    from runtime.personal_chat.backend import persist_chat
    calls = []; sent = []; notices = []
    meta = dict(META, seconds=seconds, section='完整一曲' if seconds > 120 else '开头')
    monkeypatch.setattr('runtime.media.music_reply._media_duration_seconds', lambda *a, **k: seconds)
    row = dict(letter_id='turn', channel='qq', piano_input={'source': 'original', 'title': '夜曲', 'mood': '平静', 'seconds': 30})
    async def caps(env): return CAPS
    async def persist(server): pass
    class API:
        def __init__(self, *args): pass
        async def generate(self, kind, data, path, **kwargs):
            calls.append((kind, data)); path.write_bytes(b'mp3')
            assert kwargs['receipt_path'].name == 'request.json'
            kwargs['validate'](path)
            return dict(task_id='same-task', piano_meta=meta)
    async def ack(api, task_id, output): calls.append('ack')
    monkeypatch.setattr(piano, 'capabilities', caps)
    monkeypatch.setattr('runtime.remote_generation.RemoteGeneration', API)
    monkeypatch.setattr('runtime.personal_chat.backend.persist_chat', persist)
    monkeypatch.setattr('runtime.gpu_cleanup.acknowledge_result', ack)
    async def send(text): notices.append(text); return 'text-ack'
    async def file(path, name):
        sent.append(name)
        if failure == 'rejected': raise QQFileRejected('rejected')
        if failure: raise failure()
        return 'file-ack'
    send.file = file
    server = SimpleNamespace(_state_root=lambda: tmp_path, _persist_store_state=lambda: None,
                             store=SimpleNamespace(personal_chats=[row]))
    async def scenario():
        if failure:
            with pytest.raises((TimeoutError, QQFileRejected)): await qq.deliver(server, row, send)
            assert list(piano.memory_rows(row)) == [] and 'ack' not in calls
            if failure is TimeoutError:
                await qq.deliver(server, row, send)
                assert len(sent) == 1
        else:
            await qq.deliver(server, row, send)
            await qq.deliver(server, row, send)
            assert len(sent) == 1 and sent[0].endswith('.mp3')
            assert ('一整首我自己写的《夜曲》' if seconds > 120 else '《夜曲》的开头') in notices[0]
            assert list(piano.memory_rows(row))[0]['reply_text'] == notices[0]
            assert calls[-1] == 'ack'
    asyncio.run(scenario())


def test_piano_decision_is_frozen_and_optional():
    from runtime.reply.companion_decision import FrozenCompanionTurn, FrozenCompanionDecision, JevDecisionPort
    from tests.persona.test_companion_decision import input_args, envelope
    experience = {**CAPS, 'pieces': ['雨夜', '温柔的小曲']}
    turn = FrozenCompanionTurn.create(**input_args(), piano_experience=experience)
    response = {**envelope(), 'piano_request': dict(source='original', piece_hint='夜曲', seconds=30)}
    record = FrozenCompanionDecision.from_response(turn, response).record()
    assert FrozenCompanionDecision.from_record(FrozenCompanionTurn.from_record(record), record).record() == record
    assert JevDecisionPort('http://127.0.0.1:1/v1/companion/decide')._packet(turn)['piano_experience'] == experience
    disabled = FrozenCompanionTurn.create(**input_args())
    assert 'piano_request' not in FrozenCompanionDecision.from_response(disabled, response).record()


@pytest.mark.parametrize('count', [0, 1, 200, 201])
def test_backend_advertises_saved_titles_in_upload_order(tmp_path, monkeypatch, count):
    from runtime import piano_collection
    from runtime.personal_chat.presentation import CURRENT
    from tests.http.test_personal_chat_backend import (
        test_backend_generate_uses_real_pipeline_persona_memory_and_world as run_backend,
        ReplyPipeline,
    )
    owner = SimpleNamespace(_os=SimpleNamespace(environ={
        'OLIVIA_GPU_API_URL': 'https://media.test', 'OLIVIA_GPU_API_KEY': 'synthetic'}),
        _local_data_root=lambda: tmp_path)
    titles = ['琴' * 60 if i == 0 else f'曲子 {i}' for i in range(count)]
    with piano_collection.database(piano_collection.root(owner)) as db:
        # Insert in reverse order to exercise the persisted upload ordering.
        for i in reversed(range(count)):
            db.execute('INSERT INTO pieces VALUES(?,?,?,?,NULL)',
                       (f'{i:064x}', titles[i], f'2026-10-12T04:{i // 60:02}:{i % 60:02}+00:00', 'qq'))
    saved = piano_collection.pieces(owner)
    monkeypatch.setattr(piano_collection, 'pieces', lambda server: saved)
    monkeypatch.setattr(piano_collection, 'context', lambda server: '')
    async def caps(env): return CAPS
    monkeypatch.setattr(piano, 'capabilities', caps)
    original_run = ReplyPipeline.run
    observed = []
    async def inspect(self, request, context):
        assert '[对方传给我的 MIDI 曲目' not in request.content
        experience = CURRENT.get()['piano_experience']
        assert experience['pieces'] == titles[:200]
        assert all(1 <= len(title) <= 60 for title in experience['pieces'])
        assert len(CURRENT.get()['piano_pieces']) == count
        observed.append(experience)
        return await original_run(self, request, context)
    monkeypatch.setattr(ReplyPipeline, 'run', inspect)
    run_backend(tmp_path, monkeypatch, None, 'plain', ('qq', '今天练琴了吗？', '练了一会儿。'))
    assert len(observed) == 1


def test_piano_background_deduplicates_by_owner():
    from runtime.personal_chat.service import PersonalChatService
    async def scenario():
        calls = []
        async def perform(row, send): calls.append(row['letter_id']); await asyncio.sleep(0)
        async def commit(row): pass
        binding = PersonalMessage('qq', 'bot', 'owner', '', '').binding_id
        row = dict(letter_id='one', channel='qq', binding_id=binding, delivery_status='DELIVERED', piano_input={'source': 'original'})
        rows = [row, {**row, 'letter_id': 'unknown', 'piano_delivery_status': 'UNKNOWN'},
                {**row, 'letter_id': 'foreign', 'binding_id': 'other'}]
        service = PersonalChatService(rows, lambda: None, None, commit, {'qq': ('bot', 'owner')}, piano=perform)
        async def send(text): return 'ack'
        send.file = lambda *args: None
        service.resume_speech('qq', send); service.resume_speech('qq', send)
        await asyncio.gather(*service.speech_tasks.values())
        assert calls == ['one']
    asyncio.run(scenario())


@pytest.mark.parametrize('notice', [None, 'UNKNOWN', 'DELIVERED'])
def test_delivered_file_recovers_memory_and_only_unsent_notice(tmp_path, monkeypatch, notice):
    row = dict(letter_id='turn', channel='qq', piano_delivery_status='DELIVERED', piano_meta=META,
               piano_task_id='existing', piano_file=str(tmp_path / 'recording.mp3'))
    if notice: row['piano_notice_status'] = notice
    sent = []; acks = []
    async def persist(server): pass
    async def ack(*args): acks.append(True)
    monkeypatch.setattr('runtime.personal_chat.backend.persist_chat', persist)
    monkeypatch.setattr('runtime.gpu_cleanup.acknowledge_result', ack)
    async def send(text): sent.append(text); return 'confirmed'
    async def file(*args): raise AssertionError('file already accepted')
    send.file = file
    server = SimpleNamespace(store=SimpleNamespace(personal_chats=[row]), _persist_store_state=lambda: None)
    async def scenario():
        await qq.deliver(server, row, send)
        await qq.deliver(server, row, send)
    asyncio.run(scenario())
    assert row['piano_completed_at']
    assert len(sent) == (1 if notice is None else 0)
    assert len(acks) == 1
