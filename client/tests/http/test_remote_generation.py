import asyncio
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from runtime.remote_generation import RemoteGeneration
from runtime.cloud_service import CloudError


@pytest.mark.parametrize('kind', ['video', 'lipsync', 'original_video', 'cover_video'])
def test_slow_video_is_not_cancelled_by_elapsed_client_budget(tmp_path, monkeypatch, kind):
    calls = []
    async def request(self, action, data):
        calls.append(action)
        if action == 'capabilities': return {'kinds': [kind], 'shared_assets': []}
        assert action in {'submit', 'status'}
        return {'task_id': 'synthetic', 'status': 'running' if action == 'submit' else 'succeeded'}
    async def download(self, task, output, **kwargs): return task
    async def sleep(seconds): pass
    monkeypatch.setattr(RemoteGeneration, 'request', request)
    monkeypatch.setattr(RemoteGeneration, '_download', download)
    monkeypatch.setattr('runtime.remote_generation.asyncio.sleep', sleep)
    result = asyncio.run(RemoteGeneration().generate(kind, {}, tmp_path / 'output', timeout=-1))
    assert result['status'] == 'succeeded'
    assert calls == ['capabilities', 'submit', 'status']


@pytest.mark.parametrize('kind,cap', [('tts',100),('cover',100),('original',300),('original_video',500),('image',121)])
def test_submit_preserves_kind_specific_charge_consent(kind, cap):
    async def scenario():
        async def handler(request):
            assert request.headers['X-Olivia-Max-Charge-Cents']==str(cap)
            return web.json_response({'task_id':'synthetic','status':'queued'})
        app=web.Application();app.router.add_post('/v1/tasks',handler)
        async with TestServer(app) as server:
            await RemoteGeneration(str(server.make_url('/')),'synthetic').request('submit',{'request_id':'test-request','kind':kind,'input':{}})
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [502, 503, 504, 404, 401, 'persistent'])
def test_status_transient_failure_does_not_resubmit(tmp_path, monkeypatch, failure):
    async def scenario():
        submitted, polled, delays = [], [], []
        async def sleep(seconds): delays.append(seconds)
        monkeypatch.setattr('runtime.remote_generation.asyncio.sleep', sleep)
        async def handler(request):
            if request.path == '/v1/capabilities':
                return web.json_response({'kinds': ['tts'], 'shared_assets': []})
            if request.path == '/v1/tasks':
                submitted.append(await request.json())
                return web.json_response({'task_id': 'existing', 'status': 'running'})
            if request.path == '/v1/tasks/existing':
                polled.append(1)
                if len(polled) == 1 or failure == 'persistent':
                    return web.Response(status=503 if failure == 'persistent' else failure)
                return web.json_response({'task_id': 'existing', 'status': 'succeeded',
                    'outputs': [{'url': str(server.make_url('/result'))}]})
            return web.Response(body=b'synthetic-output')
        app=web.Application();app.router.add_route('*','/{tail:.*}',handler)
        async with TestServer(app) as server:
            api=RemoteGeneration(str(server.make_url('/')),'synthetic')
            output=tmp_path/'reply.wav'
            if failure in (401,404,'persistent'):
                with pytest.raises(CloudError): await api.generate('tts',{'text':'synthetic'},output)
                assert len(polled)==(5 if failure=='persistent' else 1)
                assert not output.exists()
            else:
                await api.generate('tts',{'text':'synthetic'},output)
                assert output.read_bytes()==b'synthetic-output'
                assert len(polled)==2
            assert len(submitted)==1
            assert delays == ([1, 2, 4, 8, 16] if failure == 'persistent'
                              else [1] if failure in (401, 404) else [1, 2])
    asyncio.run(scenario())


@pytest.mark.parametrize('code', ['GPU_CONNECT_FAILED', 'GPU_CONNECTION_TIMEOUT',
    'GPU_CONNECTION_FAILED', 'GPU_TLS_FAILED', 'GPU_RESPONSE_INVALID'])
