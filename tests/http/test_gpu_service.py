import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import threading
import types
import hmac
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer
import gpu_service.server as gpu_server
from gpu_service.server import build_app
from runtime.remote_generation import RemoteGeneration
from runtime.cloud_service import CloudError


def config(scene):
    return {'public_url': 'http://127.0.0.1:18880', 'signing_key': 's' * 32,
            'tokens': {'alice': 'a' * 32, 'bob': 'b' * 32}, 'gpus': [],
            'profiles': {'tts': {}, 'lipsync': {}},
            'shared_assets': {'scene-v1': {'path': str(scene), 'sha256': hashlib.sha256(scene.read_bytes()).hexdigest()}}}


def test_isolation_idempotency_cancel_and_shared_assets(tmp_path):
    scene = tmp_path / 'scene.mp4'; scene.write_bytes(b'shared fixture')
    async def scenario():
        async with TestClient(TestServer(build_app(config(scene), tmp_path / 'state'))) as client:
            headers = {'Authorization': 'Bearer ' + 'a' * 32, 'Idempotency-Key': 'same-request'}
            body = {'request_id': 'same-request', 'kind': 'tts', 'input': {'text': 'synthetic'}}
            assert (await client.post('/v1/tasks', json=body)).status == 401
            first = await client.post('/v1/tasks', json=body, headers=headers)
            assert first.status == 202
            task = await first.json()
            again = await client.post('/v1/tasks', json=body, headers=headers)
            assert (await again.json())['task_id'] == task['task_id']
            body['input']['text'] = 'changed'
            assert (await client.post('/v1/tasks', json=body, headers=headers)).status == 409
            bob = {'Authorization': 'Bearer ' + 'b' * 32}
            assert (await client.get('/v1/tasks/' + task['task_id'], headers=bob)).status == 404
            cancelled = await client.post('/v1/tasks/' + task['task_id'] + '/cancel', headers=headers)
            assert (await cancelled.json())['status'] == 'cancelled'
            api = RemoteGeneration(str(client.make_url('/')), 'a' * 32)
            caps = await api.request('capabilities', {})
            assert caps['shared_assets'][0]['asset_id'] == 'scene-v1'
            assert 'path' not in caps['shared_assets'][0]
            wav = tmp_path / 'voice.wav'; wav.write_bytes(b'private fixture')
            aid = await api.upload(wav)
            body = {'request_id': 'other-request', 'kind': 'lipsync', 'input': {'audio_asset': aid, 'scene_asset': 'scene-v1'}}
            bob['Idempotency-Key'] = 'other-request'
            assert (await client.post('/v1/tasks', json=body, headers=bob)).status == 404
            headers['Idempotency-Key'] = 'other-request'
            assert (await client.post('/v1/tasks', json=body, headers=headers)).status == 202
            body['input']['scene_asset'] = '../../secret'
            assert (await client.post('/v1/tasks', json=body, headers=headers)).status == 400
    asyncio.run(scenario())


def test_changed_shared_reference_prevents_startup(tmp_path):
    scene = tmp_path / 'scene.mp4'; scene.write_bytes(b'v1')
    settings = config(scene); scene.write_bytes(b'v2')
    async def scenario():
        with pytest.raises(ValueError, match='Shared asset hash mismatch'):
            async with TestServer(build_app(settings, tmp_path / 'state')): pass
    asyncio.run(scenario())


def test_interrupted_task_fails_on_restart(tmp_path):
    scene = tmp_path / 'scene.mp4'; scene.write_bytes(b'fixture')
    root = tmp_path / 'state'; build_app(config(scene), root)
    with sqlite3.connect(root / 'tasks.sqlite3') as db:
        db.execute("INSERT INTO tasks(id,owner,request_id,digest,kind,payload,status,created,elapsed,stage) VALUES(?,?,?,?,?,?,?,?,?,?)", ('interrupted', 'alice', 'request-1', 'hash', 'tts', '{}', 'running', 0, None, 'gpu_running'))
    async def scenario():
        async with TestClient(TestServer(build_app(config(scene), root))) as client:
            response = await client.get('/v1/tasks/interrupted', headers={'Authorization': 'Bearer ' + 'a' * 32})
            assert (await response.json())['status'] == 'failed'
            assert (await client.get('/v1/results/interrupted?expires=99999999999&signature=forged')).status == 403
    asyncio.run(scenario())


