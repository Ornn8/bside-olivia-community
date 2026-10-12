"""Judgments return as soon as their receipt is stored; settlement follows durably."""
import asyncio
import hashlib
import http.client
import io
import json
import threading
import time
import urllib.error

import pytest

from runtime.diagnostics.reply_telemetry import TIMINGS
from runtime.reply import jev_billing as billing
from runtime.reply import jev_settlement_outbox as outbox_module
from tests.persona.test_jev_billing_helper import KEY, DIGEST, signed, result

OTHER_KEY = 'olivia-synthetic-other-account-key'


def receipt(number=1, key=KEY, turn='synthetic:turn'):
    value = signed()
    value['receipt'].update(operation_id='%064x' % number, turn_id=turn,
                            account_key_digest=hashlib.sha256(key.encode()).hexdigest())
    return value


def stored(root):
    return sorted(path for path in (root / 'jev-settlements').rglob('*.json')) if (root / 'jev-settlements').exists() else []


def wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class Relay:
    """A relay that commits each operation once and answers replays as replays."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls, self.debited, self.versions = [], [], []
        self.committed = set()
        self.lose_acknowledgement = 0
        self.fail = None
        self.gate = None
        self.entered = threading.Event()

    def post(self, key, signed_receipt, version=None):
        assert key in (KEY, OTHER_KEY)
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(5)
        if self.fail is not None:
            raise self.fail
        operation = signed_receipt['receipt']['operation_id']
        with self.lock:
            replayed = operation in self.committed
            self.committed.add(operation)
            answer = result(signed_receipt, replayed=replayed)
            self.calls.append(operation)
            self.debited.append(answer['debited_units'])
            self.versions.append(version)
            lost = self.lose_acknowledgement > 0
            self.lose_acknowledgement -= 1 if lost else 0
        if lost:
            raise http.client.RemoteDisconnected('synthetic: acknowledgement lost')
        return answer

    def rejection(self, status):
        return urllib.error.HTTPError('https://relay.invalid', status, 'synthetic', {}, io.BytesIO(b'{}'))


@pytest.fixture
def relay(monkeypatch, tmp_path):
    monkeypatch.setenv('OLIVIA_JEV_BILLING_ENABLED', '1')
    monkeypatch.setattr(billing, '_account_key', lambda: KEY)
    double = Relay()
    monkeypatch.setattr(billing, '_post_settlement', double.post)
    yield double
    if double.gate is not None:
        double.gate.set()
    billing.shutdown_settlement_outbox()


def start(root, **options):
    settings = dict(backoff=(0.05,), held_retry=60.0, other_account_retry=0.05)
    settings.update(options)
    return billing.configure_settlement_outbox(root, **settings)


def submit(value, turn='synthetic:turn', digest=DIGEST):
    with billing.billing_scope(turn):
        return billing.settle_receipt_sync(value, digest)


def test_answers_return_before_settlement_completes(relay, tmp_path):
    relay.gate = threading.Event()
    start(tmp_path)
    started = time.monotonic()
    assert submit(receipt()) is None
    assert time.monotonic() - started < 2
    assert not relay.gate.is_set() and relay.calls == []
    assert relay.entered.wait(3)  # The worker is posting while the caller already has its answer.
    relay.gate.set()
    assert wait_until(lambda: relay.calls == ['%064x' % 1] and not stored(tmp_path))
    assert relay.debited == [2550]


def test_receipt_is_stored_before_the_call_returns_and_holds_no_key(relay, tmp_path):
    relay.gate = threading.Event()
    start(tmp_path)
    submit(receipt())
    files = stored(tmp_path)
    assert len(files) == 1
    text = files[0].read_text(encoding='utf-8')
    data = json.loads(text)
    assert data['signed'] == receipt() and data['turn_id'] == 'synthetic:turn' and data['state'] == 'pending'
    assert KEY not in text and 'olivia-' not in text
    assert files[0].parent.name == hashlib.sha256(KEY.encode()).hexdigest()[:16]
    assert not list(files[0].parent.glob('.*.tmp'))


def test_settled_receipt_leaves_the_store_and_is_not_posted_again(relay, tmp_path):
    start(tmp_path)
    submit(receipt())
    assert wait_until(lambda: relay.calls and not stored(tmp_path))
    submit(receipt())  # Replayed judgment, same operation.
    billing.flush_settlements(2)
    time.sleep(0.1)
    assert relay.calls == ['%064x' % 1] and not stored(tmp_path)


def test_same_operation_queued_twice_is_posted_once(relay, tmp_path):
    relay.gate = threading.Event()
    start(tmp_path)
    submit(receipt())
    submit(receipt())
    assert len(stored(tmp_path)) == 1
    relay.gate.set()
    assert wait_until(lambda: not stored(tmp_path))
    time.sleep(0.1)
    assert relay.calls == ['%064x' % 1]


def test_pending_receipt_survives_a_restart_and_settles_exactly_once(relay, tmp_path):
    relay.fail = relay.rejection(503)
    start(tmp_path)
    submit(receipt())
    assert relay.entered.wait(3)
    billing.shutdown_settlement_outbox()  # The process ends with the receipt still unsettled.
    assert len(stored(tmp_path)) == 1 and relay.calls == []

    relay.fail = None
    start(tmp_path)  # The next start settles whatever is left.
    assert wait_until(lambda: relay.calls == ['%064x' % 1] and not stored(tmp_path))
    billing.shutdown_settlement_outbox()
    start(tmp_path)
    time.sleep(0.2)
    assert relay.calls == ['%064x' % 1] and relay.debited == [2550]


def test_restart_resumes_receipts_of_several_turns_in_one_pass(relay, tmp_path):
    relay.fail = relay.rejection(503)
    start(tmp_path)
    for number in range(1, 5):
        submit(receipt(number, turn='synthetic:turn-%d' % number), turn='synthetic:turn-%d' % number)
    billing.shutdown_settlement_outbox()
    assert len(stored(tmp_path)) == 4
    relay.fail = None
    start(tmp_path)
    assert wait_until(lambda: len(relay.calls) == 4 and not stored(tmp_path))
    assert sorted(relay.calls) == ['%064x' % number for number in range(1, 5)]


def test_lost_acknowledgement_is_replayed_without_a_second_charge(relay, tmp_path):
    relay.lose_acknowledgement = 1
    start(tmp_path)
    submit(receipt())
    assert wait_until(lambda: len(relay.calls) == 2 and not stored(tmp_path))
    assert relay.debited == [2550, 0]
    assert sum(relay.debited) == 2550


def test_lost_acknowledgement_across_a_restart_does_not_double_charge(relay, tmp_path):
    relay.lose_acknowledgement = 1
    relay.gate = None
    start(tmp_path, backoff=(30.0,))
    submit(receipt())
    assert wait_until(lambda: len(relay.calls) == 1)
    billing.shutdown_settlement_outbox()  # Committed by the relay, never acknowledged.
    assert len(stored(tmp_path)) == 1
    start(tmp_path)
    assert wait_until(lambda: len(relay.calls) == 2 and not stored(tmp_path))
    assert relay.debited == [2550, 0]


@pytest.mark.parametrize('field,value', [('turn_id', 'another-turn'), ('account_key_digest', 'a' * 64),
                                         ('input_digest', 'a' * 64), ('price_version', 'old-version'),
                                         ('input_tokens', -1), ('input_tokens', True), ('v', 2)])
def test_invalid_receipt_is_still_rejected_and_never_stored(relay, tmp_path, field, value):
    start(tmp_path)
    bad = receipt()
    bad['receipt'][field] = value
    with pytest.raises(ValueError, match='^JEV_BILLING_RECEIPT_INVALID$'):
        submit(bad)
    unsigned = receipt()
    unsigned.pop('signature')
    with pytest.raises(ValueError, match='^JEV_BILLING_RECEIPT_INVALID$'):
        submit(unsigned)
    with pytest.raises(ValueError, match='^JEV_BILLING_RECEIPT_INVALID$'):
        submit(None)
    billing.flush_settlements(1)
    assert relay.calls == [] and not stored(tmp_path)


def test_no_scope_stores_and_charges_nothing(relay, tmp_path):
    start(tmp_path)
    assert billing.settle_receipt_sync(receipt(), DIGEST) is None
    assert relay.calls == [] and not stored(tmp_path)


def test_unwritable_store_settles_in_line_instead(relay, tmp_path, monkeypatch):
    def refuse(*args):
        raise OSError('synthetic: disk full')
    monkeypatch.setattr(outbox_module, '_write', refuse)
    start(tmp_path)
    answer = submit(receipt())
    assert answer['charged_units'] == 2550 and relay.calls == ['%064x' % 1]


def test_without_an_outbox_settlement_stays_in_line(relay):
    answer = submit(receipt())
    assert answer['replayed'] is False and relay.calls == ['%064x' % 1]


def test_rejection_makes_the_next_judgment_settle_in_line_like_before(relay, tmp_path):
    relay.fail = relay.rejection(402)
    start(tmp_path)
    assert submit(receipt(1)) is None  # The judgment that ran into it has already been answered.
    assert wait_until(lambda: billing._outbox.held() == 1)
    with pytest.raises(ValueError, match='^JEV_BILLING_HTTP_402$'):
        submit(receipt(2))
    # The reply that failed is not charged later; the one already answered still is.
    assert len(stored(tmp_path)) == 1
    relay.fail = None  # Balance topped up.
    answer = submit(receipt(3))
    assert answer['charged_units'] == 2550 and answer['replayed'] is False
    assert wait_until(lambda: sorted(relay.calls) == ['%064x' % n for n in (1, 3)] and not stored(tmp_path))
    assert sum(relay.debited) == 2 * 2550
    assert submit(receipt(4)) is None  # Back to answering ahead of settlement.


@pytest.mark.parametrize('status', [401, 403, 409])
def test_other_hard_rejections_settle_in_line_and_stay_stored(relay, tmp_path, status):
    relay.fail = relay.rejection(status)
    start(tmp_path)
    submit(receipt(1))
    assert wait_until(lambda: billing._outbox.held() == 1)
    with pytest.raises(ValueError, match='^JEV_BILLING_HTTP_%d$' % status):
        submit(receipt(2))
    files = stored(tmp_path)
    assert len(files) == 1 and json.loads(files[0].read_text(encoding='utf-8'))['state'] == 'held'


def test_mismatching_relay_answer_is_held_not_dropped(relay, tmp_path, monkeypatch):
    def wrong(key, signed_receipt, version=None):
        answer = result(signed_receipt)
        answer['charged_units'] += 1
        relay.calls.append(signed_receipt['receipt']['operation_id'])
        return answer
    monkeypatch.setattr(billing, '_post_settlement', wrong)
    start(tmp_path)
    submit(receipt())
    assert wait_until(lambda: billing._outbox.held() == 1)
    with pytest.raises(ValueError, match='^JEV_BILLING_RESPONSE_INVALID$'):
        submit(receipt(2))
    assert len(stored(tmp_path)) == 1


def test_judgments_stop_running_ahead_when_settlements_keep_failing(relay, tmp_path):
    relay.fail = relay.rejection(503)
    start(tmp_path, max_stuck=3)
    for number in range(1, 4):
        assert submit(receipt(number)) is None  # An outage does not stop the first few replies.
    assert wait_until(lambda: billing._outbox._entries and all(e.attempts for e in billing._outbox._entries.values()))
    with pytest.raises(ValueError, match='^JEV_BILLING_HTTP_503$'):
        submit(receipt(4))  # Beyond that, a failed settlement fails the reply again.
    assert len(stored(tmp_path)) == 3
    relay.fail = None
    answer = submit(receipt(5))
    assert answer['charged_units'] == 2550
    assert wait_until(lambda: sorted(relay.calls) == ['%064x' % n for n in (1, 2, 3, 5)] and not stored(tmp_path))
    assert sum(relay.debited) == 4 * 2550


@pytest.mark.parametrize('failure', [lambda r: r.rejection(503), lambda r: r.rejection(500),
                                     lambda r: TimeoutError('synthetic'), lambda r: ConnectionResetError('x')])
def test_transient_failures_are_retried_until_settled_without_blocking(relay, tmp_path, failure):
    relay.fail = failure(relay)
    start(tmp_path)
    assert submit(receipt(1)) is None
    assert wait_until(lambda: relay.entered.is_set())
    assert submit(receipt(2)) is None  # A relay outage does not stop judgments.
    time.sleep(0.15)
    assert len(stored(tmp_path)) == 2
    relay.fail = None
    assert wait_until(lambda: set(relay.calls) == {'%064x' % 1, '%064x' % 2} and not stored(tmp_path))
    assert sum(relay.debited) == 2 * 2550


def test_receipt_of_another_account_waits_until_its_account_is_back(relay, tmp_path, monkeypatch):
    monkeypatch.setattr(billing, '_account_key', lambda: OTHER_KEY)
    relay.fail = relay.rejection(503)
    start(tmp_path)
    submit(receipt(1, key=OTHER_KEY))
    assert relay.entered.wait(3)
    billing.shutdown_settlement_outbox()
    relay.fail = None
    monkeypatch.setattr(billing, '_account_key', lambda: KEY)  # A different account is signed in now.
    start(tmp_path)
    time.sleep(0.3)
    assert relay.calls == [] and len(stored(tmp_path)) == 1
    assert stored(tmp_path)[0].parent.name == hashlib.sha256(OTHER_KEY.encode()).hexdigest()[:16]
    monkeypatch.setattr(billing, '_account_key', lambda: OTHER_KEY)
    assert wait_until(lambda: relay.calls == ['%064x' % 1] and not stored(tmp_path))


def test_receipt_keeps_the_billing_version_it_was_judged_under(relay, tmp_path):
    relay.fail = relay.rejection(503)
    start(tmp_path)
    submit(receipt(1))
    assert relay.entered.wait(3)
    billing.shutdown_settlement_outbox()
    path = stored(tmp_path)[0]
    data = json.loads(path.read_text(encoding='utf-8'))
    data['billing_version'] = 'jev-input-cny-20261002-v2'
    path.write_text(json.dumps(data), encoding='utf-8')
    relay.fail = None
    start(tmp_path)
    assert wait_until(lambda: relay.calls and not stored(tmp_path))
    assert relay.versions == ['jev-input-cny-20261002-v2']
    submit(receipt(2))
    assert wait_until(lambda: len(relay.calls) == 2)
    assert relay.versions[1] is None  # The current version is the default header.


def test_unreadable_or_foreign_files_are_left_alone(relay, tmp_path):
    folder = tmp_path / 'jev-settlements' / ('a' * 16)
    folder.mkdir(parents=True)
    (folder / ('f' * 64 + '.json')).write_text('{broken', encoding='utf-8')
    (folder / 'note.txt').write_text('keep', encoding='utf-8')
    start(tmp_path)
    time.sleep(0.15)
    assert relay.calls == [] and (folder / ('f' * 64 + '.json')).exists() and (folder / 'note.txt').exists()


def test_flush_waits_for_the_settlement_a_balance_read_must_include(relay, tmp_path):
    relay.gate = threading.Event()
    start(tmp_path)
    submit(receipt())
    assert relay.entered.wait(3)
    started = time.monotonic()
    assert billing.flush_settlements(0.1) is False and time.monotonic() - started < 1
    threading.Timer(0.2, relay.gate.set).start()
    assert asyncio.run(billing.flush_settlements_async(3)) is True
    assert relay.calls == ['%064x' % 1]
    billing.shutdown_settlement_outbox()
    assert billing.flush_settlements(1) is True


def test_shutdown_lets_due_receipts_finish_and_keeps_the_rest(relay, tmp_path):
    start(tmp_path)
    submit(receipt(1))
    billing.shutdown_settlement_outbox(2.0)
    assert relay.calls == ['%064x' % 1] and not stored(tmp_path)
    relay.fail = relay.rejection(503)
    start(tmp_path)
    submit(receipt(2))
    assert relay.entered.wait(3)
    billing.shutdown_settlement_outbox(0.0)
    assert len(stored(tmp_path)) == 1


def test_workers_are_daemons_and_do_not_pile_up(relay, tmp_path):
    before = set(threading.enumerate())
    start(tmp_path)
    for number in range(1, 30):
        submit(receipt(number))
    assert wait_until(lambda: len(set(relay.calls)) == 29 and not stored(tmp_path))
    workers = [t for t in threading.enumerate() if t.name.startswith('jev-settlement-') and t not in before]
    assert 0 < len(workers) <= 2 and all(t.daemon for t in workers)
    billing.shutdown_settlement_outbox(1.0)
    assert wait_until(lambda: not any(t.is_alive() for t in workers))


@pytest.mark.parametrize('kind', ['companion', 'persona', 'exchange', 'world', 'proactive', 'questions', 'questions_sync'])
def test_every_call_site_returns_validated_answers_before_settlement(monkeypatch, tmp_path, kind):
    from runtime.reply import companion_decision as companion
    from runtime.reply import companion_duties as duties
    from runtime.reply import companion_proactive as proactive
    from runtime.reply import jev_questions as questions
    from tests.persona.test_jev_transport_billing import transport

    account_digest = hashlib.sha256(KEY.encode()).hexdigest()
    turn_id = 'synthetic:transport:9'

    def make_receipt(request):
        return {'signature': 'synthetic-signed-receipt', 'receipt': {
            'v': 1, 'turn_id': turn_id, 'account_key_digest': account_digest,
            'input_digest': hashlib.sha256(request.data).hexdigest(),
            'price_version': billing.PRICE_VERSION, 'input_tokens': 17,
            'operation_id': hashlib.sha256(request.data).hexdigest(),
        }}

    run, calls, _, _ = transport(monkeypatch, kind, receipt=make_receipt)
    for module in (companion, duties, proactive, questions):
        monkeypatch.setattr(module, 'billing_headers', billing.billing_headers, raising=False)
        monkeypatch.setattr(module, 'settle_receipt', billing.settle_receipt, raising=False)
        monkeypatch.setattr(module, 'settle_receipt_sync', billing.settle_receipt_sync, raising=False)
    monkeypatch.setenv('OLIVIA_JEV_BILLING_ENABLED', '1')
    monkeypatch.setattr(billing, '_account_key', lambda: KEY)
    double = Relay()
    double.gate = threading.Event()
    monkeypatch.setattr(billing, '_post_settlement', double.post)
    row = {}
    token = TIMINGS.set(row)
    try:
        start(tmp_path)
        with billing.billing_scope(turn_id):
            outcome = run()
    finally:
        TIMINGS.reset(token)
    try:
        assert outcome == {'choice': 'yes'} if kind.startswith('questions') else outcome.error_code is None
        assert not double.gate.is_set() and len(stored(tmp_path)) == 1
        assert double.entered.wait(3)
        if kind.startswith('questions'):
            assert row['reply_timing_seconds']['jev_settlement'] < 1
            assert row['reply_timing_seconds']['jev_http'] >= 0
    finally:
        double.gate.set()
    assert wait_until(lambda: len(double.calls) == 1 and not stored(tmp_path))
    assert double.calls == [hashlib.sha256(calls[0].data).hexdigest()]
    billing.shutdown_settlement_outbox()
