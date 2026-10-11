import asyncio
import ast
import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.reply import jev_billing as billing

KEY = 'olivia-synthetic-account-no-real-secret'
DIGEST = 'b' * 64


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv('OLIVIA_JEV_BILLING_ENABLED', '1')
    monkeypatch.setattr(billing, '_account_key', lambda: KEY)
    assert billing.CURRENT.get() is None
    yield
    assert billing.CURRENT.get() is None


def signed(tokens=17):
    return dict(signature='a'*64, receipt=dict(v=1, turn_id='synthetic:turn',
        account_key_digest=hashlib.sha256(KEY.encode()).hexdigest(), input_digest=DIGEST,
        price_version=billing.PRICE_VERSION, input_tokens=tokens, operation_id='c'*64,
        purpose='character-emotion'))


def result(receipt, *, replayed=False):
    data = receipt['receipt']
    amount = data['input_tokens'] * 150
    return dict(status='settled', turn_id=data['turn_id'], operation_id=data['operation_id'],
        price_version=data['price_version'], input_tokens=data['input_tokens'],
        charged_units=amount, debited_units=0 if replayed else amount, replayed=replayed)


def test_exact_price_and_repeat_uses_relay_idempotency(configured, monkeypatch):
    debits, calls = [], []
    def settle(key, receipt):
        assert key == KEY
        replayed = bool(calls)
        calls.append(receipt)
        value = result(receipt, replayed=replayed)
        debits.append(value['debited_units'])
        return value
    monkeypatch.setattr(billing, '_post_settlement', settle)
    with billing.billing_scope('synthetic:turn'):
        first = billing.settle_receipt_sync(signed(), DIGEST)
        second = billing.settle_receipt_sync(signed(), DIGEST)
        assert first['charged_units'] == second['charged_units'] == 2550
        assert second['replayed'] is True
    assert debits == [2550, 0]


@pytest.mark.parametrize('tokens', [None, True, -1, 1.0, '17', 1_000_001])
def test_unknown_usage_never_reaches_wallet(configured, monkeypatch, tokens):
    monkeypatch.setattr(billing, '_post_settlement', lambda *a: pytest.fail('must not settle'))
    with billing.billing_scope('synthetic:turn'):
        with pytest.raises(ValueError, match='JEV_BILLING_RECEIPT_INVALID'):
            billing.settle_receipt_sync(signed(tokens), DIGEST)


@pytest.mark.parametrize('field,value', [('turn_id','another-turn'),('account_key_digest','a'*64),
    ('input_digest','a'*64),('price_version','old-price')])
def test_wrong_attribution_rejected_before_settlement(configured, monkeypatch, field, value):
    monkeypatch.setattr(billing, '_post_settlement', lambda *a: pytest.fail('must not settle'))
    receipt = signed()
    receipt['receipt'][field] = value
    with billing.billing_scope('synthetic:turn'):
        with pytest.raises(ValueError, match='JEV_BILLING_RECEIPT_INVALID'):
            billing.settle_receipt_sync(receipt, DIGEST)


@pytest.mark.parametrize('field,value', [('charged_units',2549),('debited_units',0),('replayed',1),('input_tokens',18)])
def test_wrong_relay_amount_is_not_consumed(configured, monkeypatch, field, value):
    answer = result(signed())
    answer[field] = value
    monkeypatch.setattr(billing, '_post_settlement', lambda *a: answer)
    with billing.billing_scope('synthetic:turn'):
        # A wrong amount is a real mismatch, reported as such, never "unavailable".
        with pytest.raises(ValueError, match='JEV_BILLING_RESPONSE_INVALID'):
            billing.settle_receipt_sync(signed(), DIGEST)


