import asyncio
from copy import deepcopy

import pytest

from runtime import cloud_policy as cp


def test_defaults_match_2215():
    from runtime.personal_chat.service import BALANCE_ERROR_CODES
    from runtime.personal_chat.initiative_profile import _BASE
    assert cp.DEFAULTS['qq_default_voice'] == {'mode': 'voice_only', 'wait_seconds': 60}
    assert set(cp.DEFAULTS['generation_retry']['no_retry_codes']) == BALANCE_ERROR_CODES
    for tier, profile in _BASE.items():
        assert cp.DEFAULTS['proactive']['intervals'][tier] == {
            'min_minutes': profile.im_interval_min // 60, 'max_minutes': profile.im_interval_max // 60}
    assert cp.DEFAULTS['media_share'] == {'photo_daily_limit': None, 'video_daily_limit': None}


@pytest.mark.parametrize('key', list(('qq_default_voice', 'proactive', 'generation_retry', 'notices', 'media_share')))
@pytest.mark.parametrize('bad', [None, True, [], 'bad'])
def test_invalid_key_falls_back_independently(key, bad):
    raw = deepcopy(cp.DEFAULTS)
    raw['qq_default_voice']['mode'] = 'text_only'
    raw[key] = bad
    result = cp.validate_policy(raw)
    assert result[key] == cp.DEFAULTS[key]
    if key != 'qq_default_voice':
        assert result['qq_default_voice']['mode'] == 'text_only'


def test_unknown_keys_and_numeric_clamping():
    raw = deepcopy(cp.DEFAULTS)
    raw['unknown'] = {'unsafe': True}
    raw['qq_default_voice'].update(wait_seconds=999, future=True)
    raw['media_share']['photo_daily_limit'] = -10
    result = cp.validate_policy(raw)
    assert 'unknown' not in result and 'future' not in result['qq_default_voice']
    assert result['qq_default_voice']['wait_seconds'] == 120
    assert result['media_share']['photo_daily_limit'] == 0
    raw['qq_default_voice']['wait_seconds'] = True
    assert cp.validate_policy(raw)['qq_default_voice'] == cp.DEFAULTS['qq_default_voice']


def test_cache_survives_offline_and_invalid_envelope(tmp_path):
    async def run():
        manager = cp.CloudPolicy(tmp_path, '2.2.16')
        async def ok():
            return {'version': 'test-1', 'policy': {'qq_default_voice': {'mode': 'text_only', 'wait_seconds': 30}}}
        await manager.refresh(ok)
        restored = cp.CloudPolicy(tmp_path, '2.2.16')
        async def offline(): raise TimeoutError()
        await restored.refresh(offline)
        assert restored.version == 'test-1'
        assert restored.policy['qq_default_voice']['mode'] == 'text_only'
        async def invalid(): return {'version': 'bad\nversion', 'policy': {}}
        await restored.refresh(invalid)
        assert restored.version == 'test-1'
        assert cp.CloudPolicy(tmp_path / 'empty', '2.2.16').policy == cp.DEFAULTS
    asyncio.run(run())


@pytest.mark.parametrize('value,expected', [(5,5), (120,120), (-1,5), (121,120)])
def test_wait_boundaries(value, expected):
    assert cp.validate_policy({'qq_default_voice': {'wait_seconds': value}})['qq_default_voice']['wait_seconds'] == expected


@pytest.mark.parametrize('key,value', [
    ('qq_default_voice', {'mode': []}), ('qq_default_voice', {'wait_seconds': float('nan')}),
    ('proactive', {'intervals': {'close': {'min_minutes': 26, 'max_minutes': 25}}}),
    ('proactive', {'engagement': {'trusted_days': False}}),
    ('generation_retry', {'no_retry_codes': ['not a code']}),
    ('generation_retry', {'no_retry_codes': 'LLM_QUOTA_EXHAUSTED'}),
    ('notices', {'DEFAULT': 'x' * 501}), ('notices', {'DEFAULT': 'unsafe\ntext'}),
    ('media_share', {'video_daily_limit': False}), ('media_share', {'photo_daily_limit': 1.5}),
])
def test_invalid_nested_values_restore_whole_key(key, value):
    assert cp.validate_policy({key: value})[key] == cp.DEFAULTS[key]


def test_proactive_and_media_bounds():
    raw = {'proactive': {'intervals': {'reserved': {'min_minutes': -1, 'max_minutes': 5000}},
                          'engagement': {'trusted_days': 0, 'close_count': 10001}},
           'media_share': {'photo_daily_limit': 1001, 'video_daily_limit': None}}
    result = cp.validate_policy(raw)
    assert result['proactive']['intervals']['reserved'] == {'min_minutes': 1, 'max_minutes': 1440}
    assert result['proactive']['engagement']['trusted_days'] == 1
    assert result['proactive']['engagement']['close_count'] == 10000
    assert result['media_share'] == {'photo_daily_limit': 1000, 'video_daily_limit': None}


def test_proactive_hot_reads_and_environment_override(tmp_path, monkeypatch):
    from runtime.personal_chat.initiative_profile import profile_from_snapshot
    from runtime.personal_chat.backend import _qq_default_voice_timeout_seconds
    manager = cp.CloudPolicy(tmp_path, '2.2.16')
    manager.policy['proactive']['intervals']['reserved'] = {'min_minutes': 3, 'max_minutes': 4}
    manager.policy['qq_default_voice']['wait_seconds'] = 45
    monkeypatch.setattr(cp, '_active', manager)
    monkeypatch.delenv('OLIVIA_QQ_DEFAULT_VOICE_TIMEOUT_SECONDS', raising=False)
    assert profile_from_snapshot(None).im_interval_min == 180
    assert _qq_default_voice_timeout_seconds() == 45
    monkeypatch.setenv('OLIVIA_QQ_DEFAULT_VOICE_TIMEOUT_SECONDS', '12')
    assert _qq_default_voice_timeout_seconds() == 12


def test_telemetry_waits_for_capability_and_downgrades(tmp_path):
    from runtime.diagnostics.reply_telemetry import Collector
    collector = Collector(tmp_path, clock=lambda: 1000)
    collector.account('synthetic')
    async def negotiate(events):
        assert all('policy_version' not in event for event in events)
        return {'accepted': [e['event_id'] for e in events], 'supported_schemas': [1,2,3],
                'policy_version_supported': True}
    asyncio.run(collector.upload(negotiate))
    collector.emit('app', 'ok')
    assert collector.batch(3)[0]['policy_version'] == 'builtin-2.2.15'
    assert 'policy_version' not in collector.batch(1)[0]
    async def old(events):
        return {'accepted': [e['event_id'] for e in events]}
    asyncio.run(collector.upload(old))
    collector.emit('app', 'ok')
    assert 'policy_version' not in collector.batch(3)[0]


def test_configured_no_retry_code_and_notice_are_used(tmp_path, monkeypatch):
    from runtime.personal_chat.service import PersonalChatService
    from runtime.personal_chat.events import PersonalMessage
    manager = cp.CloudPolicy(tmp_path, '2.2.16')
    manager.policy['generation_retry']['no_retry_codes'] = ['SYNTHETIC_STOP']
    manager.policy['notices']['SYNTHETIC_STOP'] = '【系统提示】请稍后再试。'
    monkeypatch.setattr(cp, '_active', manager)
    rows, sent, calls = [], [], []
    async def generate(event, row):
        calls.append(event)
        raise ValueError('SYNTHETIC_STOP')
    async def commit(row): pass
    async def send(text):
        sent.append(text)
        return 'ack'
    service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('b','u')})
    event = PersonalMessage('qq','b','u','one','synthetic')
    async def run():
        with pytest.raises(ValueError): await service.handle(event, send)
        await service.notify_generation_failure(event, send)
        with pytest.raises(RuntimeError, match='SYNTHETIC_STOP'):
            await service.handle(event, send)
    asyncio.run(run())
    assert len(calls) == 1 and rows[0]['generation_retryable'] is False
    assert sent == ['【系统提示】请稍后再试。']