def test_shared_scene_is_not_uploaded_when_missing(tmp_path):
    scene = tmp_path / 'scene.mp4'; scene.write_bytes(b'fixture')
    other = tmp_path / 'other.mp4'; other.write_bytes(b'unregistered')
    async def scenario():
        async with TestServer(build_app(config(scene), tmp_path / 'state')) as server:
            api = RemoteGeneration(str(server.make_url('/')), 'a' * 32)
            with pytest.raises(CloudError, match='GPU_SHARED_SCENE_MISSING'):
                await api.generate('lipsync', {}, tmp_path / 'output.mp4', assets={'scene_asset': other})
        with sqlite3.connect(tmp_path / 'state/tasks.sqlite3') as db:
            assert db.execute('SELECT count(*) FROM assets').fetchone()[0] == 0
    asyncio.run(scenario())


def test_named_shared_scene_needs_no_local_file(tmp_path):
    scene = tmp_path / 'scene.mp4'; scene.write_bytes(b'fixture')
    async def scenario():
        async with TestServer(build_app(config(scene), tmp_path / 'state')) as server:
            api = RemoteGeneration(str(server.make_url('/')), 'a' * 32)
            with pytest.raises(CloudError, match='GPU_SHARED_SCENE_MISSING'):
                await api.generate('lipsync', {'scene_asset': 'not-deployed'}, tmp_path / 'out.mp4')
            with pytest.raises(CloudError, match='GPU_TASK_TIMEOUT'):
                await api.generate('lipsync', {'scene_asset': 'scene-v1', 'audio_asset': 'scene-v1'}, tmp_path / 'out.mp4', timeout=-1)
            with sqlite3.connect(tmp_path / 'state/tasks.sqlite3') as db:
                assert db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
                assert db.execute('SELECT count(*) FROM assets').fetchone()[0] == 0
    asyncio.run(scenario())


def test_signed_output_and_expiration(tmp_path):
    scene = tmp_path / 'scene.mp4'; scene.write_bytes(b'fixture')
    root = tmp_path / 'state'; settings = config(scene)
    build_app(settings, root)
    job = root / 'jobs/completed'; job.mkdir()
    (job / 'output.bin').write_bytes(b'result')
    with sqlite3.connect(root / 'tasks.sqlite3') as db:
        db.execute("INSERT INTO tasks(id,owner,request_id,digest,kind,payload,status,created,elapsed,stage) VALUES(?,?,?,?,?,?,?,?,?,?)", ('completed', 'alice', 'request-1', 'hash', 'tts', '{}', 'succeeded', 0, 1, 'completed'))
    async def scenario():
        async with TestClient(TestServer(build_app(settings, root))) as client:
            expires = int(time.time()) + 100
            def url(expiry):
                signature = hmac.new(settings['signing_key'].encode(), f'completed:{expiry}'.encode(), hashlib.sha256).hexdigest()
                return f'/v1/results/completed?expires={expiry}&signature={signature}'
            response = await client.get(url(expires))
            assert response.status == 200 and await response.read() == b'result'
            assert (await client.get(url(1))).status == 403
            assert (await client.get(url(expires) + 'bad')).status == 403
    asyncio.run(scenario())


def test_upload_quota_and_timeout_cancellation(tmp_path):
    scene = tmp_path / 'scene.mp4'; scene.write_bytes(b'fixture')
    settings = config(scene); settings['upload_quota_bytes'] = 5
    async def scenario():
        async with TestServer(build_app(settings, tmp_path / 'state')) as server:
            api = RemoteGeneration(str(server.make_url('/')), 'a' * 32)
            data = tmp_path / 'audio.wav'; data.write_bytes(b'1234')
            await api.upload(data)
            with pytest.raises(CloudError, match='GPU_UPLOAD_FAILED'): await api.upload(data)
            with pytest.raises(CloudError, match='GPU_TASK_TIMEOUT'):
                await api.generate('tts', {'text': 'synthetic'}, tmp_path / 'result.wav', timeout=-1)
            with sqlite3.connect(tmp_path / 'state/tasks.sqlite3') as db:
                assert db.execute('SELECT status FROM tasks').fetchone()[0] == 'cancelled'
                assert db.execute('SELECT count(*) FROM assets').fetchone()[0] == 1
    asyncio.run(scenario())


