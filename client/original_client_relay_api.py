"""Session-protected recharge controls for the saved Olivia relay connection."""
from urllib.parse import urlsplit
import asyncio
import secrets
from aiohttp import ClientSession, ClientTimeout, ClientError, ClientSSLError, ClientConnectionError, web
from original_client_setup_api import LLMSetupError, _authorize, _body, _headers, SESSION_HEADER

RELAY_BASE = 'https://175.24.191.6/v1'


async def relay_request(base, key, method, path, payload=None):
    if urlsplit(base).scheme != 'https' or not key.startswith('olivia-'):
        raise LLMSetupError('RELAY_NOT_CONFIGURED', status=400)
    try:
        async with ClientSession(timeout=ClientTimeout(total=20)) as session:
            async with session.request(method, base.rstrip('/')+path, json=payload,
                                       headers={'Authorization':'Bearer '+key}, allow_redirects=False) as response:
                raw = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise ValueError('oversize')
                import json
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError('shape')
                if response.status != 200:
                    code = data.get('error', {}).get('code', 'RELAY_UNAVAILABLE')
                    # Do not expose arbitrary upstream bodies or credentials.
                    known = {'invalid_api_key':'RELAY_AUTH_FAILED',
                             'registration_rate_limited':'RELAY_ORDER_LIMIT',
                             '监听手机离线，暂停新订单。':'RELAY_PHONE_OFFLINE',
                             '创建订单过于频繁，请稍后重试。':'RELAY_ORDER_LIMIT',
                             '当前充值人数较多，暂时没有可用金额，请稍后重试。':'RELAY_SLOTS_FULL'}
                    raise LLMSetupError(known.get(code, 'RELAY_SERVICE_REJECTED'), status=503)
                return data
    except LLMSetupError:
        raise
    except ClientSSLError:
        raise LLMSetupError('RELAY_TLS_FAILED', status=503) from None
    except TimeoutError:
        raise LLMSetupError('RELAY_TIMEOUT', status=503) from None
    except ClientConnectionError:
        raise LLMSetupError('RELAY_CONNECTION_FAILED', status=503) from None
    except (ValueError, TypeError, AttributeError):
        raise LLMSetupError('RELAY_RESPONSE_INVALID', status=503) from None
    except ClientError:
        raise LLMSetupError('RELAY_UNAVAILABLE', status=503) from None


def mount_relay_api(app, setup):
    lock = asyncio.Lock()
    key_path = setup._config_root / 'olivia_relay_key.dpapi'
    pending_path = setup._config_root / 'olivia_relay_registration.pending'

    def stored_key():
        if key_path.is_file():
            return setup._unprotect(key_path.read_text(encoding='utf-8').strip())
        config = setup._config()
        active = setup._active_key_path()
        if config.base_url.rstrip('/') == RELAY_BASE and active:
            return setup._unprotect(active.read_text(encoding='utf-8').strip())
        return None

    def store_key(key):
        key_path.parent.mkdir(parents=True, exist_ok=True)
        staging = key_path.with_suffix('.staging')
        staging.write_text(setup._protect(key), encoding='utf-8')
        staging.replace(key_path)

    async def connect(key):
        payload = {'base_url':RELAY_BASE, 'model':'qwen3.7-flash', 'api_key':key}
        await setup.test(payload)
        setup.save(payload)

    async def action(request):
        origin = _authorize(request, confirm=True)
        setup.require_session(request.headers.get(SESSION_HEADER, ''))
        data = await _body(request)
        if data.get('action') in {'claim', 'connect', 'import_key', 'account', 'export_key'}:
            operation = data['action']
            if set(data) != ({'action','key'} if operation == 'import_key' else {'action'}):
                raise LLMSetupError('LLM_SETUP_FIELDS_INVALID', status=400)
            async with lock:
                key = stored_key()
                if operation == 'account':
                    config = setup._config()
                    active = setup._active_key_path()
                    active_key = setup._unprotect(active.read_text(encoding='utf-8').strip()) if active else None
                    return web.json_response({'configured':bool(key) and not pending_path.exists(), 'registration_pending':bool(key) and pending_path.exists(), 'key_prefix':key[:15] if key and not pending_path.exists() else '',
                        'connected':bool(key) and key == active_key and config.base_url.rstrip('/') == RELAY_BASE and config.model == 'qwen3.7-flash'}, headers=_headers(origin))
                if operation == 'claim':
                    if not key:
                        key = 'olivia-'+secrets.token_urlsafe(32)
                        pending_path.parent.mkdir(parents=True, exist_ok=True)
                        pending_path.touch()
                        store_key(key)  # Persist before the network call, so retries reuse the same identity.
                    await relay_request(RELAY_BASE, key, 'POST', '/accounts', {})
                    pending_path.unlink(missing_ok=True)
                    return web.json_response({'registered':True, 'key_prefix':key[:15]}, headers=_headers(origin))
                elif operation == 'import_key':
                    key = data['key']
                    if not isinstance(key, str) or not key.startswith('olivia-') or len(key) > 128:
                        raise LLMSetupError('LLM_SETUP_FIELDS_INVALID', status=400)
                if not key:
                    raise LLMSetupError('RELAY_NOT_CONFIGURED', status=400)
                if operation == 'export_key':
                    return web.json_response({'key':key}, headers=_headers(origin))
                await connect(key)
                store_key(key)
                pending_path.unlink(missing_ok=True)
                return web.json_response({'connected':True, 'key_prefix':key[:15]}, headers=_headers(origin))
        try:
            key = stored_key()
        except (OSError, ValueError):
            raise LLMSetupError('RELAY_NOT_CONFIGURED', status=400) from None
        if not key:
            raise LLMSetupError('RELAY_NOT_CONFIGURED', status=400)
        if data == {'action':'balance'}:
            result = await relay_request(RELAY_BASE, key, 'GET', '/quota')
        elif data == {'action':'order_status'}:
            result = await relay_request(RELAY_BASE, key, 'GET', '/payments/orders/latest')
        elif set(data) == {'action','amount_cents'} and data['action'] == 'create_order' and type(data['amount_cents']) is int and data['amount_cents'] in (1000,2000,5000,10000):
            result = await relay_request(RELAY_BASE, key, 'POST', '/payments/orders', {'amount_cents':data['amount_cents']})
        else:
            raise LLMSetupError('LLM_SETUP_FIELDS_INVALID', status=400)
        return web.json_response(result, headers=_headers(origin))

    async def options(request):
        return web.Response(status=204, headers=_headers(_authorize(request, confirm=False), preflight=True))
    # Backend-only accessor: reuse the encrypted account key without exposing it to the page.
    app['olivia_relay_stored_key'] = stored_key
    app.router.add_post('/toy/relay/action', action)
    app.router.add_options('/toy/relay/action', options)
