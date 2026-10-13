import asyncio
import json
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from runtime.remote_generation import RemoteGeneration
from runtime.cloud_service import CloudError


def test_long_speech_download_defers_ack_until_qq_delivery(tmp_path,monkeypatch):
    calls=[]
    async def request(self,action,data):
        calls.append(action)
        if action=='capabilities':return dict(kinds=['tts'],shared_assets=[],result_acknowledgement=True)
        assert action=='submit'
        return dict(task_id='synthetic',status='succeeded')
    async def download(self,task,output,**kwargs):
        output.write_bytes(b'synthetic-mp3');return task
    monkeypatch.setattr(RemoteGeneration,'request',request)
    monkeypatch.setattr(RemoteGeneration,'_download',download)
    asyncio.run(RemoteGeneration().generate('tts',dict(text='frozen-script',speech_mode='asmr'),tmp_path/'audio.mp3'))
    assert calls==['capabilities','submit']


@pytest.mark.parametrize('stage,resumes,resumed_id',[
    ('speech_postprocess_failed',True,'same-order'),('speech_postprocess_failed',True,'unexpected-order'),
    ('failed',False,None),('cancelled',False,None)])
def test_long_speech_retries_only_cpu_export_with_original_submission(tmp_path,monkeypatch,stage,resumes,resumed_id):
    from runtime.remote_generation import CloudError
    submissions=[]
    failed=dict(task_id='same-order',status='cancelled' if stage=='cancelled' else 'failed',stage=stage)
    async def request(self,action,data):
        if action=='capabilities':return dict(kinds=['tts'],shared_assets=[],result_acknowledgement=True)
        assert action=='submit';submissions.append(data)
        return dict(task_id=resumed_id,status='succeeded') if len(submissions)>1 else failed
    async def status(self,task_id):
        assert task_id=='same-order';return failed
    async def download(self,task,output,**kwargs):
        if task['status']!='succeeded':raise CloudError('GPU_TASK_FAILED',503)
        output.write_bytes(b'synthetic-mp3');return task
    monkeypatch.setattr(RemoteGeneration,'request',request)
    monkeypatch.setattr(RemoteGeneration,'_status',status)
    monkeypatch.setattr(RemoteGeneration,'_download',download)
    async def scenario():
        api=RemoteGeneration();data=dict(text='frozen-script',speech_mode='asmr')
        output=tmp_path/'audio.mp3';receipt=tmp_path/'receipt.json'
        with pytest.raises(CloudError):await api.generate('tts',data,output,receipt_path=receipt)
        if resumes and resumed_id!='same-order':
            with pytest.raises(CloudError,match='GPU_TASK_INVALID'):
                await api.generate('tts',data,output,receipt_path=receipt)
            import json
            assert json.loads(receipt.read_text())['task_id']=='same-order'
            assert not output.exists()
        elif resumes:
            result=await api.generate('tts',data,output,receipt_path=receipt)
            assert result['task_id']=='same-order'
        else:
            with pytest.raises(CloudError):await api.generate('tts',data,output,receipt_path=receipt)
    asyncio.run(scenario())
    assert len(submissions)==(2 if resumes else 1)
    if resumes:assert submissions[0]==submissions[1]


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


