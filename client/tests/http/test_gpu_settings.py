import asyncio
import json
import pytest
from runtime.cloud_service import CloudError
from runtime.gpu_settings import GPUSettings


def settings(root, env):
    return GPUSettings(root, environment=env, protect=lambda s:'encrypted:'+s[::-1],
                       unprotect=lambda s:s.removeprefix('encrypted:')[::-1])


def test_saved_connection_restores_without_exposing_key(tmp_path):
    env={}
    service=settings(tmp_path, env)
    service.save('remote', 'https://gpu.example', 'synthetic-secret')
    assert env['OLIVIA_GPU_ROUTE']=='remote'
    assert 'synthetic-secret' not in service.path.read_text()
    assert 'synthetic-secret' not in json.dumps(service.status())
    assert service.status()['status'] == 'OK'
    restored=settings(tmp_path, {})
    restored.load()
    assert restored.environment['OLIVIA_GPU_API_KEY']=='synthetic-secret'
    restored.save('local', 'https://gpu.example', '')
    assert restored.environment['OLIVIA_GPU_ROUTE']=='local'
    restored.clear()
    assert not restored.path.exists()
    assert 'OLIVIA_GPU_API_KEY' not in restored.environment


def test_changing_host_needs_new_key_and_failed_save_preserves_route(tmp_path):
    service=settings(tmp_path, {})
    service.save('remote', 'https://first.example', 'first-secret')
    with pytest.raises(CloudError):
        service.save('remote', 'https://second.example', '')
    assert service.environment['OLIVIA_GPU_API_URL']=='https://first.example'
    service.save('remote', 'https://second.example', 'second-secret')
    assert service.environment['OLIVIA_GPU_API_KEY']=='second-secret'
    service.protect=lambda s: (_ for _ in ()).throw(OSError())
    with pytest.raises(CloudError):
        service.save('local', 'https://second.example', '')
    assert service.environment['OLIVIA_GPU_ROUTE']=='remote'


def test_invalid_key_and_corrupt_file_fail_closed(tmp_path):
    service=settings(tmp_path, {})
    for key in ('bad\r\nheader', '白字', 'x'*4097):
        with pytest.raises(CloudError):
            service.save('remote', 'https://gpu.example', key)
    service.path.write_text('invalid')
    service.load()
    assert service.environment['OLIVIA_GPU_ROUTE']=='local'
    assert service.status()['error_code']=='GPU_SETTINGS_UNAVAILABLE'


@pytest.mark.parametrize('failure,code', [(FileNotFoundError('private path'), 'GPU_ENCRYPTION_TOOL_MISSING'), (RuntimeError('private key'), 'GPU_ENCRYPTION_FAILED')])
def test_encryption_failure_is_distinct_and_redacted(tmp_path, failure, code):
    service=settings(tmp_path,{})
    service.save('remote','https://old.example','old-key')
    old=service.path.read_bytes()
    def fail(value): raise failure
    service.protect=fail
    with pytest.raises(CloudError) as caught:
        service.save('remote','https://new.example','new-key')
    assert caught.value.code==code
    assert str(caught.value)==code
    assert service.path.read_bytes()==old
    assert service.environment['OLIVIA_GPU_API_KEY']=='old-key'


def test_permission_failure_preserves_saved_connection(tmp_path, monkeypatch):
    from pathlib import Path
    service=settings(tmp_path,{})
    service.save('remote','https://old.example','old-key')
    old=service.path.read_bytes()
    def fail(*args,**kwargs): raise PermissionError('private path')
    monkeypatch.setattr(Path,'write_text',fail)
    with pytest.raises(CloudError) as caught:
        service.save('remote','https://new.example','new-key')
    assert caught.value.code=='GPU_SETTINGS_PERMISSION_DENIED'
    assert service.path.read_bytes()==old
    assert service.environment['OLIVIA_GPU_API_KEY']=='old-key'


def test_network_diagnostics_do_not_expose_transport_details():
    from aiohttp import ClientSSLError, ClientConnectorError, ClientError
    from runtime.remote_generation import connection_error
    for error,code in [(ClientSSLError(None,OSError('private')), 'GPU_TLS_FAILED'),
                       (TimeoutError('private'), 'GPU_CONNECTION_TIMEOUT'),
                       (ClientConnectorError(None,OSError('private')), 'GPU_CONNECT_FAILED'),
                       (ClientError('private'), 'GPU_CONNECTION_FAILED')]:
        assert str(connection_error(error))==code


def test_gpu_tls_context_supplements_empty_os_roots_without_disabling_verification(monkeypatch):
    import ssl
    from runtime.remote_generation import gpu_tls_context
    empty=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    assert empty.cert_store_stats()['x509_ca']==0
    monkeypatch.setattr(ssl,'create_default_context',lambda:empty)
    context=gpu_tls_context()
    assert context is empty
    assert context.cert_store_stats()['x509_ca']>100
    assert context.check_hostname is True
    assert context.verify_mode==ssl.CERT_REQUIRED


