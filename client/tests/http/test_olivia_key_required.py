import asyncio

import local_server
from runtime.reply import jev_billing


def test_send_and_preview_ask_for_the_olivia_key_before_creating_a_letter(monkeypatch):
    monkeypatch.setenv('OLIVIA_JEV_BILLING_ENABLED', '1')
    monkeypatch.setattr(jev_billing, '_account_key', lambda: None)
    monkeypatch.setattr(local_server, '_missing_memory_component', lambda: None)
    monkeypatch.setattr(local_server, '_proactive_busy', False)
    before = list(local_server.store.letters)
    for path in ('/toy/letter/send', '/toy/letter/route-preview'):
        result = asyncio.run(local_server.route('POST', path, {'content': 'synthetic letter'}, {}, defer_reply=True))
        assert result['code'] == 503
        assert result['data']['error_code'] == 'OLIVIA_KEY_REQUIRED'
        assert result['data']['retryable'] is False
    assert local_server.store.letters == before


def test_queued_letter_fails_with_key_required_when_the_key_disappears(monkeypatch):
    letter = {'letter_id': 'key-removed', 'content': 'synthetic', 'letter_status': 'PENDING'}
    monkeypatch.setattr(local_server.store, 'letters', [letter])
    monkeypatch.setattr(local_server, '_persist_store_state', lambda: None)

    async def generate(*_args, **_kwargs):
        raise ValueError('JEV_BILLING_ACCOUNT_UNAVAILABLE')

    monkeypatch.setattr(local_server, 'generate_reply', generate)
    assert asyncio.run(local_server._run_reply_job('key-removed', 'synthetic', idempotency_key=None)) is False
    assert letter['letter_status'] == 'FAILED' and letter['error_code'] == 'OLIVIA_KEY_REQUIRED'

    other = {'letter_id': 'other-failure', 'content': 'synthetic', 'letter_status': 'PENDING'}
    monkeypatch.setattr(local_server.store, 'letters', [other])

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError('synthetic outage')

    monkeypatch.setattr(local_server, 'generate_reply', unavailable)
    asyncio.run(local_server._run_reply_job('other-failure', 'synthetic', idempotency_key=None))
    assert other['error_code'] == 'LLM_UNAVAILABLE'


def test_send_and_preview_stop_when_the_reply_writer_has_no_key(monkeypatch):
    """The account key can exist while replies still have no key (1.x upgrade)."""
    from llm_gateway import UnconfiguredAdapter
    monkeypatch.setenv('OLIVIA_JEV_BILLING_ENABLED', '1')
    monkeypatch.setattr(jev_billing, '_account_key', lambda: 'olivia-synthetic-account-key')
    monkeypatch.setattr(local_server, '_missing_memory_component', lambda: None)
    monkeypatch.setattr(local_server, '_proactive_busy', False)
    config, _gateway = local_server.letters_adapter._runtime
    monkeypatch.setattr(local_server.letters_adapter, '_runtime', (config, UnconfiguredAdapter()))
    before = list(local_server.store.letters)
    for path in ('/toy/letter/send', '/toy/letter/route-preview'):
        result = asyncio.run(local_server.route('POST', path, {'content': 'synthetic letter'}, {}, defer_reply=True))
        assert result['code'] == 503
        assert result['data']['error_code'] == 'REPLY_SERVICE_NOT_CONNECTED'
        assert result['data']['retryable'] is False
    assert local_server.store.letters == before


def test_reply_writer_check_reads_the_key_of_a_configured_gateway(monkeypatch):
    from types import SimpleNamespace
    keyed = SimpleNamespace(config=SimpleNamespace(requires_api_key=True), _key=lambda: 'synthetic')
    keyless = SimpleNamespace(config=SimpleNamespace(requires_api_key=True), _key=lambda: None)
    optional = SimpleNamespace(config=SimpleNamespace(requires_api_key=False), _key=lambda: None)
    for gateway, missing in ((keyed, False), (keyless, True), (optional, False)):
        monkeypatch.setattr(local_server.letters_adapter, '_runtime', (None, gateway))
        assert local_server._reply_writer_unavailable() is missing