def test_status_transport_retry_boundary(monkeypatch, code):
    calls = []
    async def request(self, action, data):
        calls.append((action, data))
        if len(calls) == 1: raise CloudError(code)
        return {'task_id': 'existing', 'status': 'succeeded'}
    async def sleep(seconds): pass
    monkeypatch.setattr(RemoteGeneration, 'request', request)
    monkeypatch.setattr('runtime.remote_generation.asyncio.sleep', sleep)
    if code in ('GPU_TLS_FAILED', 'GPU_RESPONSE_INVALID'):
        with pytest.raises(CloudError): asyncio.run(RemoteGeneration()._status('existing'))
        assert len(calls) == 1
    else:
        assert asyncio.run(RemoteGeneration()._status('existing'))['status'] == 'succeeded'
        assert calls == [('status', {'task_id': 'existing'})] * 2


def test_missing_shared_spoken_scene_fails_before_submission(tmp_path, monkeypatch):
    async def request(self, action, data):
        assert action == 'capabilities'
        return {'kinds': ['original_video'], 'shared_assets': [
            {'asset_id': 'performance', 'sha256': '0' * 64}]}
    monkeypatch.setattr(RemoteGeneration, 'request', request)
    with pytest.raises(CloudError) as error:
        asyncio.run(RemoteGeneration().generate('original_video',
            {'scene_asset': 'performance', 'spoken_scene_asset': 'missing'}, tmp_path / 'result.zip'))
    assert error.value.code == 'GPU_SHARED_SCENE_MISSING'


@pytest.mark.parametrize('mode', ['simulation', 'money'])
def test_billing_reads_server_amounts_and_rejects_invalid_money(mode):
    async def scenario():
        account = {'mode': mode, 'currency': 'CNY', 'opening_cents': 10000,
                   'balance_cents': 9985, 'spent_cents': 15,
                   'charges': [{'task_id': 'task-1', 'amount_cents': 15,
                                'pricing_version': 'test-v1', 'stages': []}]}
        prices = {'billing_enabled': mode == 'money', 'pricing': {'mode': mode, 'currency': 'CNY',
            'version': 'test-v1', 'base_micros_per_hour': 3600000, 'utilization': '1', 'multipliers': {'lipsync': '1.25'}}}
        async def handler(request):
            assert request.method == 'GET'
            assert request.headers['Authorization'] == 'Bearer synthetic'
            assert not request.headers.get('Cookie')
            return web.json_response(prices if request.path.endswith('/prices') else account)
        app = web.Application(); app.router.add_route('*', '/{tail:.*}', handler)
        async with TestServer(app) as server:
            api = RemoteGeneration(str(server.make_url('/')), 'synthetic')
            assert await api.request('billing_account', {}) == account
            assert await api.request('billing_prices', {}) == prices
            for bad in (True, None, '9985', 1.5):
                account['balance_cents'] = bad
                with pytest.raises(CloudError) as error:
                    await api.request('billing_account', {})
                assert error.value.code == 'GPU_RESPONSE_INVALID'
            with pytest.raises(CloudError):
                await api.request('billing_account', {'owner': 'other'})
    asyncio.run(scenario())


def test_task_contract_and_no_cookie_forwarding():
    async def scenario():
        calls=[]
        async def handler(request):
            assert request.headers['Authorization']=='Bearer synthetic'
            assert not request.headers.get('Cookie')
            calls.append((request.method,request.path))
            if request.path=='/v1/tasks':
                assert request.headers['Idempotency-Key']=='test-request'
                assert (await request.json())['kind']=='tts'
            return web.json_response({'task_id':'task-1','status':'cancelled' if request.path.endswith('/cancel') else 'queued'})
        app=web.Application(); app.router.add_route('*','/{tail:.*}',handler)
        async with TestServer(app) as server:
            api=RemoteGeneration(str(server.make_url('/')),'synthetic')
            await api.request('submit',{'request_id':'test-request','kind':'tts','input':{'text':'test'}})
            await api.request('status',{'task_id':'task-1'})
            assert (await api.request('cancel',{'task_id':'task-1'}))['status']=='cancelled'
            with pytest.raises(CloudError): await api.request('status',{'task_id':'../other'})
            assert len(calls)==3
        with pytest.raises(CloudError): await RemoteGeneration().request('submit',{})
    asyncio.run(scenario())


