import asyncio
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from runtime.cloud_service import CloudService, CloudError, endpoint
from original_client_cloud_api import mount_cloud_api
from original_client_setup_api import LLMSetupService, mount_original_client_setup_api


def test_endpoint_blocks_cleartext_and_credential_urls():
    for url in ('http://public.example', 'https://x@y', 'https://host/path', 'file:///tmp', 'https://host:99999', 'https://host?token=x'):
        with pytest.raises(CloudError): endpoint(url)
    assert endpoint('http://127.0.0.1:18765/') == 'http://127.0.0.1:18765'
    assert endpoint('https://service.example') == 'https://service.example'


def test_privacy_deletion_clears_queue_only_after_success(tmp_path):
    async def scenario():
        service = CloudService(tmp_path, protect=lambda v:v)
        service.state = {'queue':[{'pending':True}], 'receipts':[{'id':'old'}]}
        async def fail(*args, **kwargs):
            raise CloudError('CLOUD_CONNECTION_FAILED')
        service.call = fail
        with pytest.raises(CloudError): await service.delete_cloud_data()
        assert service.state['queue']
        calls = []
        async def succeed(path, payload=None):
            calls.append((path, payload))
            return {'ok':True}
        service.call = succeed
        await service.delete_cloud_data()
        assert not service.state['queue'] and not service.state['receipts']
        await service.delete_cloud_data(password='test-only-password')
        assert calls[-1] == ('/api/v1/account/delete', {'confirm':True, 'password':'test-only-password'})
        assert service.state == {}
    asyncio.run(scenario())


def test_login_restart_report_retry_and_revocation(tmp_path):
    async def scenario():
        received, unavailable, revoked = [], False, False
        async def handler(request):
            nonlocal unavailable
            if request.path.endswith('/session'):
                response = web.json_response({'authenticated': False}); response.set_cookie('csrftoken', 'synthetic-csrf'); return response
            if request.path.endswith('/login'):
                assert (await request.post())['password'] == 'synthetic-password'
                assert request.headers['X-CSRFToken'] == 'synthetic-csrf'
                response = web.json_response({'username': 'alice'}); response.set_cookie('sessionid', 'synthetic-session'); return response
            if revoked: return web.json_response({}, status=401)
            assert request.cookies.get('sessionid') == 'synthetic-session'
            if request.path.endswith('/me'): return web.json_response({'balance_cents': 0})
            if request.path.endswith('/presence'):
                assert set(await request.json()) == {'app_version', 'os_family'}
                return web.json_response({'ok': True})
            if request.path.endswith('/publications'): return web.json_response({'items': []})
            if request.path.endswith('/diagnostics'):
                received.append(await request.json())
                if unavailable: return web.json_response({}, status=503)
                return web.json_response({'id': 'receipt-1'})
            return web.json_response({'ok': True})
        app = web.Application(); app.router.add_route('*', '/{tail:.*}', handler)
        async with TestServer(app) as server:
            kwargs = dict(version='1.2.7', errors=lambda: [{'error_code':'VOICE_FAILED', 'body':'PRIVATE'}], protect=lambda v:'cipher:'+v, unprotect=lambda v:v[7:])
            client = CloudService(tmp_path, **kwargs)
            await client.login(str(server.make_url('/')), 'alice', 'synthetic-password', True)
            assert 'synthetic-password' not in client.path.read_text()
            restored = CloudService(tmp_path, **kwargs); await restored.load()
            assert restored.status()['signed_in']
            with pytest.raises(CloudError): await restored.enqueue(False, ['VOICE_FAILED'])
            with pytest.raises(CloudError): await restored.enqueue(True, ['FAKE_CODE'])
            await restored.enqueue(True, ['VOICE_FAILED'])
            unavailable = True; await restored.sync()
            assert len(restored.state['queue']) == 1
            unavailable = False; await restored.sync()
            assert received[0]['report_id'] == received[1]['report_id']
            assert set(received[0]) == {'consent','report_id','component','error_code','app_version','os_family'}
            assert restored.status()['pending_reports'] == 0
            assert restored.status()['receipts'][0]['id'] == 'receipt-1'
            revoked = True; await restored.sync()
            assert restored.last_error == 'CLOUD_AUTH_REQUIRED'
            assert not restored.status()['signed_in']
            await restored.logout(); assert not restored.path.exists()
    asyncio.run(scenario())


def test_report_queue_bounds_and_offline_startup(tmp_path):
    async def scenario():
        service = CloudService(tmp_path, protect=lambda v:v, errors=lambda:[{'error_code':'TEST_FAILURE'}])
        service.state = {'url':'http://127.0.0.1:1', 'cookies':{'sessionid':'synthetic'}, 'queue':[]}
        await service.enqueue(True, ['TEST_FAILURE'])
        await service.sync()
        assert service.last_error == 'CLOUD_CONNECTION_FAILED'
        assert len(service.state['queue']) == 1
        service.path.write_text('corrupted', encoding='utf-8')
        await service.load()
        assert service.last_error == 'CLOUD_LOCAL_SESSION_UNAVAILABLE'
        assert not service.status()['signed_in']
    asyncio.run(scenario())


def test_local_cloud_actions_require_origin_and_session(tmp_path):
    async def scenario():
        app = web.Application()
        setup = LLMSetupService(tmp_path)
        mount_original_client_setup_api(app, setup, trusted_origins=('https://client.example',))
        service = CloudService(tmp_path)
        mount_cloud_api(app, service, setup)
        async with TestClient(TestServer(app)) as client:
            route = '/toy/cloud/action'
            assert (await client.post(route, json={'action':'status'})).status == 403
            headers = {'Origin':'https://client.example', 'X-Olivia-Setup-Action':'confirmed'}
            assert (await client.post(route, json={'action':'status'}, headers=headers)).status == 403
            status = await (await client.get('/toy/setup/status', headers={'Origin':'https://client.example'})).json()
            headers['X-Olivia-Setup-Session'] = status['session_token']
            response = await client.post(route, json={'action':'status'}, headers=headers)
            assert response.status == 200
            assert (await response.json())['signed_in'] is False
            headers['Origin'] = 'https://attacker.example'
            assert (await client.post(route, json={'action':'status'}, headers=headers)).status == 403
    asyncio.run(scenario())


def test_optional_cloud_failure_does_not_block_original_fallback(tmp_path):
    from original_client_server import create_original_client_server_runtime
    async def scenario():
        async def original(request):
            return web.json_response({'local': 'available'})
        setup = LLMSetupService(tmp_path)
        service = CloudService(tmp_path, unprotect=lambda value: (_ for _ in ()).throw(ValueError()))
        service.path.write_text('corrupt-test-session', encoding='utf-8')
        runtime = create_original_client_server_runtime(original, setup_service=setup, cloud_service=service)
        async with TestClient(TestServer(runtime.app)) as client:
            result = await client.get('/toy/local-health')
            assert result.status == 200 and (await result.json()) == {'local':'available'}
            assert service.last_error == 'CLOUD_LOCAL_SESSION_UNAVAILABLE'
    asyncio.run(scenario())
