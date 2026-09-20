import asyncio
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from runtime.remote_generation import RemoteGeneration
from runtime.cloud_service import CloudError


def test_billing_reads_server_amounts_and_rejects_invalid_money():
    async def scenario():
        account = {'mode': 'simulation', 'currency': 'CNY', 'opening_cents': 10000,
                   'balance_cents': 9985, 'spent_cents': 15,
                   'charges': [{'task_id': 'task-1', 'amount_cents': 15,
                                'pricing_version': 'test-v1', 'stages': []}]}
        prices = {'billing_enabled': False, 'pricing': None}
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
