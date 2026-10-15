"""New accounts start on Haiku with an Opus diary; existing users keep what they saved."""
import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import original_client_relay_api as relay
from original_client_setup_api import LLMSetupError, LLMSetupService, mount_original_client_setup_api
from runtime import model_routes

KEY = 'olivia-synthetic-private-key'
NEW_ROUTES = {'qq': None, 'letter': None, 'diary': 'claude-opus-5-5', 'story': 'gemini-4-argon'}


@pytest.fixture(autouse=True)
def reset_routes():
    yield
    model_routes._path, model_routes._cache = None, (None, {})


def make_service(tmp_path, monkeypatch, *, fail_first_registration=False):
    calls = []

    async def remote(base, key, method, path, payload=None):
        calls.append((method, path))
        if fail_first_registration and path == '/accounts' and calls.count((method, path)) == 1:
            raise LLMSetupError('RELAY_UNAVAILABLE', status=503)
        return {'registered': True}

    async def probe(*args):
        pass

    monkeypatch.setattr(relay, 'relay_request', remote)
    service = LLMSetupService(tmp_path, protect=lambda key: 'cipher:' + key,
                              unprotect=lambda cipher: cipher.removeprefix('cipher:'), probe=probe)
    service.observe_login(success=True)
    return service, calls


def run(service, steps):
    """steps: async callable receiving a post(payload) helper; returns its result."""
    async def scenario():
        app = web.Application()
        mount_original_client_setup_api(app, service, trusted_origins=('https://client.example',))
        headers = {'Origin': 'https://client.example', 'X-Olivia-Setup-Action': 'confirmed',
                   'X-Olivia-Setup-Session': service._session_token}
        async with TestClient(TestServer(app)) as client:
            async def post(payload):
                return await client.post('/toy/relay/action', headers=headers, json=payload)
            return await steps(post)
    return asyncio.run(scenario())


def routes_file(service):
    return service._config_root / 'olivia_model_routes.json'


def saved_model(service):
    return json.loads(service._config_path.read_text(encoding='utf-8'))['model']


def test_defaults_are_haiku_for_replies_and_sonnet_stays_only_the_price_baseline():
    assert relay.RELAY_MODEL == 'claude-haiku-5-5'
    assert relay.RELAY_MULTIPLIER_BASELINE == 'claude-sonnet-5-5'
    assert {relay.RELAY_MODEL, 'claude-sonnet-5-5', 'claude-opus-5-5'} <= relay.RELAY_MODELS
    assert relay.NEW_ACCOUNT_ROUTES == NEW_ROUTES


def test_claim_on_a_fresh_config_binds_haiku_and_routes_the_diary_to_opus(tmp_path, monkeypatch):
    service, calls = make_service(tmp_path, monkeypatch)
    assert not service._config_path.exists() and not routes_file(service).exists()

    async def steps(post):
        claimed = await post({'action': 'claim'})
        assert claimed.status == 200
        assert json.loads(routes_file(service).read_text(encoding='utf-8')) == NEW_ROUTES
        assert service._config().model == 'claude-haiku-5-5'
        connected = await post({'action': 'connect'})
        assert connected.status == 200
    run(service, steps)

    assert saved_model(service) == 'claude-haiku-5-5'
    assert model_routes.load() == NEW_ROUTES
    with model_routes.using('diary'):
        assert model_routes.routed_model(relay.RELAY_BASE, 'claude-haiku-5-5') == 'claude-opus-5-5'
    with model_routes.using('qq'):
        assert model_routes.routed_model(relay.RELAY_BASE, 'claude-haiku-5-5') == 'claude-haiku-5-5'
    with model_routes.using('letter'):
        assert model_routes.routed_model(relay.RELAY_BASE, 'claude-haiku-5-5') == 'claude-haiku-5-5'
    assert ('POST', '/accounts') in calls


def test_failed_registration_writes_no_routes_until_the_retry_succeeds(tmp_path, monkeypatch):
    service, _ = make_service(tmp_path, monkeypatch, fail_first_registration=True)

    async def steps(post):
        first = await post({'action': 'claim'})
        assert first.status == 503
        assert not routes_file(service).exists()
        second = await post({'action': 'claim'})
        assert second.status == 200
    run(service, steps)

    assert json.loads(routes_file(service).read_text(encoding='utf-8')) == NEW_ROUTES


def test_existing_sonnet_binding_stays_sonnet(tmp_path, monkeypatch):
    service, _ = make_service(tmp_path, monkeypatch)
    service._config_root.mkdir(parents=True)
    (service._config_root / 'olivia_relay_key.dpapi').write_text('cipher:' + KEY)

    async def steps(post):
        assert (await post({'action': 'select_model', 'model': 'claude-sonnet-5-5'})).status == 200
        assert saved_model(service) == 'claude-sonnet-5-5'
        # Reconnecting and claiming again with the same account never touch the saved model or routes.
        assert (await post({'action': 'connect'})).status == 200
        assert (await post({'action': 'claim'})).status == 200
        assert (await post({'action': 'connect'})).status == 200
    run(service, steps)

    assert saved_model(service) == 'claude-sonnet-5-5'
    assert service._config().model == 'claude-sonnet-5-5'
    assert not routes_file(service).exists()


def test_existing_routes_file_is_left_untouched_by_a_new_claim(tmp_path, monkeypatch):
    service, _ = make_service(tmp_path, monkeypatch)
    service._config_root.mkdir(parents=True)
    own = '{"qq": "qwen3.7-flash", "letter": null, "diary": null}'
    routes_file(service).write_text(own, encoding='utf-8')

    async def steps(post):
        assert (await post({'action': 'claim'})).status == 200
    run(service, steps)

    assert routes_file(service).read_text(encoding='utf-8') == own


def test_damaged_routes_file_is_not_replaced_by_a_new_claim(tmp_path, monkeypatch):
    service, _ = make_service(tmp_path, monkeypatch)
    service._config_root.mkdir(parents=True)
    routes_file(service).write_text('{broken', encoding='utf-8')

    async def steps(post):
        assert (await post({'action': 'claim'})).status == 200
    run(service, steps)

    assert routes_file(service).read_text(encoding='utf-8') == '{broken'


def test_import_key_does_not_write_routes_and_keeps_the_default_for_a_fresh_binding(tmp_path, monkeypatch):
    service, _ = make_service(tmp_path, monkeypatch)

    async def steps(post):
        imported = await post({'action': 'import_key', 'key': KEY})
        assert imported.status == 200
    run(service, steps)

    assert not routes_file(service).exists()
    assert saved_model(service) == 'claude-haiku-5-5'


def test_import_key_after_an_interrupted_claim_does_not_write_routes(tmp_path, monkeypatch):
    service, _ = make_service(tmp_path, monkeypatch, fail_first_registration=True)

    async def steps(post):
        assert (await post({'action': 'claim'})).status == 503
        assert (await post({'action': 'import_key', 'key': KEY})).status == 200
        assert (await post({'action': 'claim'})).status == 200
    run(service, steps)

    assert not routes_file(service).exists()


def test_save_if_absent_never_overwrites(tmp_path):
    model_routes.configure(tmp_path)
    assert model_routes.save_if_absent(NEW_ROUTES) is True
    assert model_routes.load() == NEW_ROUTES
    other = {'qq': 'qwen3.7-flash', 'letter': None, 'diary': None}
    model_routes.save(other)
    assert model_routes.save_if_absent(NEW_ROUTES) is False
    assert model_routes.load() == {**other, 'story': 'gemini-4-argon'}
    with pytest.raises(ValueError):
        model_routes.save_if_absent({'qq': None})