def test_failed_letter_diagnostics_name_the_internal_cause_without_user_text(monkeypatch):
    """A 2.0.1 bundle only said LLM_UNAVAILABLE, hiding which step failed."""
    monkeypatch.setattr(local_server, '_persist_store_state', lambda: None)
    causes = {
        'too-large': ValueError('JEV_INPUT_TOO_LARGE'),
        'wrapped': RuntimeError('outer'),
        'private': ValueError('我吃完了，味道很好'),
    }
    causes['wrapped'].__cause__ = ValueError('JEV_WORLD_SELECTION_CAPACITY')
    records = {}
    for letter_id, error in causes.items():
        monkeypatch.setattr(local_server.store, 'letters', [{'letter_id': letter_id, 'content': 'synthetic', 'letter_status': 'PENDING'}])

        async def generate(*_args, _error=error, **_kwargs):
            raise _error

        monkeypatch.setattr(local_server, 'generate_reply', generate)
        asyncio.run(local_server._run_reply_job(letter_id, 'synthetic', idempotency_key=None))
        records[letter_id] = [r for r in local_server.runtime_diagnostic_event_snapshot() if r['event'] == 'letter_failed'][-1]
    assert records['too-large']['cause_code'] == 'JEV_INPUT_TOO_LARGE'
    assert records['too-large']['exception_type'] == 'ValueError'
    assert records['wrapped']['cause_code'] == 'JEV_WORLD_SELECTION_CAPACITY'
    assert 'cause_code' not in records['private']
    assert '味道' not in str(records)


def test_letter_failed_for_low_balance_says_so(monkeypatch):
    """A JEV 402 (insufficient_balance) showed as "寄信通道好像有点忙" (LLM_UNAVAILABLE)."""
    import io
    from urllib.error import HTTPError
    from runtime.reply.companion_decision import _http_error_code
    error = HTTPError('https://relay/v1/companion/decide', 402, 'Payment Required', {},
                      io.BytesIO(b'{"error": "insufficient_balance"}'))
    assert _http_error_code(error) == 'JEV_BALANCE_INSUFFICIENT'

    letter = {'letter_id': 'low-balance', 'content': 'synthetic', 'letter_status': 'PENDING'}
    monkeypatch.setattr(local_server.store, 'letters', [letter])
    monkeypatch.setattr(local_server, '_persist_store_state', lambda: None)

    async def generate(*_args, **_kwargs):
        raise ValueError('JEV_BALANCE_INSUFFICIENT')

    monkeypatch.setattr(local_server, 'generate_reply', generate)
    asyncio.run(local_server._run_reply_job('low-balance', 'synthetic', idempotency_key=None))
    assert letter['error_code'] == 'LLM_QUOTA_EXHAUSTED'


def test_reply_world_fragments_carry_the_addressing_profile(monkeypatch, tmp_path):
    """Replies must call this user by their own names, not "用户" or a guess."""
    import json
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from runtime.private_world.daily_life import DailyLifeStore
    now = datetime(2026, 9, 29, 7, tzinfo=timezone.utc)
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    store.record_addressing('reply:a:1', {'user_calls_linli': '小离，', 'user_self': '你的老姜。'}, occurred_at=now)

    class Port:
        async def ask(self, state, questions, *, purpose):
            return {key: 'skip' for key in questions}

    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: Port())
    monkeypatch.setattr(local_server.letters_adapter, 'daily_life', SimpleNamespace(store=store))
    monkeypatch.setattr(local_server.letters_adapter, 'recent_letter_fragments', lambda *a, **k: ())
    fragments = asyncio.run(local_server.letters_adapter.prepare_daily_life_fragments('你还记得吗', now=now))
    addressing = next(f for f in fragments if f.fragment_id == 'linli.addressing')
    value = json.loads(addressing.text)
    assert [q['quote'] for q in value['quotes']['user_calls_linli']] == ['小离']
    assert '不要称对方为“用户”' in value['meaning']