def test_connection_test_uses_candidate_without_saving(tmp_path, monkeypatch):
    from runtime.remote_generation import RemoteGeneration
    calls=[]
    async def request(self, action, data):
        calls.append((self.url,self.token,action,data))
        return {'kinds':['tts'], 'shared_assets':[]}
    monkeypatch.setattr(RemoteGeneration,'request',request)
    service=settings(tmp_path,{})
    result = asyncio.run(service.test('https://gpu.example','test-secret'))
    assert result['kinds']==['tts']
    assert result['status'] == 'OK'
    assert not service.path.exists() and not service.environment
    assert calls==[('https://gpu.example','test-secret','capabilities',{})]


def test_settings_api_is_session_protected_and_changes_generation_route(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from original_client_cloud_api import mount_cloud_api
    from original_client_setup_api import LLMSetupService, mount_original_client_setup_api
    from runtime.cloud_service import CloudService
    async def scenario():
        env={}
        gpu=settings(tmp_path,env)
        app=web.Application()
        setup=LLMSetupService(tmp_path)
        mount_original_client_setup_api(app,setup,trusted_origins=('https://client.example',))
        mount_cloud_api(app,CloudService(tmp_path),setup,gpu)
        async with TestClient(TestServer(app)) as client:
            route='/toy/generation/action'
            payload={'action':'settings_save','route':'remote','url':'https://chosen.example','key':'private-test-key'}
            assert (await client.post(route,json=payload)).status==403
            info=await (await client.get('/toy/setup/status',headers={'Origin':'https://client.example'})).json()
            headers={'Origin':'https://client.example','X-Olivia-Setup-Action':'confirmed',
                     'X-Olivia-Setup-Session':info['session_token']}
            result=await client.post(route,json=payload,headers=headers)
            assert result.status==200 and 'private-test-key' not in await result.text()
            assert env['OLIVIA_GPU_API_URL']=='https://chosen.example'
            assert env['OLIVIA_GPU_API_KEY']=='private-test-key'
            music={'action':'music_settings_save','options':{'guidance_scale':7,'use_cot':True,'caption':'synthetic piano'}}
            assert (await client.post(route,json=music)).status==403
            saved=await client.post(route,json=music,headers=headers)
            assert saved.status==200
            assert (await saved.json())['options']['use_cot'] is True
            invalid=await client.post(route,json={'action':'music_settings_save','options':{'seed':-1}},headers=headers)
            assert invalid.status==400
            current=await (await client.post(route,json={'action':'music_settings_status'},headers=headers)).json()
            assert current['options']['caption']=='synthetic piano'
            assert (await client.post(route,json={'action':'settings_clear'},headers=headers)).status==200
            assert env=={'OLIVIA_GPU_ROUTE':'local'}
    asyncio.run(scenario())


def test_paid_quote_and_explicit_shared_key_never_forward_to_custom_host(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from original_client_cloud_api import mount_cloud_api
    from original_client_setup_api import LLMSetupService, mount_original_client_setup_api
    from runtime.cloud_service import CloudService
    calls=[]
    available=[500]
    async def remote(self, action, data):
        calls.append((self.url,self.token,action))
        if action=='capabilities':
            return {'kinds':['tts','video'],'billing_enabled':True,'reservation_cents':{'audio':100,'video':500}}
        return {'balance_cents':available[0]}
    monkeypatch.setattr('runtime.remote_generation.RemoteGeneration.request',remote)
    async def scenario():
        env={};gpu=settings(tmp_path,env);app=web.Application();setup=LLMSetupService(tmp_path)
        mount_original_client_setup_api(app,setup,trusted_origins=('https://client.example',))
        app['olivia_relay_stored_key']=lambda:'olivia-synthetic-shared'
        mount_cloud_api(app,CloudService(tmp_path),setup,gpu)
        async with TestClient(TestServer(app)) as client:
            info=await (await client.get('/toy/setup/status',headers={'Origin':'https://client.example'})).json()
            headers={'Origin':'https://client.example','X-Olivia-Setup-Action':'confirmed','X-Olivia-Setup-Session':info['session_token']}
            path='/toy/generation/action'
            assert (await client.post(path,json={'action':'settings_use_olivia'})).status==403
            local=await (await client.post(path,json={'action':'billing_quote','video':True},headers=headers)).json()
            assert local=={'status':'OK','paid':False} and not calls
            result=await client.post(path,json={'action':'settings_use_olivia'},headers=headers)
            assert result.status==200 and 'olivia-synthetic-shared' not in await result.text()
            assert env['OLIVIA_GPU_API_URL']=='https://175.24.191.6'
            assert env['OLIVIA_GPU_API_KEY']=='olivia-synthetic-shared'
            for video,expected in [(True,500),(False,100)]:
                quote=await (await client.post(path,json={'action':'billing_quote','video':video},headers=headers)).json()
                assert quote['paid'] and quote['max_charge_cents']==expected
            available[0]=99
            denied=await client.post(path,json={'action':'billing_quote','video':False},headers=headers)
            assert denied.status==402
            assert (await denied.json())['error_code']=='GPU_INSUFFICIENT_BALANCE'
            assert (await client.post(path,json={'action':'settings_use_olivia','url':'https://other.example'},headers=headers)).status==400
    asyncio.run(scenario())
    assert all(url=='https://175.24.191.6' and key=='olivia-synthetic-shared' for url,key,_ in calls)
