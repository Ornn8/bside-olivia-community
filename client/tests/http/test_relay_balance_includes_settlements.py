"""Balance and statement reads include judgments that settle in the background."""
import asyncio
import hashlib
import threading
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from original_client_setup_api import LLMSetupService, mount_original_client_setup_api
from runtime.reply import jev_billing as billing

KEY = 'olivia-synthetic-private-key'


@pytest.mark.parametrize('action,path', [('balance', '/quota'), ('statement', '/billing/statement')])
def test_read_waits_for_due_settlements(tmp_path, monkeypatch, action, path):
    import original_client_relay_api as relay
    events = []
    gate = threading.Event()

    async def remote(base, key, method, route, payload=None):
        if route.split('?')[0] == '/telemetry/events':
            return {'accepted': [e['event_id'] for e in payload['events']]}
        events.append(route)
        return {'balance': '1.00'}

    def settle(key, signed, version=None):
        assert gate.wait(5)
        events.append('settled')
        data = signed['receipt']
        return dict(status='settled', turn_id=data['turn_id'], operation_id=data['operation_id'],
                    price_version=data['price_version'], input_tokens=data['input_tokens'],
                    charged_units=data['input_tokens'] * 150, replayed=False,
                    debited_units=data['input_tokens'] * 150)

    monkeypatch.setattr(relay, 'RELAY_BASE', 'https://relay.example/v1')
    monkeypatch.setattr(relay, 'relay_request', remote)
    monkeypatch.setattr(billing, '_post_settlement', settle)
    monkeypatch.setenv('OLIVIA_JEV_BILLING_ENABLED', '1')
    monkeypatch.setattr(billing, '_account_key', None)  # The route installs its own; restore afterwards.
    service = LLMSetupService(tmp_path, unprotect=lambda value: KEY)
    cipher = tmp_path / 'cipher'
    cipher.write_text('encrypted')
    monkeypatch.setattr(service, '_config', lambda: SimpleNamespace(model='qwen3.7-flash', base_url='https://relay.example/v1'))
    monkeypatch.setattr(service, '_active_key_path', lambda: cipher)
    service.observe_login(success=True)
    state = tmp_path / 'state'
    billing.configure_settlement_outbox(state)

    async def scenario():
        app = web.Application()
        mount_original_client_setup_api(app, service, trusted_origins=('https://client.example',))
        async with TestClient(TestServer(app)) as client:
            headers = {'Origin': 'https://client.example', 'X-Olivia-Setup-Action': 'confirmed',
                       'X-Olivia-Setup-Session': service._session_token}
            receipt = {'signature': 'a' * 64, 'receipt': dict(
                v=1, turn_id='synthetic:turn', account_key_digest=hashlib.sha256(KEY.encode()).hexdigest(),
                input_digest='b' * 64, price_version=billing.PRICE_VERSION, input_tokens=17,
                operation_id='c' * 64)}
            with billing.billing_scope('synthetic:turn'):
                assert billing.settle_receipt_sync(receipt, 'b' * 64) is None
            events.clear()
            threading.Timer(0.3, gate.set).start()
            response = await client.post('/toy/relay/action', headers=headers, json={'action': action})
            assert response.status == 200
            assert events == ['settled', path]

    try:
        asyncio.run(scenario())
    finally:
        gate.set()
        billing.shutdown_settlement_outbox()
