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
            assert (await client.post(route,json={'action':'settings_clear'},headers=headers)).status==200
            assert env=={'OLIVIA_GPU_ROUTE':'local'}
    asyncio.run(scenario())
