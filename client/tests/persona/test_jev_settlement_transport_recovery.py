"""A lost wallet acknowledgement must replay the same signed operation."""
from email.message import Message
import http.client
import io
import json
import ssl
import urllib.error

import pytest

from runtime.reply import jev_billing as billing
from tests.persona.test_jev_billing_helper import KEY, DIGEST, signed, result


def settlement_transport(monkeypatch, failures, *, malformed=False):
    monkeypatch.setenv('OLIVIA_JEV_BILLING_ENABLED', '1')
    monkeypatch.setattr(billing, '_account_key', lambda: KEY)
    monkeypatch.setattr('runtime.remote_generation.gpu_tls_context', lambda: None)
    calls, debits = [], []

    class Response:
        status = 200
        headers = Message()
        headers['Content-Type'] = 'application/json'

        def __init__(self, value):
            self.body = json.dumps(value).encode()

        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, limit): return self.body[:limit]

    class Opener:
        def open(self, request, **kwargs):
            calls.append((request.data, dict(request.header_items()), kwargs))
            value = json.loads(request.data)
            # The server commits before the client learns whether it succeeded.
            replayed = bool(debits)
            answer = result(value, replayed=replayed)
            debits.append(answer['debited_units'])
            if len(calls) <= len(failures):
                raise failures[len(calls)-1]
            response = Response(answer)
            if malformed:
                response.body = b'{PRIVATE invalid JSON'
            return response

    monkeypatch.setattr(billing.urllib.request, 'build_opener', lambda *_: Opener())
    if hasattr(billing, 'time'):
        monkeypatch.setattr(billing.time, 'sleep', lambda *_: None)
    return calls, debits


def test_lost_acknowledgement_replays_same_operation_without_second_debit(monkeypatch):
    calls, debits = settlement_transport(monkeypatch, [http.client.RemoteDisconnected('PRIVATE upstream detail')])
    with billing.billing_scope('synthetic:turn'):
        answer = billing.settle_receipt_sync(signed(), DIGEST)
    assert answer['replayed'] is True and answer['debited_units'] == 0
    assert debits == [2550, 0]
    assert len(calls) == 2 and calls[0] == calls[1]


def test_repeated_connection_failure_stops_after_three_attempts(monkeypatch):
    calls, debits = settlement_transport(monkeypatch, [TimeoutError('PRIVATE') for _ in range(4)])
    with billing.billing_scope('synthetic:turn'):
        with pytest.raises(ValueError, match='^JEV_BILLING_UNAVAILABLE$'):
            billing.settle_receipt_sync(signed(), DIGEST)
    assert len(calls) == 3 and debits == [2550, 0, 0]


@pytest.mark.parametrize('status', [400, 401, 402, 403, 409])
def test_terminal_http_rejection_does_not_retry(monkeypatch, status):
    failure = urllib.error.HTTPError('https://synthetic.invalid', status, 'PRIVATE', {}, io.BytesIO(b'PRIVATE'))
    calls, _ = settlement_transport(monkeypatch, [failure])
    code = 'JEV_BILLING_UNAVAILABLE' if status == 400 else 'JEV_BILLING_HTTP_' + str(status)
    with billing.billing_scope('synthetic:turn'):
        with pytest.raises(ValueError, match='^' + code + '$'):
            billing.settle_receipt_sync(signed(), DIGEST)
    assert len(calls) == 1


def test_invalid_json_response_is_terminal(monkeypatch):
    calls, _ = settlement_transport(monkeypatch, [], malformed=True)
    with billing.billing_scope('synthetic:turn'):
        with pytest.raises(ValueError, match='^JEV_BILLING_UNAVAILABLE$'):
            billing.settle_receipt_sync(signed(), DIGEST)
    assert len(calls) == 1


def test_untrusted_tls_certificate_is_terminal(monkeypatch):
    error = urllib.error.URLError(ssl.SSLCertVerificationError('PRIVATE'))
    calls, _ = settlement_transport(monkeypatch, [error])
    with billing.billing_scope('synthetic:turn'):
        with pytest.raises(ValueError, match='^JEV_BILLING_UNAVAILABLE$'):
            billing.settle_receipt_sync(signed(), DIGEST)
    assert len(calls) == 1
