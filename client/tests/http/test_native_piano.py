import asyncio
import json
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web
import pytest

from runtime import native_piano as native


CAPS = {'piano': {'enabled': True, 'price_ranges_cents': {'midi':[7,13],'original':[11,29]}, 'max_seconds': 120, 'max_midi_bytes': 64, 'original': True}}
META = {'title': 'Evening', 'composer': None, 'key': None, 'tempo_bpm': 80, 'seconds': 60, 'section': '开头'}


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setenv('OLIVIA_GPU_API_URL','http://127.0.0.1:1')
    monkeypatch.setenv('OLIVIA_GPU_API_KEY','synthetic')
    class API:
        calls = []
        state = 'queued'
        capabilities = CAPS
        async def request(self, action, data):
            self.calls.append((action, data))
            if action == 'capabilities': return self.capabilities
            if action == 'cancel': self.state = 'cancelled'
            return {'task_id': 'remote_one', 'status': self.state, 'piano_meta': META}
        async def upload(self, path):
            assert path.read_bytes().startswith(b'MThd')
            import hashlib
            return hashlib.sha256(path.read_bytes()).hexdigest()
        async def download_task(self, task_id, path, *, validate):
            assert callable(validate)
            path.write_bytes(b'ID3mock')
    api = API()
    server = SimpleNamespace(_local_data_root=lambda: tmp_path, _os=__import__('os'),
                             PORT=8765, store=SimpleNamespace(midi_jobs=[]), _persist_store_state=lambda: None,
                             ok=lambda data: {'code': 0, 'data': data}, err=lambda code, msg, data: {'code': code, 'message': msg, 'data': data},
                             CORS_HEADERS=lambda request: {})
    monkeypatch.setattr(native, 'api', lambda _: api)
    monkeypatch.setattr(native, 'monitor', lambda *_: None)
    native._locks.clear()
    return server, api


async def uploaded(server, name='evening.midi'):
    ticket = (await native.route(server, '/toy/genObjectUploadUrl', {'filename': name}, {}))['data']
    _, path = native.upload_paths(server, ticket['key'])
    path.write_bytes(b'MThd' + b'\0' * 10)
    return ticket['key']


def test_native_submit_poll_audio_memory_and_delete(adapter):
    async def scenario():
        server, api = adapter
        token = await uploaded(server)
        request = {'midiUrl': token, 'seconds': 60}
        result = await native.route(server, '/toy/midi/generate', request, {})
        job = result['data']; assert job['state'] == 1
        again = await native.route(server, '/toy/midi/generate', request, {})
        assert again['data']['jobId'] == job['jobId']
        assert len([a for a, _ in api.calls if a == 'submit']) == 1
        api.state = 'succeeded'
        result = await native.route(server, '/toy/midi/getGenerateResult', {}, {'jobId': job['jobId']})
        assert result['data']['state'] == 3
        assert result['data']['info']['audioUrl'].endswith('.mp3')
        assert result['data']['info']['videoUrls'] == []
        assert server.store.midi_jobs[0]['piano_completed_at']
        assert server.store.midi_jobs[0]['piano_meta'] == META
        result = await native.route(server, '/toy/midi/listJobs', {}, {'pageSize': '10'})
        assert result['data']['list'][0]['createdAt'] > 0
        assert result['data']['hasMore'] is False
        await native.route(server, '/toy/midi/deleteJob', {'jobId': job['jobId']}, {})
        assert server.store.midi_jobs[0]['hidden'] is True
        assert (await native.route(server, '/toy/midi/listJobs', {}, {}))['data']['list'] == []
    asyncio.run(scenario())


def test_native_disabled_and_untrusted_upload(adapter):
    async def scenario():
        server, api = adapter
        for path in ('http://elsewhere/file.mid', '../secret.mid', 'a'*32):
            result = await native.route(server, '/toy/midi/generate', {'midiUrl': path}, {})
            assert result['code'] == 400
        api.capabilities = {'kinds': []}
        assert (await native.route(server, '/toy/midi/capabilities', {}, {}))['data'] == {'enabled': False}
        assert (await native.route(server, '/toy/genObjectUploadUrl', {'filename': 'x.mid'}, {}))['code'] == 503
        assert not any(a == 'submit' for a, _ in api.calls)
    asyncio.run(scenario())


@pytest.mark.parametrize('filename,payload,expected', [('ok.mid', b'MThd123', 204), ('ok.midi', b'bad!', 400), ('large.mid', b'MThd'+b'x'*100, 400)])
def test_native_http_upload(adapter, filename, payload, expected):
    async def scenario():
        server, _ = adapter
        ticket = (await native.route(server, '/toy/genObjectUploadUrl', {'filename': filename}, {}))['data']
        app = web.Application()
        async def upload(request):
            return await native.upload_http(server, request)
        app.router.add_put('/toy/midi/upload/{token}', upload)
        async with TestClient(TestServer(app)) as client:
            response = await client.put('/toy/midi/upload/' + ticket['key'], data=payload)
            assert response.status == expected
    asyncio.run(scenario())


