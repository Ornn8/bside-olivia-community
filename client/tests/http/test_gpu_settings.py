import asyncio
from runtime.gpu_settings import GPU_BASE, GPUSettings


def test_generation_uses_account_key_and_ignores_legacy_custom_connection(tmp_path):
    (tmp_path / 'gpu-connection.dpapi').write_text('legacy-custom-endpoint', encoding='utf-8')
    env = {'OLIVIA_GPU_API_URL': 'https://custom.example', 'OLIVIA_GPU_API_KEY': 'custom-key'}
    GPUSettings(tmp_path, environment=env).load()
    assert env == {}
    gpu = GPUSettings(tmp_path, environment=env, account_key=lambda: 'olivia-synthetic-account')
    status = gpu.status()
    assert env == {'OLIVIA_GPU_ROUTE': 'remote', 'OLIVIA_GPU_API_URL': GPU_BASE,
                   'OLIVIA_GPU_API_KEY': 'olivia-synthetic-account'}
    assert status == {'status': 'OK', 'route': 'remote', 'url': GPU_BASE, 'has_key': True, 'error_code': ''}
    assert 'olivia-synthetic-account' not in repr(status)
    for broken in (lambda: 'sk-not-olivia', lambda: None, lambda: 1 / 0):
        GPUSettings(tmp_path, environment=env, account_key=broken).load()
        assert env == {}


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