@pytest.mark.parametrize('replayed', [False, True])
def test_published_minimum_is_accepted_for_small_judgments(configured, monkeypatch, replayed):
    answer = result(signed(), replayed=replayed)
    answer.update(charged_units=billing.MINIMUM_CHARGE_UNITS,
                  debited_units=0 if replayed else billing.MINIMUM_CHARGE_UNITS)
    monkeypatch.setattr(billing, '_post_settlement', lambda *a: answer)
    with billing.billing_scope('synthetic:turn'):
        assert billing.settle_receipt_sync(signed(), DIGEST)['charged_units'] == billing.MINIMUM_CHARGE_UNITS
        assert billing.billing_headers()['X-Olivia-Billing-Minimum'] == '1'


def test_minimum_is_not_accepted_when_usage_already_exceeds_it(configured, monkeypatch):
    big = signed(tokens=20000)
    answer = result(big)
    answer.update(charged_units=billing.MINIMUM_CHARGE_UNITS, debited_units=billing.MINIMUM_CHARGE_UNITS)
    monkeypatch.setattr(billing, '_post_settlement', lambda *a: answer)
    with billing.billing_scope('synthetic:turn'):
        with pytest.raises(ValueError, match='JEV_BILLING_RESPONSE_INVALID'):
            billing.settle_receipt_sync(big, DIGEST)


def test_no_scope_does_not_retroactively_charge_existing_receipt(configured, monkeypatch):
    monkeypatch.setattr(billing, '_post_settlement', lambda *a: pytest.fail('must not settle'))
    assert billing.settle_receipt_sync(signed(), DIGEST) is None
    assert billing.billing_headers() == {}


@pytest.mark.parametrize('status', [401, 402, 403, 409, 429, 502, 503, 504])
def test_relay_http_failure_keeps_status_without_private_body(configured, monkeypatch, status):
    import io
    import urllib.error
    def fail(*args):
        raise urllib.error.HTTPError('https://private.invalid', status, KEY, {}, io.BytesIO(KEY.encode()))
    monkeypatch.setattr(billing, '_post_settlement', fail)
    with billing.billing_scope('synthetic:turn'):
        with pytest.raises(ValueError, match='^JEV_BILLING_HTTP_' + str(status) + '$'):
            billing.settle_receipt_sync(signed(), DIGEST)


def test_key_not_in_sidecar_headers_repr_or_sanitized_errors(configured, monkeypatch):
    def fail(*args): raise RuntimeError(KEY)
    monkeypatch.setattr(billing, '_post_settlement', fail)
    with billing.billing_scope('synthetic:turn'):
        assert KEY not in repr(billing.CURRENT.get())
        assert KEY not in str(billing.billing_headers())
        with pytest.raises(ValueError) as error:
            billing.settle_receipt_sync(signed(), DIGEST)
        assert KEY not in str(error.value)
    monkeypatch.setattr(billing, '_account_key', fail)
    with pytest.raises(ValueError) as error:
        with billing.billing_scope('synthetic:turn'): pass
    assert str(error.value) == 'JEV_BILLING_ACCOUNT_UNAVAILABLE'


def test_scope_contract_length_and_nested_scope(configured):
    with pytest.raises(ValueError, match='JEV_BILLING_TURN_INVALID'):
        with billing.billing_scope('a'*129): pass
    with billing.billing_scope('synthetic:turn'):
        with billing.billing_scope('inner'):
            assert billing.CURRENT.get().turn_id == 'synthetic:turn'


