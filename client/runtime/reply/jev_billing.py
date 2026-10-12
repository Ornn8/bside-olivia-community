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

from runtime.diagnostics.jev_pricing import CURRENT_PRICE_VERSION, INPUT_RATE_UNITS, PRICE_VERSION


@dataclass(frozen=True)
class BillingTurn:
    turn_id: str
    key: str = field(repr=False)


CURRENT = ContextVar('jev_billing_turn', default=None)
# Published CNY 0.01 minimum per charged judgment (1 yuan = 100,000,000 units).
# This header tells the relay the client verifies it; older clients are billed
# exact usage because they reject any other amount.
MINIMUM_CHARGE_UNITS = 1_000_000
MINIMUM_HEADER = {'X-Olivia-Billing-Minimum': '1', 'X-Olivia-JEV-Price': CURRENT_PRICE_VERSION}
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


def _settlement_opener():
    from .jev_http import shared_opener
    return shared_opener()


def _post_settlement(key, signed, *, version=None):
    from original_client_relay_api import RELAY_BASE
    body = json.dumps(signed, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', **MINIMUM_HEADER}
    if version:
        # A receipt settled after a restart keeps the billing version it was judged under.
        headers['X-Olivia-JEV-Price'] = version
    request = urllib.request.Request(RELAY_BASE + '/jev/settle', data=body, method='POST', headers=headers)
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


def _receipt_shape(billing):
    """The signed receipt when it has the form this client can settle, else None."""
    receipt = billing.get('receipt') if isinstance(billing, dict) else None
    if (not isinstance(receipt, dict) or receipt.get('v') != 1
            or receipt.get('price_version') != PRICE_VERSION
            or type(receipt.get('input_tokens')) is not int or not 0 <= receipt['input_tokens'] <= 1_000_000
            or not isinstance(receipt.get('operation_id'), str)
            or not isinstance(billing.get('signature'), str)):
        return None
    return receipt


def _verified_receipt(billing, expected_body_digest, turn):
    receipt = _receipt_shape(billing)
    if (receipt is None or receipt.get('turn_id') != turn.turn_id
            or receipt.get('account_key_digest') != hashlib.sha256(turn.key.encode()).hexdigest()
            or receipt.get('input_digest') != expected_body_digest):
        raise ValueError('JEV_BILLING_RECEIPT_INVALID')
    return receipt


def _settle_receipt_now(key, billing, turn_id, version=CURRENT_PRICE_VERSION):
    """Post one verified receipt and check the relay's answer against it."""
    receipt = billing['receipt']
    # Only the relay has the signing secret. A matching client-side shape does
    # not establish trust; its authenticated verification and transaction do.
    try:
        if version == CURRENT_PRICE_VERSION:
            result = _post_settlement(key, billing)
        else:
            result = _post_settlement(key, billing, version=version)
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
            or result.get('turn_id') != turn_id
            or result.get('operation_id') != receipt['operation_id']
            or rate is None
            or type(result.get('input_tokens')) is not int or result['input_tokens'] != receipt['input_tokens']
            or type(result.get('charged_units')) is not int or result['charged_units'] not in allowed
            or type(result.get('replayed')) is not bool
            or type(result.get('debited_units')) is not int
            or result['debited_units'] != (0 if result['replayed'] else result['charged_units'])):
        raise ValueError('JEV_BILLING_RESPONSE_INVALID')
    return result


_outbox = None


def _key_for_account(digest):
    try:
        key = _account_key() if callable(_account_key) else None
    except Exception:
        return None
    if (isinstance(key, str) and key.startswith('olivia-')
            and hashlib.sha256(key.encode()).hexdigest() == digest):
        return key
    return None


def _stored_receipt_valid(billing, turn_id, account):
    receipt = _receipt_shape(billing)
    return (receipt is not None and receipt.get('turn_id') == turn_id
            and receipt.get('account_key_digest') == account)


def configure_settlement_outbox(root, **options):
    """Settle receipts in the background from now on, resuming any left in ``root``."""
    global _outbox
    from .jev_settlement_outbox import SettlementOutbox
    shutdown_settlement_outbox()
    outbox = SettlementOutbox(root, _settle_receipt_now, _key_for_account, _stored_receipt_valid, **options)
    _outbox = outbox
    outbox.recover()
    return outbox


def shutdown_settlement_outbox(timeout=0.0):
    """Stop background settlement; queued receipts stay stored for the next start."""
    global _outbox
    outbox, _outbox = _outbox, None
    if outbox is not None:
        if timeout:
            outbox.flush(timeout)
        outbox.close(0.5 if timeout else 0.0)


def flush_settlements(timeout=3.0):
    """Wait for receipts that are due now, so balance and statement reads include them."""
    outbox = _outbox
    return True if outbox is None else outbox.flush(timeout)


async def flush_settlements_async(timeout=3.0):
    return await asyncio.to_thread(flush_settlements, timeout)


def settle_receipt_sync(billing, expected_body_digest):
    """Verify a signed receipt, then settle it without holding the reply.

    With a settlement outbox the verified receipt is stored and posted by a
    background worker, and this returns None. Without one (no state directory,
    it cannot be written, or the account has receipts the relay refuses or keeps
    failing) the receipt is settled in line.
    """
    turn = CURRENT.get()
    if turn is None:
        return None
    receipt = _verified_receipt(billing, expected_body_digest, turn)
    outbox = _outbox
    account = receipt['account_key_digest']
    # While earlier receipts are refused or keep failing, settle in line: the
    # relay's refusal or outage then fails the reply, as it always did.
    if outbox is not None and not outbox.congested(account):
        try:
            outbox.submit(turn_id=turn.turn_id, account=account, version=CURRENT_PRICE_VERSION,
                          signed=billing, key=turn.key)
        except OSError:
            pass
        else:
            return None
    result = _settle_receipt_now(turn.key, billing, turn.turn_id)
    if outbox is not None:
        outbox.expedite(account)
    return result


async def settle_receipt(billing, expected_body_digest):
    return await asyncio.to_thread(settle_receipt_sync, billing, expected_body_digest)
