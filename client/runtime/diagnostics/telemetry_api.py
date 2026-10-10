"""Local configuration and best-effort batched transport on the saved account."""
import asyncio
import sqlite3

from aiohttp import web
from original_client_setup_api import LLMSetupError, SESSION_HEADER, _authorize, _body, _headers
from . import reply_telemetry


def mount(app, setup, stored_key, remote):
    from original_client_update_api import running_component_version
    collector = None
    try:
        collector = reply_telemetry.Collector(setup._config_root, version=running_component_version().get('version') or 'unknown')
        if not collector.account(stored_key()): collector.emit('app', 'ok')
        from runtime.model_routes import routed_model
        collector.model = lambda: routed_model(setup._config().base_url, setup._config().model)
    except (LLMSetupError, OSError, ValueError, sqlite3.Error):
        collector = None
    reply_telemetry.configure(collector)

    async def action(request):
        origin = _authorize(request, confirm=True)
        setup.require_session(request.headers.get(SESSION_HEADER, ''))
        body = await _body(request)
        if not (body == {'action': 'status'} or body == {'action': 'recharge_page'} or
                set(body) == {'action', 'enabled'} and body['action'] == 'set_enabled' and type(body['enabled']) is bool):
            raise LLMSetupError('LLM_SETUP_FIELDS_INVALID', status=400)
        if collector is None:
            raise LLMSetupError('LLM_SETUP_SAVE_FAILED', status=503)
        try:
            collector.account(stored_key())
            if body['action'] == 'set_enabled': collector.set_enabled(body['enabled'])
            if body['action'] == 'recharge_page': collector.emit('recharge_page', 'ok')
        except ValueError:
            raise LLMSetupError('RELAY_NOT_CONFIGURED', status=400) from None
        except (OSError, sqlite3.Error):
            raise LLMSetupError('LLM_SETUP_SAVE_FAILED', status=503) from None
        return web.json_response(collector.status(), headers=_headers(origin))

    async def options(request):
        return web.Response(status=204, headers=_headers(_authorize(request, confirm=False), preflight=True))

    async def lifecycle(app):
        async def run():
            while True:
                try:
                    key = stored_key()
                    collector.account(key)
                    async def send(events):
                        return await remote(key, {'schema': 1, 'events': events})
                    await collector.upload(send)
                except (LLMSetupError, OSError, ValueError, sqlite3.Error):
                    collector.last_error = 'TELEMETRY_UPLOAD_FAILED'
                await asyncio.sleep(60)
        task = asyncio.create_task(run()) if collector else None
        yield
        if task:
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass
        if reply_telemetry._collector is collector:
            reply_telemetry.configure(None)

    app.router.add_post('/toy/telemetry/action', action)
    app.router.add_options('/toy/telemetry/action', options)
    app.cleanup_ctx.append(lifecycle)