def test_cancelled_waiter_clears_scope_without_retry_or_cancelling_inflight_wallet(configured, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def settle(key, receipt):
        calls.append((key, receipt))
        entered.set()
        assert release.wait(3)
        finished.set()
        return result(receipt)
    monkeypatch.setattr(billing, '_post_settlement', settle)
    async def scenario():
        async def operation():
            with billing.billing_scope('synthetic:turn'):
                await billing.settle_receipt(signed(), DIGEST)
        task = asyncio.create_task(operation())
        await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert billing.CURRENT.get() is None
        release.set()
        assert await asyncio.to_thread(finished.wait, 3)
    asyncio.run(scenario())
    assert len(calls) == 1


def test_qq_real_generate_wrapper_carries_revision_and_cleans_context(configured, monkeypatch):
    from runtime.personal_chat import backend
    seen = []
    async def generated(server, event, row):
        seen.append(billing.billing_headers())
        return 'synthetic'
    monkeypatch.setattr(backend, '_generate_billed', generated)
    value = asyncio.run(backend.generate(None, SimpleNamespace(exchange_id='qq-synthetic', channel='qq'), {'input_revision':3}))
    assert value == 'synthetic'
    assert seen[0]['X-Olivia-Turn-Id'] == 'personal-chat:qq-synthetic:3'
    assert seen[0]['X-Olivia-Account-Digest'] == hashlib.sha256(KEY.encode()).hexdigest()


def test_actual_letter_wrapper_carries_id_without_initializing_user_server(configured):
    # Compile only the checked-in wrapper: importing local_server initializes
    # application resources. The wrapper itself and imported billing helper are real.
    source = Path(__file__).resolve().parents[2] / 'local_server.py'
    tree = ast.parse(source.read_text(encoding='utf8'))
    wrapper = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'generate_reply')
    seen = []
    async def generated(letter_id, content, *, idempotency_key=None):
        seen.append((billing.billing_headers(), idempotency_key))
        return True
    namespace = {'_generate_reply_billed':generated,
                 'store':SimpleNamespace(letters=[{'letter_id':'synthetic-letter'}])}
    exec(compile(ast.Module(body=[wrapper], type_ignores=[]), str(source), 'exec'), namespace)
    assert asyncio.run(namespace['generate_reply']('synthetic-letter','hello',idempotency_key='retry-id'))
    assert seen[0][0]['X-Olivia-Turn-Id'] == 'letter:synthetic-letter'
    assert seen[0][1] == 'retry-id'


def test_account_key_missing_only_when_billing_needs_a_key(monkeypatch):
    monkeypatch.setenv('OLIVIA_JEV_BILLING_ENABLED', '0')
    monkeypatch.setattr(billing, '_account_key', lambda: None)
    assert billing.account_key_missing() is False
    monkeypatch.setenv('OLIVIA_JEV_BILLING_ENABLED', '1')
    for getter in (lambda: None, lambda: 'sk-other', lambda: 'olivia-', lambda: 1 / 0):
        monkeypatch.setattr(billing, '_account_key', getter)
        assert billing.account_key_missing() is True
    monkeypatch.setattr(billing, '_account_key', lambda: 'olivia-synthetic-account')
    assert billing.account_key_missing() is False


@pytest.mark.parametrize('version,rate,ok', [
    ('jev-input-cny-20261002-v2', 135, True),   # declared lower price
    ('jev-input-cny-20260928-v1', 150, True),   # a server that has not applied it yet
    ('jev-input-cny-20261002-v2', 150, False),  # price does not match its version
    ('jev-input-cny-20991231-v9', 135, False),  # unknown price version
])
def test_settlement_is_verified_against_its_declared_price_version(configured, monkeypatch, version, rate, ok):
    tokens = 17000
    answer = result(signed(tokens))
    answer.update(price_version=version, charged_units=tokens * rate, debited_units=tokens * rate)
    monkeypatch.setattr(billing, '_post_settlement', lambda *a: answer)
    assert billing.MINIMUM_HEADER['X-Olivia-JEV-Price'] == 'jev-input-cny-20261002-v2'
    with billing.billing_scope('synthetic:turn'):
        if ok:
            assert billing.settle_receipt_sync(signed(tokens), DIGEST)['charged_units'] == tokens * rate
        else:
            with pytest.raises(ValueError, match='JEV_BILLING_RESPONSE_INVALID'):
                billing.settle_receipt_sync(signed(tokens), DIGEST)
