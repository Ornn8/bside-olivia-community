"""Synthetic upgrade coverage: preserve credentials while retiring the old IP."""
import asyncio
import hashlib
import json

import pytest

from installer import start_local
from llm_gateway import GatewayConfig, ManagedLLMConfig, OpenAICompatibleAdapter
from original_client_setup_api import LLMSetupService, LLMSetupError, _relay_config
from runtime import image_assets
from runtime.cloud_service import CloudError
from runtime.remote_generation import RemoteGeneration
from runtime.reply.companion_decision import JevDecisionPort


LEGACY = 'https://175.24.191.6/v1'
OFFICIAL = 'https://api.bside-moon.cn/v1'
COS = 'https://iupaper-1387429524.cos.ap-guangzhou.myqcloud.com'


@pytest.mark.parametrize('schema', [1, 2, 3])
def test_upgrade_keeps_bound_key_and_selected_model(tmp_path, monkeypatch, schema):
    root = tmp_path / 'config'
    root.mkdir()
    name = 'deepseek_api_key.dpapi' if schema == 1 else f'deepseek_api_key.{"a" * 32}.dpapi'
    ciphertext = b'dpapi-v1:synthetic-encrypted-key\n'
    (root / name).write_bytes(ciphertext)
    original = {'schema_version': schema, 'base_url': LEGACY, 'model': 'qwen3.7-flash'}
    if schema in (2, 3):
        original.update(provider='openai_compatible', max_retries=4, key_file=name,
                        key_sha256=hashlib.sha256(ciphertext).hexdigest())
    # A separately saved account must never replace this valid active binding.
    (root / 'olivia_relay_key.dpapi').write_bytes(b'different-saved-account\n')
    config_path = root / 'llm.json'
    config_path.write_text(json.dumps(original), encoding='utf-8')
    loaded = []
    monkeypatch.setattr(start_local, '_load_dpapi_key', lambda path: loaded.append(path) or 'olivia-synthetic')

    environment = start_local._load_llm_environment({}, tmp_path, include_secret=True)

    assert environment['OLIVIA_LLM_BASE_URL'] == OFFICIAL
    assert environment['OLIVIA_LLM_MODEL'] == original['model']
    assert environment['OLIVIA_LLM_API_KEY'] == 'olivia-synthetic'
    assert loaded == [root / name]
    assert json.loads(config_path.read_text(encoding='utf-8')) == {**original, 'base_url': OFFICIAL}
    assert (root / name).read_bytes() == ciphertext
    assert not (root / 'llm.retired.json').exists()
    assert sorted(p.name for p in root.glob('deepseek_api_key*.dpapi')) == [name]
    assert LLMSetupService(tmp_path).status()['llm']['key_configured'] is True


def test_setup_alone_migrates_saved_config_without_decrypting(tmp_path):
    root = tmp_path / 'config'
    root.mkdir()
    (root / 'deepseek_api_key.dpapi').write_bytes(b'encrypted-key')
    config = {'schema_version': 1, 'base_url': LEGACY, 'model': 'qwen3.7-flash'}
    path = root / 'llm.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    setup = LLMSetupService(tmp_path, unprotect=lambda _: pytest.fail('must not decrypt to migrate'))
    assert setup.status()['llm']['base_url'] == OFFICIAL
    assert setup._active_key_path() == root / 'deepseek_api_key.dpapi'
    saved = path.read_bytes()
    assert json.loads(saved)['base_url'] == OFFICIAL
    assert LLMSetupService(tmp_path).status()['llm']['key_configured'] is True
    assert path.read_bytes() == saved  # Already migrated settings are not rewritten.


def test_existing_environment_and_direct_clients_use_new_domain(tmp_path):
    environment = start_local._load_llm_environment({
        'OLIVIA_LLM_BASE_URL': LEGACY,
        'OLIVIA_MEMORY_LLM_DEFAULT_BASE_URL': LEGACY,
        'OLIVIA_JEV_DECISION_URL': LEGACY + '/companion/decide',
    }, tmp_path)
    assert environment['OLIVIA_LLM_BASE_URL'] == OFFICIAL
    assert environment['OLIVIA_MEMORY_LLM_DEFAULT_BASE_URL'] == OFFICIAL
    assert environment['OLIVIA_JEV_DECISION_URL'] == OFFICIAL + '/companion/decide'
    assert GatewayConfig.from_mapping({'base_url': LEGACY}).base_url == OFFICIAL
    assert OpenAICompatibleAdapter(GatewayConfig(base_url=LEGACY))._url() == OFFICIAL + '/chat/completions'
    assert RemoteGeneration(LEGACY[:-3], 'olivia-synthetic').url == OFFICIAL[:-3]
    assert JevDecisionPort(LEGACY + '/companion/decide').endpoint == OFFICIAL + '/companion/decide'