def test_sla_priority_and_health(tmp_path):
    scene = tmp_path / 'scene.mp4'; scene.write_bytes(b'fixture')
    settings = config(scene)
    settings['profiles']['original'] = {}
    async def scenario():
        async with TestClient(TestServer(build_app(settings, tmp_path / 'state'))) as client:
            auth = {'Authorization': 'Bearer ' + 'a' * 32}
            heavy_id = 'heavy-0001'
            voice_id = 'voice-0001'
            heavy_headers = dict(auth, **{'Idempotency-Key': heavy_id})
            voice_headers = dict(auth, **{'Idempotency-Key': voice_id})
            heavy = await client.post('/v1/tasks', headers=heavy_headers, json={
                'request_id': heavy_id, 'kind': 'original', 'input': {'lyrics': 'synthetic'}})
            assert heavy.status == 202
            await asyncio.sleep(0.01)
            voice = await client.post('/v1/tasks', headers=voice_headers, json={
                'request_id': voice_id, 'kind': 'tts', 'input': {'text': 'synthetic'}})
            assert voice.status == 202
            heavy_task = await heavy.json()
            voice_task = await voice.json()
            heavy_state = await (await client.get('/v1/tasks/' + heavy_task['task_id'], headers=auth)).json()
            voice_state = await (await client.get('/v1/tasks/' + voice_task['task_id'], headers=auth)).json()
            assert voice_state['queue_position'] == 1
            assert heavy_state['queue_position'] == 2
            assert voice_state['queue_sla_seconds'] == 600
            assert heavy_state['queue_sla_seconds'] == 1800
            health = await (await client.get('/v1/health', headers=auth)).json()
            assert health['queue_depth'] == 2
            assert health['workers_configured'] == 0
            assert health['workers_alive'] == 0
    asyncio.run(scenario())


def test_gpu_is_released_before_result_upload_finishes(tmp_path, monkeypatch):
    first_upload_started = threading.Event()
    release_first_upload = threading.Event()
    published = []

    class FakeStore:
        def __init__(self, config):
            pass

        def publish(self, task_id, kind, job):
            published.append(task_id)
            if len(published) == 1:
                first_upload_started.set()
                assert release_first_upload.wait(5)
            (Path(job) / 'r2-result.json').write_text('{}', encoding='utf-8')

        def url(self, job):
            return 'https://results.example.invalid/result'

    module = types.ModuleType('gpu_service.result_store')
    module.ResultStore = FakeStore
    monkeypatch.setitem(sys.modules, 'gpu_service.result_store', module)

    class FakeProcess:
        next_pid = 1000

        def __init__(self):
            self.pid = FakeProcess.next_pid
            FakeProcess.next_pid += 1
            self.returncode = None

        async def wait(self):
            await asyncio.sleep(0)
            self.returncode = 0
            return 0

    async def fake_subprocess(*args, **kwargs):
        request_path = Path(args[-1])
        spec = json.loads(request_path.read_text(encoding='utf-8'))
        Path(spec['output']).write_bytes(b'synthetic-output')
        return FakeProcess()

    monkeypatch.setattr(gpu_server.asyncio, 'create_subprocess_exec', fake_subprocess)

    scene = tmp_path / 'scene.mp4'; scene.write_bytes(b'fixture')
    settings = config(scene)
    settings['gpus'] = ['0']
    settings['result_store'] = {'fake': True}
    settings['result_upload_workers'] = 1

    async def scenario():
        async with TestClient(TestServer(build_app(settings, tmp_path / 'state'))) as client:
            auth = {'Authorization': 'Bearer ' + 'a' * 32}
            first_id = 'voice-1001'
            second_id = 'voice-1002'
            first = await (await client.post('/v1/tasks', headers=dict(auth, **{'Idempotency-Key': first_id}), json={
                'request_id': first_id, 'kind': 'tts', 'input': {'text': 'first'}})).json()
            deadline = time.monotonic() + 5
            while not first_upload_started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert first_upload_started.is_set()
            second = await (await client.post('/v1/tasks', headers=dict(auth, **{'Idempotency-Key': second_id}), json={
                'request_id': second_id, 'kind': 'tts', 'input': {'text': 'second'}})).json()
            deadline = time.monotonic() + 5
            second_state = None
            while time.monotonic() < deadline:
                second_state = await (await client.get('/v1/tasks/' + second['task_id'], headers=auth)).json()
                if second_state['stage'] == 'uploading':
                    break
                await asyncio.sleep(0.01)
            assert second_state['stage'] == 'uploading'
            assert second_state['status'] == 'succeeded'
            health = await (await client.get('/v1/health', headers=auth)).json()
            assert health['gpu_running'] == 0
            assert health['uploads_running'] == 1
            assert health['uploads_queued'] >= 1
            release_first_upload.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                states = [
                    await (await client.get('/v1/tasks/' + task['task_id'], headers=auth)).json()
                    for task in (first, second)
                ]
                if all(item['stage'] == 'completed' for item in states):
                    break
                await asyncio.sleep(0.01)
            assert all(item['status'] == 'succeeded' for item in states)
            assert all(item['stage'] == 'completed' for item in states)
            assert len(published) == 2
    asyncio.run(scenario())