@pytest.mark.parametrize('kind', ['cover', 'cover_video'])
@pytest.mark.parametrize('sings', [True, False])
def test_yue2_cover_options_only_reach_a_service_that_prices_them(kind, sings):
    """Her own performance needs a service that knows it; an older one gets the voice-swap request."""
    options = {'audio_cover_strength': .6, 'cover_noise_strength': .25, 'engine': 'yue2', 'arrangement': '古风，二胡'}
    seen = {}
    async def scenario():
        async def prices(request):
            multipliers = {'cover': '13', **({'cover_yue2': '6', 'cover_yue2_video': '4'} if sings else {})}
            return web.json_response({'billing_enabled': True, 'pricing': {'mode': 'money', 'currency': 'CNY',
                'version': 'test-v1', 'base_micros_per_hour': 3600000, 'utilization': '1', 'multipliers': multipliers}})
        async def submit(request):
            seen.update(cap=request.headers['X-Olivia-Max-Charge-Cents'], body=await request.json())
            return web.json_response({'task_id': 'synthetic', 'status': 'queued'})
        app = web.Application(); app.router.add_get('/v1/billing/prices', prices); app.router.add_post('/v1/tasks', submit)
        async with TestServer(app) as server:
            await RemoteGeneration(str(server.make_url('/')), 'synthetic').request(
                'submit', {'request_id': 'test-request', 'kind': kind, 'input': {'cover_options': options}})
    asyncio.run(scenario())
    sent = seen['body']['input']['cover_options']
    if sings:
        assert sent == options
        assert seen['cap'] == ('300' if kind == 'cover' else '500')
    else:
        assert sent == {'audio_cover_strength': .6, 'cover_noise_strength': .25}
        assert seen['cap'] == ('100' if kind == 'cover' else '500')


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
                assert len(polled)==(24 if failure=='persistent' else 1)
                assert not output.exists()
            else:
                await api.generate('tts',{'text':'synthetic'},output)
                assert output.read_bytes()==b'synthetic-output'
                assert len(polled)==2
            assert len(submitted)==1
            assert delays == ([1, 2, 4, 8, 16] + [30] * 19 if failure == 'persistent'
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
            if request.path == '/v1/tasks/same-order':
                return web.json_response({'task_id': 'same-order', 'status': 'succeeded',
                    'outputs': [{'url': str(server.make_url('/result'))}]})
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
            assert len(submissions) == 1
            assert uploads == [b'synthetic']
            assert (tmp_path / 'out.zip').read_bytes() == b'materials'
            assert 'synthetic' not in receipt.read_text()
    asyncio.run(scenario())


def test_service_switch_recovers_after_old_retry_window(tmp_path, monkeypatch):
    calls, delays = [], []
    async def sleep(seconds): delays.append(seconds)
    async def request(self, action, data):
        calls.append(action)
        if action == 'capabilities': return {'kinds': ['cover_video'], 'shared_assets': []}
        if action == 'submit': return {'task_id': 'existing', 'status': 'running'}
        assert action == 'status'
        if calls.count('status') <= 6: raise CloudError('GPU_REQUEST_FAILED', 502)
        return {'task_id': 'existing', 'status': 'succeeded'}
    async def download(self, task, output, **kwargs): return task
    monkeypatch.setattr(RemoteGeneration, 'request', request)
    monkeypatch.setattr(RemoteGeneration, '_download', download)
    monkeypatch.setattr('runtime.remote_generation.asyncio.sleep', sleep)
    result = asyncio.run(RemoteGeneration().generate('cover_video', {}, tmp_path / 'out'))
    assert result['status'] == 'succeeded'
    assert calls.count('submit') == 1
    assert sum(delays) > 60


@pytest.mark.parametrize('restart_status', ['succeeded', 'missing', 'auth'])
def test_restart_resumes_receipt_without_submission(tmp_path, monkeypatch, restart_status):
    calls = []
    restarting = False
    async def request(self, action, data):
        calls.append(action)
        if action == 'capabilities': return {'kinds': ['cover_video'], 'shared_assets': []}
        if action == 'submit': return {'task_id': 'existing', 'status': 'running'}
        assert action == 'status' and data == {'task_id': 'existing'}
        if not restarting: raise asyncio.CancelledError()
        if restart_status == 'missing': raise CloudError('GPU_REQUEST_FAILED', 404)
        if restart_status == 'auth': raise CloudError('GPU_AUTH_FAILED', 502)
        return {'task_id': 'existing', 'status': 'succeeded'}
    async def sleep(seconds): pass
    async def download(self, task, output, **kwargs): return task
    monkeypatch.setattr(RemoteGeneration, 'request', request)
    monkeypatch.setattr(RemoteGeneration, '_download', download)
    monkeypatch.setattr('runtime.remote_generation.asyncio.sleep', sleep)
    receipt = tmp_path / 'receipt.json'
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(RemoteGeneration().generate('cover_video', {}, tmp_path / 'out', receipt_path=receipt))
    assert json.loads(receipt.read_text())['task_id'] == 'existing'
    restarting = True
    if restart_status == 'succeeded':
        assert asyncio.run(RemoteGeneration().generate('cover_video', {}, tmp_path / 'out', receipt_path=receipt))['status'] == 'succeeded'
    else:
        with pytest.raises(CloudError):
            asyncio.run(RemoteGeneration().generate('cover_video', {}, tmp_path / 'out', receipt_path=receipt))
    assert calls.count('submit') == 1
    assert receipt.exists()


def test_legacy_receipt_reuses_idempotency_key_after_lost_submit_response(tmp_path, monkeypatch):
    submissions = []
    async def request(self, action, data):
        if action == 'capabilities': return {'kinds': ['cover_video'], 'shared_assets': []}
        assert action == 'submit'
        submissions.append(dict(data))
        if len(submissions) == 1: raise CloudError('GPU_CONNECTION_FAILED')
        return {'task_id': 'existing', 'status': 'succeeded'}
    async def download(self, task, output, **kwargs): return task
    monkeypatch.setattr(RemoteGeneration, 'request', request)
    monkeypatch.setattr(RemoteGeneration, '_download', download)
    receipt = tmp_path / 'receipt.json'
    with pytest.raises(CloudError):
        asyncio.run(RemoteGeneration().generate('cover_video', {}, tmp_path / 'out', receipt_path=receipt))
    assert 'task_id' not in json.loads(receipt.read_text())
    asyncio.run(RemoteGeneration().generate('cover_video', {}, tmp_path / 'out', receipt_path=receipt))
    assert submissions[0] == submissions[1]
    assert json.loads(receipt.read_text())['task_id'] == 'existing'


def test_stalled_result_download_is_fetched_again_without_resubmitting(monkeypatch, tmp_path):
    import asyncio
    from runtime import remote_generation as module
    from runtime.cloud_service import CloudError
    api = module.RemoteGeneration('https://gpu.example', 'olivia-test-key')
    calls, statuses = [], []
    async def once(task, output, *, validate=None):
        calls.append(task['outputs'][0]['url'])
        if len(calls) < 3:
            raise CloudError('GPU_CONNECTION_TIMEOUT')
        return {'task_id': task['task_id']}
    async def status(task_id):
        statuses.append(task_id)
        return {'task_id': task_id, 'status': 'succeeded', 'outputs': [{'url': f'https://r2.example/fresh{len(statuses)}'}]}
    async def no_sleep(_):
        return None
    monkeypatch.setattr(api, '_download_once', once)
    monkeypatch.setattr(api, '_status', status)
    monkeypatch.setattr(module.asyncio, 'sleep', no_sleep)
    task = {'task_id': 't1', 'status': 'succeeded', 'outputs': [{'url': 'https://r2.example/first'}]}
    assert asyncio.run(api._download(task, tmp_path / 'out.mp4')) == {'task_id': 't1'}
    # Retries read a fresh signed link; nothing is submitted or charged again.
    assert calls == ['https://r2.example/first', 'https://r2.example/fresh1', 'https://r2.example/fresh2']
    with __import__('pytest').raises(CloudError):
        async def broken(task, output, *, validate=None):
            raise CloudError('GPU_OUTPUT_EMPTY', 502)
        monkeypatch.setattr(api, '_download_once', broken)
        asyncio.run(api._download(task, tmp_path / 'out.mp4'))
