"""The app resumes stored judgment receipts at start and stops the workers at exit."""
import asyncio
import hashlib
import json

from aiohttp import web

import local_server
from runtime.reply import jev_billing as billing

KEY = 'olivia-synthetic-startup-account'
DIGEST = 'b' * 64


def receipt():
    return {'signature': 'a' * 64, 'receipt': dict(
        v=1, turn_id='synthetic:startup', account_key_digest=hashlib.sha256(KEY.encode()).hexdigest(),
        input_digest=DIGEST, price_version=billing.PRICE_VERSION, input_tokens=17, operation_id='d' * 64)}


def settled(signed):
    data = signed['receipt']
    return dict(status='settled', turn_id=data['turn_id'], operation_id=data['operation_id'],
                price_version=data['price_version'], input_tokens=17, charged_units=2550,
                replayed=False, debited_units=2550)


def test_start_resumes_stored_receipts_and_stop_ends_the_workers(tmp_path, monkeypatch):
    monkeypatch.setenv('OLIVIA_JEV_BILLING_ENABLED', '1')
    monkeypatch.setattr(billing, '_account_key', lambda: KEY)
    monkeypatch.setattr(local_server, '_state_root', lambda: tmp_path)
    posted = []

    def unavailable(key, signed, version=None):
        raise OSError('synthetic: relay unreachable')

    monkeypatch.setattr(billing, '_post_settlement', unavailable)
    billing.configure_settlement_outbox(tmp_path)
    with billing.billing_scope('synthetic:startup'):
        assert billing.settle_receipt_sync(receipt(), DIGEST) is None
    billing.shutdown_settlement_outbox()
    stored = list((tmp_path / 'jev-settlements').rglob('*.json'))
    assert len(stored) == 1 and json.loads(stored[0].read_text(encoding='utf-8'))['signed'] == receipt()

    def reachable(key, signed, version=None):
        posted.append(key)
        return settled(signed)

    monkeypatch.setattr(billing, '_post_settlement', reachable)
    local_server._start_jev_settlement()
    assert billing._outbox is not None
    assert billing.flush_settlements(5) and not list((tmp_path / 'jev-settlements').rglob('*.json'))
    assert posted == [KEY]

    asyncio.run(local_server._stop_reply_tasks(web.Application()))
    assert billing._outbox is None


def test_without_a_state_directory_settlement_stays_in_line(monkeypatch):
    monkeypatch.setattr(local_server, '_state_root', lambda: None)
    local_server._start_jev_settlement()
    assert billing._outbox is None
