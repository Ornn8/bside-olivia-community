"""Turn-bound JEV settlement. Only signed provider receipts reach the wallet."""
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

from runtime.diagnostics.jev_pricing import DISCOUNT_PRICE_VERSION, INPUT_RATE_UNITS, PRICE_VERSION


@dataclass(frozen=True)
class BillingTurn:
    turn_id: str
    key: str = field(repr=False)


CURRENT = ContextVar('jev_billing_turn', default=None)
# Published CNY 0.01 minimum per charged judgment (1 yuan = 100,000,000 units).
# This header tells the relay the client verifies it; older clients are billed
# exact usage because they reject any other amount.
MINIMUM_CHARGE_UNITS = 1_000_000
MINIMUM_HEADER = {'X-Olivia-Billing-Minimum': '1', 'X-Olivia-JEV-Price': DISCOUNT_PRICE_VERSION}
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
def billing_scope(turn_id):
    if os.environ.get('OLIVIA_JEV_BILLING_ENABLED') != '1' or CURRENT.get() is not None:
        yield
        return
    if not isinstance(turn_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', turn_id):
        raise ValueError('JEV_BILLING_TURN_INVALID')
    try:
        key = _account_key() if callable(_account_key) else None
    except Exception:
        raise ValueError('JEV_BILLING_ACCOUNT_UNAVAILABLE') from None
    if (not isinstance(key, str) or not key.startswith('olivia-')
            or len(key) <= len('olivia-') or any(c.isspace() for c in key)):
        raise ValueError('JEV_BILLING_ACCOUNT_UNAVAILABLE')
    token = CURRENT.set(BillingTurn(turn_id, key))
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


def _post_settlement(key, signed):
    from original_client_relay_api import RELAY_BASE
    from runtime.remote_generation import gpu_tls_context
    from .companion_decision import _NoRedirect
    body = json.dumps(signed, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    request = urllib.request.Request(RELAY_BASE + '/jev/settle', data=body, method='POST',
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', **MINIMUM_HEADER})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                        urllib.request.HTTPSHandler(context=gpu_tls_context()))
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
    rate = INPUT_RATE_UNITS.get(result.get('price_version')) if isinstance(result, dict) else None
    amount = receipt['input_tokens'] * (rate or 0)
    # Exact usage, or the published minimum when usage is below it. A charge
    # that is neither is a real mismatch, reported as such (not "unavailable").
    allowed = {amount} | ({MINIMUM_CHARGE_UNITS} if 0 < amount < MINIMUM_CHARGE_UNITS else set())
    if (not isinstance(result, dict) or result.get('status') != 'settled'
            or result.get('turn_id') != turn.turn_id
            or result.get('operation_id') != receipt['operation_id']
            or rate is None
            or type(result.get('input_tokens')) is not int or result['input_tokens'] != receipt['input_tokens']
            or type(result.get('charged_units')) is not int or result['charged_units'] not in allowed
            or type(result.get('replayed')) is not bool
            or type(result.get('debited_units')) is not int
            or result['debited_units'] != (0 if result['replayed'] else result['charged_units'])):
        raise ValueError('JEV_BILLING_RESPONSE_INVALID')
    return result


async def settle_receipt(billing, expected_body_digest):
    return await asyncio.to_thread(settle_receipt_sync, billing, expected_body_digest)
