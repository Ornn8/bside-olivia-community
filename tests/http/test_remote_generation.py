import asyncio
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from runtime.remote_generation import RemoteGeneration
from runtime.cloud_service import CloudError


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
