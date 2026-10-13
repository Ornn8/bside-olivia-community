"""Frozen proactive proposals; the caller retains scheduling and send authority."""
import asyncio
import hashlib
import http.client
import json
import os
from pathlib import Path
import urllib.error

from jsonschema import Draft202012Validator, ValidationError

from runtime.reply.jev_billing import settle_receipt

from runtime.reply.companion_decision import CompanionDecisionError, DEFAULT_ENDPOINT, ERROR_CODES, _json
from runtime.reply.companion_duties import (
    FrozenCompanionDutyResult, JevDutiesPort, _digest, _index, _json_value, _require, _stamp,
)


_SCHEMA_BYTES = Path(__file__).with_name('companion_proactive_schema.json').read_bytes()
_SCHEMA = json.loads(_SCHEMA_BYTES)
SCHEMA_DIGEST = hashlib.sha256(_SCHEMA_BYTES).hexdigest()
_INPUT = Draft202012Validator(_SCHEMA['request'])
_OUTPUT = Draft202012Validator(_SCHEMA['response'])
from runtime.reply.jev_limits import JEV_MAX_INPUT_BYTES as _MAX_INPUT_BYTES


def _validate_input(value):
    _json_value(value)
    _INPUT.validate(value)
    _stamp(value['as_of'])
    _index(value['opportunities'], 'id')
    _require(type(value['contact']['unanswered_count']) is int)


def _media(value):
    # By default a letter is written text and contact on a chat channel is a voice
    # message only; the cloud may set the media each channel starts with.
    from runtime import proactive_config
    kept = proactive_config.media('letter' if value['channel'] == 'letter' else 'qq')
    return [medium for medium in value['available_media'] if medium in kept]


def _local_defer(value):
    gates = value['hard_gates']
    reason = ('paused' if gates['paused'] else 'hard_gate' if gates['blocked_reasons']
              else 'no_medium' if not _media(value) else None)
    return (dict(action='defer', opportunity_id=None, reason=reason, intent=None, medium=None)
            if reason is not None else None)


def _validate_response(value, response, input_digest):
    _json_value(response)
    _OUTPUT.validate(response)
    _require(response['input_digest'] == input_digest)
    _require(type(response['api_calls']) is int and type(response['usage']['input_tokens']) is int)
    decision = response['decision']
    if decision['action'] == 'defer':
        return
    _require(_local_defer(value) is None and decision['medium'] in _media(value))
    if decision['opportunity_id'] is None:
        _require(value['relationship']['tier'] in {'close', 'committed'}
                 and decision['intent'] == 'connection' and decision['reason'] == 'spontaneous_connection')
    else:
        offered = {item['id']: item for item in value['opportunities']}
        _require(decision['opportunity_id'] in offered)
        _require(decision['intent'] == offered[decision['opportunity_id']]['kind']
                 and decision['reason'] == 'opportunity')


class JevProactivePort:
    def __init__(self, endpoint=DEFAULT_ENDPOINT, *, token='', timeout_seconds=50):
        self._transport = JevDutiesPort(endpoint, token=token, timeout_seconds=timeout_seconds)

    def _request(self, input_json):
        return self._transport._request('proactive', input_json)

    async def evaluate(self, packet):
        try:
            _validate_input(packet)
            encoded = _json(packet)
            if len(encoded.encode('utf-8')) > _MAX_INPUT_BYTES:
                return FrozenCompanionDutyResult(error_code='JEV_INPUT_TOO_LARGE', schema_digest=SCHEMA_DIGEST)
            value = json.loads(encoded)
            input_digest = _digest(encoded)
        except (ValueError, TypeError, KeyError, ValidationError, OverflowError, RecursionError):
            return FrozenCompanionDutyResult(error_code='JEV_INPUT_INVALID', schema_digest=SCHEMA_DIGEST)
        local = _local_defer(value)
        if local is not None:
            # No synthetic backend/model envelope: this is a local hard-gate result.
            return FrozenCompanionDutyResult(decision_json=_json(local), input_digest=input_digest,
                input_json=encoded, schema_digest=SCHEMA_DIGEST)
        try:
            response = await asyncio.to_thread(self._request, encoded)
            billing = response.pop('billing', None) if isinstance(response, dict) else None
            try:
                _validate_response(value, response, input_digest)
                response_json = _json(response)
            except (ValueError, TypeError, KeyError, ValidationError, OverflowError, RecursionError):
                raise CompanionDecisionError('JEV_RESPONSE_INVALID') from None
            await settle_receipt(billing, input_digest)
            return FrozenCompanionDutyResult(decision_json=_json(response['decision']), input_digest=input_digest,
                input_json=encoded, response_json=response_json, response_digest=_digest(response_json),
                schema_digest=SCHEMA_DIGEST)
        except CompanionDecisionError as error:
            code = error.code
        except urllib.error.HTTPError as error:
            code = f'JEV_HTTP_{error.code}'
            if code not in ERROR_CODES:
                code = 'JEV_HTTP_ERROR'
            error.close()
        except TimeoutError:
            code = 'JEV_TIMEOUT'
        except urllib.error.URLError as error:
            code = 'JEV_TIMEOUT' if isinstance(error.reason, TimeoutError) else 'JEV_UNAVAILABLE'
        except (OSError, ValueError, TypeError, http.client.HTTPException):
            code = 'JEV_UNAVAILABLE'
        return FrozenCompanionDutyResult(input_digest=input_digest, input_json=encoded, error_code=code,
                                         schema_digest=SCHEMA_DIGEST)


def configured_proactive():
    endpoint = os.environ.get('OLIVIA_JEV_DECISION_URL', '').strip()
    if not endpoint:
        return None
    return JevProactivePort(endpoint, token=os.environ.get('COMPANION_CLASSIFIER_TOKEN', ''))