def test_lifecycle_fetches_at_start_and_each_ten_minutes(tmp_path, monkeypatch):
    from aiohttp import web
    from types import SimpleNamespace
    calls, delays = [], []
    async def fetch(key, version):
        calls.append((key, version))
        return {'version': 'live-1', 'policy': {}}
    original_wait_for = asyncio.wait_for
    async def wait_for(awaitable, timeout):
        assert timeout == 3
        return await original_wait_for(awaitable, timeout)
    async def run():
        stopped = asyncio.Event()
        async def sleep(delay):
            delays.append(delay)
            if len(delays) > 1:
                stopped.set()
                await asyncio.Event().wait()
        monkeypatch.setattr(cp, 'fetch_policy', fetch)
        monkeypatch.setattr(cp, 'asyncio', SimpleNamespace(wait_for=wait_for, sleep=sleep,
            create_task=asyncio.create_task, gather=asyncio.gather))
        app = web.Application()
        cp.mount(app, tmp_path, lambda: 'synthetic-account')
        lifecycle = app.cleanup_ctx[0](app)
        await anext(lifecycle)
        await stopped.wait()
        assert cp.policy_version() == 'live-1'
        await lifecycle.aclose()
        assert cp._active is None
    asyncio.run(run())
    assert len(calls) == 2 and delays == [600,600]


def test_http_request_uses_account_key_version_timeout_and_no_redirect(monkeypatch):
    import aiohttp
    import json
    calls = []
    class Response:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def raise_for_status(self): pass
        @property
        def content(self): return self
        async def iter_chunked(self, size):
            yield json.dumps({'version':'test-1','policy':{}}).encode()
    class Session:
        def __init__(self, *, timeout): assert timeout.total == 3
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()
    monkeypatch.setattr(aiohttp, 'ClientSession', Session)
    assert asyncio.run(cp.fetch_policy('synthetic', '2.2.16'))['version'] == 'test-1'
    url, options = calls[0]
    assert url.endswith('/v1/client-policy')
    assert options['headers'] == {'Authorization': 'Bearer synthetic', 'X-Olivia-Client-Version':'2.2.16'}
    assert options['allow_redirects'] is False
