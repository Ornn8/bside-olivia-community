"""Cloud controls on the existing, session-protected local settings API."""
import asyncio
import contextlib
import os
from aiohttp import web
from original_client_setup_api import _authorize, _body, _headers, SESSION_HEADER
from runtime.cloud_service import CloudError


def mount_cloud_api(app, service, setup, gpu_settings=None):
    from runtime.remote_generation import RemoteGeneration
    from runtime.gpu_settings import GPUSettings
    gpu_settings = gpu_settings or GPUSettings(service.path.parent)
    gpu_settings.load()
    from runtime.music_settings import MusicSettings
    music_settings = MusicSettings(service.path.parent)
    music_settings.load()
    # Only explicit selection shares Olivia credentials; custom servers never receive them.
    async def generation(request):
        origin = _authorize(request, confirm=True)
        setup.require_session(request.headers.get(SESSION_HEADER, ''))
        body = await _body(request)
        operation = body.pop('action', None)
        try:
            if operation == 'settings_use_olivia':
                if body:
                    raise CloudError('GPU_REQUEST_INVALID', 400)
                getter = app.get('olivia_relay_stored_key')
                key = getter() if getter else None
                if not key:
                    raise CloudError('RELAY_NOT_CONFIGURED', 400)
                url = 'https://175.24.191.6'
                await gpu_settings.test(url, key)
                return web.json_response(gpu_settings.save('remote', url, key), headers=_headers(origin))
            if operation == 'billing_quote':
                if set(body) not in ({'video'}, {'video', 'original'}) or type(body['video']) is not bool or type(body.get('original', False)) is not bool:
                    raise CloudError('GPU_REQUEST_INVALID', 400)
                if gpu_settings.environment.get('OLIVIA_GPU_ROUTE') != 'remote':
                    return web.json_response({'status': 'OK', 'paid': False}, headers=_headers(origin))
                api = RemoteGeneration(gpu_settings.environment.get('OLIVIA_GPU_API_URL', ''), gpu_settings.environment.get('OLIVIA_GPU_API_KEY', ''))
                caps = await api.request('capabilities', {})
                paid = caps.get('billing_enabled') is True
                result = {'status': 'OK', 'paid': paid}
                if paid:
                    # An already-open pre-update frontend only sends video and
                    # would otherwise confirm a 100-cent cap for a 300-cent song.
                    if 'original' not in body:
                        raise CloudError('GPU_CLIENT_UPDATE_REQUIRED', 409)
                    original = body.get('original', False)
                    amount = caps.get('reservation_cents', {}).get('video' if body['video'] else 'original' if original else 'audio')
                    if type(amount) is not int or amount != (500 if body['video'] else 300 if original else 100):
                        raise CloudError('GPU_RESPONSE_INVALID', 502)
                    account = await api.request('billing_account', {})
                    if account['balance_cents'] < amount:
                        raise CloudError('GPU_INSUFFICIENT_BALANCE', 402)
                    result.update(max_charge_cents=amount, balance_cents=account['balance_cents'])
                return web.json_response(result, headers=_headers(origin))
            if operation == 'music_settings_status' and not body:
                result = music_settings.status()
                result['original_music_provider'] = 'legacy'
                if gpu_settings.environment.get('OLIVIA_GPU_ROUTE') == 'remote':
                    api = RemoteGeneration(gpu_settings.environment.get('OLIVIA_GPU_API_URL', ''), gpu_settings.environment.get('OLIVIA_GPU_API_KEY', ''))
                    try:
                        caps = await api.request('capabilities', {})
                    except CloudError:
                        caps = {}
                        result['original_music_provider'] = 'unavailable'
                    if caps.get('original_music_provider') == 'suno_v6':
                        result['original_music_provider'] = 'suno_v6'
                return web.json_response(result, headers=_headers(origin))
            if operation == 'music_settings_save' and set(body) == {'options'}:
                return web.json_response(music_settings.save(body['options']), headers=_headers(origin))
            if operation == 'settings_status' and not body:
                return web.json_response(gpu_settings.status(), headers=_headers(origin))
            if operation == 'settings_save' and set(body) == {'route', 'url', 'key'}:
                return web.json_response(gpu_settings.save(**body), headers=_headers(origin))
            if operation == 'settings_clear' and not body:
                return web.json_response(gpu_settings.clear(), headers=_headers(origin))
            if operation == 'settings_test' and set(body) == {'url', 'key'}:
                return web.json_response(await gpu_settings.test(**body), headers=_headers(origin))
            if operation == 'settings_claim' and set(body) == {'url'} and isinstance(body['url'], str):
                return web.json_response(await gpu_settings.claim(body['url']), headers=_headers(origin))
            remote = RemoteGeneration(os.environ.get('OLIVIA_GPU_API_URL', ''), os.environ.get('OLIVIA_GPU_API_KEY', ''))
            result = await remote.request(operation, body)
            if operation in ('billing_prices', 'billing_account', 'billing_statement'):
                result = dict(result, status='OK')
            return web.json_response(result, headers=_headers(origin))
        except CloudError as exc:
            payload = {'error_code': exc.code}
            if exc.code == 'GPU_CLIENT_UPDATE_REQUIRED':
                payload['message'] = '请重启客户端以更新原创收费确认。草稿已保留。'
            return web.json_response(payload, status=exc.status, headers=_headers(origin))
    async def options(request):
        origin = _authorize(request, confirm=False)
        return web.Response(status=204, headers=_headers(origin, preflight=True))

    async def action(request):
        origin = _authorize(request, confirm=True)
        setup.require_session(request.headers.get(SESSION_HEADER, ''))
        body = await _body(request)
        operation = body.pop('action', None)
        try:
            if operation == 'status' and not body:
                result = service.status()
                result['report_preview'] = service.preview()
                return web.json_response(result, headers=_headers(origin))
            if service.lock.locked():
                raise CloudError('CLOUD_SYNC_BUSY', 409)
            async with service.lock:
                if operation == 'login' and set(body) == {'url', 'username', 'password', 'consent'}:
                    await service.login(**body)
                elif operation == 'logout' and not body:
                    await service.logout()
                elif operation == 'report' and set(body) == {'consent', 'codes'}:
                    await service.enqueue(**body)
                elif operation == 'delete_reports' and body == {'confirm': True}:
                    await service.delete_cloud_data()
                elif operation == 'delete_account' and set(body) == {'confirm', 'password'} and body['confirm'] is True and isinstance(body['password'], str):
                    await service.delete_cloud_data(password=body['password'])
                elif operation == 'status' and not body:
                    pass
                else:
                    raise CloudError('CLOUD_ACTION_INVALID', 400)
                result = service.status()
                result['report_preview'] = service.preview()
            return web.json_response(result, headers=_headers(origin))
        except CloudError as exc:
            return web.json_response({'status': 'FAILED', 'error_code': exc.code},
                                     status=exc.status, headers=_headers(origin))

    async def lifecycle(app):
        await service.load()
        task = asyncio.create_task(service.run(), name='optional-cloud-service')
        async def cleanup_results():
            from runtime.gpu_cleanup import retry_pending
            while True:
                remote = RemoteGeneration(os.environ.get('OLIVIA_GPU_API_URL', ''), os.environ.get('OLIVIA_GPU_API_KEY', ''))
                await retry_pending(service.path.parent / 'media', remote)
                await asyncio.sleep(30)
        cleanup = asyncio.create_task(cleanup_results(), name='gpu-result-cleanup')
        yield
        task.cancel()
        cleanup.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup

    app.router.add_post('/toy/cloud/action', action)
    app.router.add_post('/toy/generation/action', generation)
    app.router.add_options('/toy/generation/action', options)
    app.router.add_options('/toy/cloud/action', options)
    app.cleanup_ctx.append(lifecycle)