@pytest.mark.parametrize('url', [
    'http://175.24.191.6/v1', 'https://175.24.191.6:444/v1',
    LEGACY + '?forward=1', LEGACY + '#hidden', LEGACY + '/other',
    'https://175.24.191.6.attacker.test/v1', 'https://175.24.191.6@attacker.test/v1',
    'https://api.bside-moon.cn.attacker.test/v1', 'https://api.bside-moon.cn:444/v1',
    OFFICIAL + '?forward=1', OFFICIAL + '/other',
])
def test_untrusted_settings_never_reuse_saved_key(tmp_path, url):
    with pytest.raises(LLMSetupError):
        _relay_config(url, 'qwen3.7-flash')
    root = tmp_path / 'config'
    root.mkdir()
    (root / 'deepseek_api_key.dpapi').write_bytes(b'encrypted-key')
    (root / 'llm.json').write_text(json.dumps({'schema_version': 1, 'base_url': OFFICIAL,
                                              'model': 'qwen3.7-flash'}), encoding='utf-8')
    setup = LLMSetupService(tmp_path, unprotect=lambda _: pytest.fail('must not decrypt'))
    with pytest.raises(LLMSetupError):
        asyncio.run(setup.test({'base_url': url, 'model': 'qwen3.7-flash', 'api_key': ''}))


def test_read_only_config_still_connects_with_existing_binding(tmp_path, monkeypatch):
    from runtime import official_endpoints
    root = tmp_path / 'config'
    root.mkdir()
    (root / 'deepseek_api_key.dpapi').write_bytes(b'encrypted-key')
    path = root / 'llm.json'
    path.write_text(json.dumps({'schema_version': 1, 'base_url': LEGACY,
                                'model': 'qwen3.7-flash'}), encoding='utf-8')
    previous = path.read_bytes()
    def denied(*args):
        raise PermissionError('synthetic-read-only-install')
    monkeypatch.setattr(official_endpoints.os, 'replace', denied)
    status = LLMSetupService(tmp_path).status()
    assert status['llm']['base_url'] == OFFICIAL
    assert status['llm']['key_configured'] is True
    assert path.read_bytes() == previous
    assert not list(root.glob('.llm-domain-*'))


@pytest.mark.parametrize('base', ['https://attacker.test/v1', OFFICIAL + '?forward=1', OFFICIAL + '/other'])
def test_relay_transport_rejects_arbitrary_host_before_network(monkeypatch, base):
    import original_client_relay_api as relay
    monkeypatch.setattr(relay, 'ClientSession', lambda **_: pytest.fail('must not send credentials'))
    with pytest.raises(LLMSetupError, match='RELAY_NOT_CONFIGURED'):
        asyncio.run(relay.relay_request(base, 'olivia-synthetic', 'GET', '/quota'))


def test_both_official_hosts_require_an_account_key():
    for base in (LEGACY, OFFICIAL):
        with pytest.raises(ValueError, match='authentication'):
            ManagedLLMConfig.from_mapping({'schema_version': 3, 'provider': 'openai_compatible',
                'base_url': base, 'model': 'qwen3.7-flash', 'max_retries': 2, 'requires_api_key': False})


def test_exact_cos_distribution_image_and_legacy_r2_are_allowed():
    entry = {'key': 'distribution/olivia-images/wardrobe/synthetic/look.webp'}
    for url in (COS + '/olivia/components/' + entry['key'] + '?q-signature=synthetic',
                'https://' + image_assets.R2_HOST + '/vocal-backlog/' + entry['key']):
        image_assets._validate_download_url(url, entry)
    for url in (COS + '/other/' + entry['key'], COS.replace('iupaper', 'other') + '/olivia/components/' + entry['key'],
                COS + '/olivia/components/' + entry['key'] + '#fragment',
                COS.replace('https:', 'http:') + '/olivia/components/' + entry['key']):
        with pytest.raises(CloudError, match='IMAGE_ASSET_INVALID'):
            image_assets._validate_download_url(url, entry)


def test_cos_qq_ticket_keeps_pinned_version_size_and_hash(monkeypatch):
    from runtime.personal_chat import napcat_bundle as bundle, napcat_installer as installer
    key = f'distribution/olivia-components/qq/v4.18.28/{bundle.BUNDLE_SHA256}/Olivia-QQ-v4.18.28-full.zip'
    url = COS + '/olivia/components/' + key + '?q-signature=synthetic'
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def geturl(self): return bundle.TICKET_URL
        def read(self, limit):
            return json.dumps({'url': url, 'sha256': bundle.BUNDLE_SHA256, 'size_bytes': bundle.BUNDLE_SIZE}).encode()
    def open_ticket(request, **kwargs):
        calls.append(request.full_url)
        return Response()
    monkeypatch.setattr(bundle.urllib.request, 'urlopen', open_ticket)
    assert bundle._ticket() == url
    assert calls == [OFFICIAL + '/components/qq/download']
    assert installer._allowed_download_url(url)
    assert not installer._allowed_download_url(url.replace('.com/', '.com:444/'))
    assert not installer._allowed_download_url(url + '#fragment')