def test_native_cancel_and_batch_camelcase(adapter):
    async def scenario():
        server, api = adapter
        token = await uploaded(server)
        result = await native.route(server, '/toy/midi/generate', {'midiUrl': token}, {})
        job_id = result['data']['jobId']
        result = await native.route(server, '/toy/midi/batchGetResult', {}, {'jobIds[]': [job_id]})
        assert result['data']['results'][0]['jobId'] == job_id
        assert 'generatedToday' in result['data']
        result = await native.route(server, '/toy/midi/cancelGenerate', {'jobId': job_id}, {})
        assert result['data']['state'] == 4
        assert ('cancel', {'task_id': 'remote_one'}) in api.calls
    asyncio.run(scenario())


def test_native_ambiguous_submit_retains_frozen_request(adapter, monkeypatch):
    from runtime.cloud_service import CloudError
    async def scenario():
        server, api = adapter
        token = await uploaded(server)
        original = api.request
        submissions = []
        async def uncertain(action, data):
            if action == 'submit':
                submissions.append(data)
                if len(submissions) == 1:
                    raise CloudError('GPU_CONNECTION_TIMEOUT')
            return await original(action, data)
        monkeypatch.setattr(api, 'request', uncertain)
        assert (await native.route(server, '/toy/midi/generate', {'midiUrl': token}, {}))['code'] == 503
        changed = await native.route(server, '/toy/midi/generate', {'midiUrl': token, 'seconds': 90}, {})
        assert changed['code'] == 409
        result = await native.route(server, '/toy/midi/generate', {'midiUrl': token}, {})
        assert result['code'] == 0
        assert submissions[0] == submissions[1]
    asyncio.run(scenario())


def test_native_real_http_route_chain(adapter, monkeypatch):
    import local_server
    async def scenario():
        server, api = adapter
        monkeypatch.setattr(local_server, '_local_data_root', server._local_data_root)
        monkeypatch.setattr(local_server.store, 'midi_jobs', [])
        monkeypatch.setattr(local_server, '_persist_store_state', lambda: None)
        app = web.Application()
        app.router.add_route('*', '/{tail:.*}', local_server.handler)
        async with TestClient(TestServer(app)) as client:
            monkeypatch.setattr(local_server, 'PORT', client.port)
            response = await client.post('/toy/genObjectUploadUrl', json={'filename': 'evening.mid'})
            assert response.status == 200
            ticket = (await response.json())['data']
            response = await client.put('/toy/midi/upload/' + ticket['key'], data=b'MThd123')
            assert response.status == 204
            response = await client.post('/toy/midi/generate', json={'midiUrl': ticket['key']})
            assert response.status == 200
            identity = (await response.json())['data']['jobId']
            api.state = 'succeeded'
            response = await client.get('/toy/midi/getGenerateResult', params={'jobId': identity})
            assert (await response.json())['data']['state'] == 3
            response = await client.get('/toy/midi/audio/' + identity + '.mp3?download=1')
            assert response.status == 200
            assert response.headers['Content-Type'] == 'audio/mpeg'
            assert 'attachment' in response.headers['Content-Disposition']
    asyncio.run(scenario())


def test_cancel_racing_completion_downloads_and_remembers(adapter, monkeypatch):
    async def scenario():
        server, api = adapter
        token = await uploaded(server)
        job = (await native.route(server, '/toy/midi/generate', {'midiUrl': token}, {}))['data']
        original = api.request
        async def completed_before_cancel(action, data):
            if action == 'cancel':
                api.state = 'succeeded'
                return {'task_id': 'remote_one', 'status': 'succeeded'}
            return await original(action, data)
        monkeypatch.setattr(api, 'request', completed_before_cancel)
        result = await native.route(server, '/toy/midi/cancelGenerate', {'jobId': job['jobId']}, {})
        assert result['data']['state'] == 3
        assert server.store.midi_jobs[0]['piano_completed_at']
        assert (native.directory(server) / (job['jobId'] + '.mp3')).is_file()
    asyncio.run(scenario())


@pytest.mark.parametrize('limit', [150, 240, 480])
def test_native_full_original_download_and_projection(adapter, monkeypatch, limit):
    async def scenario():
        server, api = adapter
        api.capabilities = {'piano': dict(CAPS['piano'], original_max_seconds=limit, original_full_length=True)}
        meta = dict(META, seconds=156, section='完整一曲', composer=None, key=None)
        job = dict(job_id='a'*32, task_id='full', piano_account=native.account(server),
                   state=2, status='running', input={'source':'original'})
        server.store.midi_jobs.append(job)
        original = api.request
        async def request(action, data):
            if action=='status': return dict(task_id='full', status='succeeded', piano_meta=meta)
            return await original(action,data)
        async def download(task_id, path, *, validate):
            path.write_bytes(b'ID3synthetic')
            validate(path)
        monkeypatch.setattr(api,'request',request)
        monkeypatch.setattr(api,'download_task',download)
        monkeypatch.setattr('runtime.media.music_reply._media_duration_seconds',lambda *a,**k:156)
        result=await native.route(server,'/toy/midi/getGenerateResult',{},dict(jobId=job['job_id']))
        assert job['piano_original_max_seconds'] == limit
        if limit < 156:
            assert result['code'] == 400 and 'piano_completed_at' not in job
            return
        assert result['data']['state']==3
        assert '一整首' in result['data']['description']
        assert result['data']['info']['audioUrl'].endswith('.mp3')
        assert job['piano_completed_at']
    asyncio.run(scenario())
