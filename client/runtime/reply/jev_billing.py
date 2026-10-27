"""Turn-bound JEV settlement. Only signed provider receipts reach the wallet."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import asyncio
import hashlib
import http.client
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request

from runtime import contact_context
from runtime.diagnostics.jev_pricing import PRICE_VERSION


@dataclass(frozen=True)
class BillingTurn:
    turn_id: str
    key: str = field(repr=False)
    # A QQ turn settles its judgments beside its next stages and confirms them before the reply goes out
    # (2026-10-24: ~0.4 s a judgment, 2-3.7 s a turn, all of it on the reply's path). None: settle at once.
    pending: list | None = field(default=None, compare=False, repr=False)


_SETTLEMENTS = ThreadPoolExecutor(max_workers=4, thread_name_prefix='jev-settle')


CURRENT = ContextVar('jev_billing_turn', default=None)
# The relay alone prices a judgment. It debits the signed receipt when the judgment
# answers, before this client sees it, so recounting the amount here could only fail
# a turn already paid for. This version tells the relay the client takes the amount
# it charged; the client checks that the settlement is this judgment's, debited once.
SERVER_PRICE_VERSION = 'jev-input-cny-20261027-v7'
MINIMUM_HEADER = {'X-Olivia-Billing-Minimum': '1', 'X-Olivia-JEV-Price': SERVER_PRICE_VERSION}
_account_key = None


def configure_account(get_key):
    global _account_key
    def stripped():
        # A key pasted with a trailing space or newline still works on the relay;
        # it must not make every reply fail here with ACCOUNT_UNAVAILABLE.
        key = get_key()
        return key.strip() if isinstance(key, str) else key
    _account_key = stripped


def account_key_missing():
    """True when replies need the Olivia account key and none is usable."""
    if os.environ.get('OLIVIA_JEV_BILLING_ENABLED') != '1':
        return False
    try:
        key = _account_key() if callable(_account_key) else None
    except Exception:
        return True
    return (not isinstance(key, str) or not key.startswith('olivia-')
            or len(key) <= len('olivia-') or any(c.isspace() for c in key))


@contextmanager
def billing_scope(turn_id, *, deferred=False):
    if os.environ.get('OLIVIA_JEV_BILLING_ENABLED') != '1' or CURRENT.get() is not None:
        yield
        return
    # Every judgment made for a cloud contact settles under the contact's own id.
    turn_id = contact_context.current() or turn_id
    if not isinstance(turn_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', turn_id):
        raise ValueError('JEV_BILLING_TURN_INVALID')
    try:
        key = _account_key() if callable(_account_key) else None
    except Exception:
        raise ValueError('JEV_BILLING_ACCOUNT_UNAVAILABLE') from None
    if (not isinstance(key, str) or not key.startswith('olivia-')
            or len(key) <= len('olivia-') or any(c.isspace() for c in key)):
        raise ValueError('JEV_BILLING_ACCOUNT_UNAVAILABLE')
    token = CURRENT.set(BillingTurn(turn_id, key, pending=[] if deferred else None))
    try:
        yield
    finally:
        CURRENT.reset(token)


def billing_headers():
    turn = CURRENT.get()
    if turn is None:
        return {}
    return {'X-Olivia-Account-Digest': hashlib.sha256(turn.key.encode()).hexdigest(),
            'X-Olivia-Turn-Id': turn.turn_id, **MINIMUM_HEADER}


def cloud_request_headers(body_digest):
    turn = CURRENT.get()
    try:
        key = turn.key if turn is not None else _account_key() if callable(_account_key) else None
    except Exception:
        raise ValueError('JEV_BILLING_ACCOUNT_UNAVAILABLE') from None
    if (not isinstance(key, str) or not key.startswith('olivia-')
            or len(key) <= 7 or any(c.isspace() for c in key)):
        raise ValueError('JEV_BILLING_ACCOUNT_UNAVAILABLE')
    return {'Authorization': 'Bearer ' + key,
            'X-Olivia-Account-Digest': hashlib.sha256(key.encode()).hexdigest(),
            'X-Olivia-Turn-Id': turn.turn_id if turn is not None else 'jev:' + body_digest,
            **MINIMUM_HEADER}


def _settlement_opener():
    """Keep-alive connections shared with the judgments: direct, no redirects, the client's TLS context."""
    from .jev_http import shared_opener
    return shared_opener()