def test_settings_api_is_session_protected_and_follows_account_key(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from original_client_cloud_api import mount_cloud_api
    from original_client_setup_api import LLMSetupService, mount_original_client_setup_api
    from runtime.cloud_service import CloudService
    async def scenario():
        env={}
        gpu=GPUSettings(tmp_path,environment=env)
        app=web.Application()
        setup=LLMSetupService(tmp_path)
        mount_original_client_setup_api(app,setup,trusted_origins=('https://client.example',))
        mount_cloud_api(app,CloudService(tmp_path),setup,gpu)
        async with TestClient(TestServer(app)) as client:
            route='/toy/generation/action'
            status={'action':'settings_status'}
            assert (await client.post(route,json=status)).status==403
            info=await (await client.get('/toy/setup/status',headers={'Origin':'https://client.example'})).json()
            headers={'Origin':'https://client.example','X-Olivia-Setup-Action':'confirmed',
                     'X-Olivia-Setup-Session':info['session_token']}
            assert (await (await client.post(route,json=status,headers=headers)).json())['has_key'] is False
            app['olivia_relay_stored_key']=lambda:'olivia-synthetic-account'
            app['olivia_gpu_refresh']()
            result=await client.post(route,json=status,headers=headers)
            assert (await result.json())['has_key'] is True and 'olivia-synthetic-account' not in await result.text()
            assert env['OLIVIA_GPU_API_URL']==GPU_BASE and env['OLIVIA_GPU_API_KEY']=='olivia-synthetic-account'
            music={'action':'music_settings_save','options':{'guidance_scale':7,'use_cot':True,'caption':'synthetic piano'}}
            assert (await client.post(route,json=music)).status==403
            saved=await client.post(route,json=music,headers=headers)
            assert saved.status==200
            assert (await saved.json())['options']['use_cot'] is True
            invalid=await client.post(route,json={'action':'music_settings_save','options':{'seed':-1}},headers=headers)
            assert invalid.status==400
            current=await (await client.post(route,json={'action':'music_settings_status'},headers=headers)).json()
            assert current['options']['caption']=='synthetic piano'
    asyncio.run(scenario())


def test_paid_quote_uses_only_the_olivia_account_key(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from original_client_cloud_api import mount_cloud_api
    from original_client_setup_api import LLMSetupService, mount_original_client_setup_api
    from runtime.cloud_service import CloudService
    calls=[]
    available=[500]
    provider = [None]
    music_limits = [None]
    music_price = [None]
    async def remote(self, action, data):
        calls.append((self.url,self.token,action))
        if action=='capabilities':
            caps = {'kinds':['tts','video'],'billing_enabled':True,'reservation_cents':{'audio':100,'original':300,'video':500}}
            if provider[0] is not None: caps['original_music_provider'] = provider[0]
            if music_limits[0] is not None: caps['original_music_input_limits'] = music_limits[0]
            if music_price[0] is not None: caps['original_music_retail_price'] = music_price[0]
            return caps
        return {'balance_cents':available[0]}
    monkeypatch.setattr('runtime.remote_generation.RemoteGeneration.request',remote)
    async def scenario():
        env={};gpu=GPUSettings(tmp_path,environment=env,account_key=lambda:'olivia-synthetic-shared');app=web.Application();setup=LLMSetupService(tmp_path)
        mount_original_client_setup_api(app,setup,trusted_origins=('https://client.example',))
        app['olivia_relay_stored_key']=lambda:'olivia-synthetic-shared'
        mount_cloud_api(app,CloudService(tmp_path),setup,gpu)
        async with TestClient(TestServer(app)) as client:
            info=await (await client.get('/toy/setup/status',headers={'Origin':'https://client.example'})).json()
            headers={'Origin':'https://client.example','X-Olivia-Setup-Action':'confirmed','X-Olivia-Setup-Session':info['session_token']}
            path='/toy/generation/action'
            assert env['OLIVIA_GPU_API_URL']==GPU_BASE
            assert env['OLIVIA_GPU_API_KEY']=='olivia-synthetic-shared'
            music = await (await client.post(path,json={'action':'music_settings_status'},headers=headers)).json()
            assert music['original_music_provider'] == 'legacy'
            assert 'original_music_input_limits' not in music
            provider[0] = 'suno_v6'
            music = await (await client.post(path,json={'action':'music_settings_status'},headers=headers)).json()
            assert music['original_music_provider'] == 'suno_v6'
            assert 'original_music_input_limits' not in music
            options = {'caption': 'x' * 140, 'duration': 210}
            saved = await client.post(path, json={'action': 'music_settings_save', 'options': options}, headers=headers)
            assert saved.status == 200
            music_limits[0] = {'lyrics': 3000, 'style': 120, 'duration_control': False}
            music = await (await client.post(path, json={'action': 'music_settings_status'}, headers=headers)).json()
            assert music['original_music_input_limits'] == music_limits[0]
            assert music['options']['caption'] == options['caption'] and music['options']['duration'] == 210
            stale=await client.post(path,json={'action':'billing_quote','video':False},headers=headers)
            assert stale.status==409
            assert (await stale.json())['error_code']=='GPU_CLIENT_UPDATE_REQUIRED'
            for video,expected in [(True,500),(False,100)]:
                quote=await (await client.post(path,json={'action':'billing_quote','video':video,'original':False},headers=headers)).json()
                assert quote['paid'] and quote['max_charge_cents']==expected
            for video,expected in [(True,500),(False,300)]:
                quote=await (await client.post(path,json={'action':'billing_quote','video':video,'original':True},headers=headers)).json()
                assert quote['paid'] and quote['max_charge_cents']==expected
            available[0]=299
            denied=await client.post(path,json={'action':'billing_quote','video':False,'original':True},headers=headers)
            assert denied.status==402
            music_price[0] = {'version': 'synthetic-new-song-price', 'min_cents': 120, 'max_cents': 130}
            available[0] = 130
            response = await client.post(path,json={'action':'billing_quote','video':False,'original':True},headers=headers)
            assert response.status == 200
            quote = await response.json()
            assert quote['max_charge_cents'] == 130 and quote['original_music_retail_price'] == music_price[0]
            available[0] = 500
            quote = await (await client.post(path,json={'action':'billing_quote','video':True,'original':True},headers=headers)).json()
            assert quote['max_charge_cents'] == 500 and quote['original_music_retail_price'] == music_price[0]
            music = await (await client.post(path,json={'action':'music_settings_status'},headers=headers)).json()
            assert music['original_music_retail_price'] == music_price[0]
            available[0] = 129
            assert (await client.post(path,json={'action':'billing_quote','video':False,'original':True},headers=headers)).status == 402
            music_price[0] = {**music_price[0], 'max_cents': True}
            assert (await client.post(path,json={'action':'billing_quote','video':False,'original':True},headers=headers)).status == 502
            music_price[0] = None
            available[0] = 299
            still_audio=await client.post(path,json={'action':'billing_quote','video':False,'original':False},headers=headers)
            assert still_audio.status==200
            available[0]=99
            denied=await client.post(path,json={'action':'billing_quote','video':False,'original':False},headers=headers)
            assert denied.status==402
            assert (await denied.json())['error_code']=='GPU_INSUFFICIENT_BALANCE'
    asyncio.run(scenario())
    assert all(url==GPU_BASE and key=='olivia-synthetic-shared' for url,key,_ in calls)