def test_redirect_and_invalid_results_fail_closed():
    async def scenario():
        mode='redirect'
        async def handler(request):
            if mode=='redirect': return web.Response(status=302,headers={'Location':'https://example.com'})
            return web.json_response({'task_id':'wrong','status':'succeeded','outputs':[{'url':'file:///secret'}]})
        app=web.Application(); app.router.add_route('*','/{tail:.*}',handler)
        async with TestServer(app) as server:
            api=RemoteGeneration(str(server.make_url('/')),'synthetic')
            with pytest.raises(CloudError): await api.request('status',{'task_id':'expected'})
            mode='bad'
            with pytest.raises(CloudError): await api.request('status',{'task_id':'expected'})
    asyncio.run(scenario())


def test_existing_result_download_never_submits_again_and_preserves_old_file(tmp_path):
    async def scenario():
        calls=[]
        async def handler(request):
            calls.append((request.method,request.path))
            assert request.method=='GET'
            if request.path=='/v1/tasks/existing':
                assert request.headers['Authorization']=='Bearer synthetic'
                return web.json_response({'task_id':'existing','status':'succeeded',
                    'outputs':[{'url':str(server.make_url('/result'))}]})
            assert not request.headers.get('Authorization')
            return web.Response(body=b'new-result')
        app=web.Application();app.router.add_route('*','/{tail:.*}',handler)
        async with TestServer(app) as server:
            api=RemoteGeneration(str(server.make_url('/')),'synthetic')
            output=tmp_path/'result.wav';output.write_bytes(b'old-result')
            def invalid(path): raise ValueError('Invalid media')
            with pytest.raises(ValueError):await api.download_task('existing',output,validate=invalid)
            assert output.read_bytes()==b'old-result'
            await api.download_task('existing',output)
            assert output.read_bytes()==b'new-result'
            assert all(method=='GET' for method,_ in calls)
    asyncio.run(scenario())


def test_material_download_retry_keeps_same_order_and_upload(tmp_path):
    async def scenario():
        submissions, uploads = [], []
        fail_download = True
        async def handler(request):
            if request.path == '/v1/capabilities':
                return web.json_response({'kinds': ['cover_video'], 'shared_assets': []})
            if request.path == '/v1/assets':
                uploads.append(await request.read())
                return web.json_response({'asset_id': 'source-one'}, status=201)
            if request.path == '/v1/tasks':
                submissions.append(await request.json())
                return web.json_response({'task_id': 'same-order', 'status': 'succeeded',
                    'outputs': [{'url': str(server.make_url('/result'))}]})
            return web.Response(status=503 if fail_download else 200, body=b'materials')
        app = web.Application(); app.router.add_route('*', '/{tail:.*}', handler)
        source = tmp_path / 'input.wav'; source.write_bytes(b'synthetic')
        receipt = tmp_path / 'order.private.json'
        async with TestServer(app) as server:
            api = RemoteGeneration(str(server.make_url('/')), 'synthetic')
            with pytest.raises(CloudError):
                await api.generate('cover_video', {}, tmp_path / 'out.zip', assets={'source_asset': source}, receipt_path=receipt)
            fail_download = False
            await api.generate('cover_video', {}, tmp_path / 'out.zip', assets={'source_asset': source}, receipt_path=receipt)
            assert submissions[0] == submissions[1]
            assert uploads == [b'synthetic']
            assert (tmp_path / 'out.zip').read_bytes() == b'materials'
            assert 'synthetic' not in receipt.read_text()
    asyncio.run(scenario())