def _post_settlement(key, signed):
    from original_client_relay_api import RELAY_BASE
    from runtime import ui_language
    body = json.dumps(signed, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    request = urllib.request.Request(RELAY_BASE + '/jev/settle', data=body, method='POST',
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', **MINIMUM_HEADER,
                 **ui_language.headers()})
    opener = _settlement_opener()
    # A wallet commit can succeed while its acknowledgement is lost. Replay
    # the identical signed operation; the relay never debits it twice.
    for attempt in range(3):
        try:
            with opener.open(request, timeout=20) as response:
                if response.status != 200 or response.headers.get_content_type() != 'application/json':
                    raise ValueError('JEV_BILLING_RESPONSE_INVALID')
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError('JEV_BILLING_RESPONSE_INVALID')
            return json.loads(raw)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            if attempt == 2 or isinstance(getattr(exc, 'reason', None), ssl.SSLCertVerificationError):
                raise
            time.sleep(.25 * (attempt + 1))


def settle_receipt_sync(billing, expected_body_digest):
    turn = CURRENT.get()
    if turn is None:
        return None
    receipt = billing.get('receipt') if isinstance(billing, dict) else None
    if (not isinstance(receipt, dict) or receipt.get('v') != 1
            or receipt.get('turn_id') != turn.turn_id
            or receipt.get('account_key_digest') != billing_headers()['X-Olivia-Account-Digest']
            or receipt.get('input_digest') != expected_body_digest
            or receipt.get('price_version') != PRICE_VERSION
            or type(receipt.get('input_tokens')) is not int or not 0 <= receipt['input_tokens'] <= 1_000_000
            or not isinstance(receipt.get('operation_id'), str)
            or not isinstance(billing.get('signature'), str)):
        raise ValueError('JEV_BILLING_RECEIPT_INVALID')
    if turn.pending is not None:
        turn.pending.append(_SETTLEMENTS.submit(_settle, turn, billing, receipt))
        return None
    return _settle(turn, billing, receipt)


def _settle(turn, billing, receipt):
    # Only the relay has the signing secret. A matching client-side shape does
    # not establish trust; its authenticated verification and transaction do.
    try:
        result = _post_settlement(turn.key, billing)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        code = ('JEV_BILLING_HTTP_' + str(status) if status in (401, 402, 403, 409, 429, 502, 503, 504)
                else 'JEV_BILLING_UNAVAILABLE')
        raise ValueError(code) from None
    except ValueError as exc:
        if str(exc) == 'JEV_BILLING_RESPONSE_INVALID':
            raise
        raise ValueError('JEV_BILLING_UNAVAILABLE') from None
    except Exception:
        # Do not expose provider bodies, tokens or signed receipt contents.
        raise ValueError('JEV_BILLING_UNAVAILABLE') from None
    if (not isinstance(result, dict) or result.get('status') != 'settled'
            or result.get('turn_id') != turn.turn_id
            or result.get('operation_id') != receipt['operation_id']
            or not isinstance(result.get('price_version'), str)
            or type(result.get('input_tokens')) is not int or result['input_tokens'] != receipt['input_tokens']
            or type(result.get('charged_units')) is not int or result['charged_units'] < 0
            or type(result.get('replayed')) is not bool
            or type(result.get('debited_units')) is not int
            or result['debited_units'] != (0 if result['replayed'] else result['charged_units'])):
        raise ValueError('JEV_BILLING_RESPONSE_INVALID')
    return result


async def settle_receipt(billing, expected_body_digest):
    return await asyncio.to_thread(settle_receipt_sync, billing, expected_body_digest)


def _confirm_settlements(pending):
    failures = []
    for future in pending:
        try:
            future.result()
        except Exception as exc:
            failures.append(exc)
    if failures:
        raise failures[0]


async def confirm_settlements():
    """Every deferred settlement of this turn has gone through; the first failure fails the turn, as it did when
    each judgment waited for its own."""
    turn = CURRENT.get()
    if turn is None or not turn.pending:
        return
    pending = list(turn.pending)
    del turn.pending[:len(pending)]
    await asyncio.to_thread(_confirm_settlements, pending)
