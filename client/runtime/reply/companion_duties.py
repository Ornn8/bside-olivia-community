"""Source-bound Jev proposals; canonical persona and experience authority stays local.

The schema is copied byte-for-byte from the v2 development sidecar contract.
This module never writes a ledger, promotes preferences, or executes a proposal.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import urllib.error
import urllib.request

from jsonschema import Draft202012Validator, ValidationError

from runtime.reply.jev_billing import settle_receipt

from runtime.reply.companion_decision import (
    CompanionDecisionError, DEFAULT_ENDPOINT, ERROR_CODES, JevDecisionPort,
    _json,
)


_SCHEMA_BYTES = Path(__file__).with_name('companion_duties_schema.json').read_bytes()
_SCHEMA = json.loads(_SCHEMA_BYTES)
SCHEMA_DIGEST = hashlib.sha256(_SCHEMA_BYTES).hexdigest()
_KINDS = ('persona', 'exchange', 'world')
_REQUESTS = {kind: Draft202012Validator(_SCHEMA[kind + '_request']) for kind in _KINDS}
_RESPONSES = {kind: Draft202012Validator(_SCHEMA[kind + '_response']) for kind in _KINDS}
from runtime.reply.jev_limits import JEV_MAX_INPUT_BYTES as _MAX_INPUT_BYTES


def _digest(encoded):
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def _require(condition):
    if not condition:
        raise ValueError('invalid companion duty binding')


def _json_value(value):
    """Do not coerce non-JSON mapping keys, tuples, floats or custom objects."""
    if type(value) is dict:
        _require(all(type(key) is str for key in value))
        for item in value.values():
            _json_value(item)
    elif type(value) is list:
        for item in value:
            _json_value(item)
    elif type(value) is float:
        _require(math.isfinite(value))
    else:
        _require(value is None or type(value) in (str, bool, int))


def _index(items, *keys):
    indexed = {tuple(item[key] for key in keys): item for item in items}
    _require(len(indexed) == len(items))
    return indexed


def _stamp(value):
    # Schema enforces the RFC3339 spelling; datetime validates calendar/offsets.
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    _require(parsed.utcoffset() is not None)
    return parsed


def _validate_input(kind, value):
    _require(kind in _KINDS)
    _json_value(value)
    _REQUESTS[kind].validate(value)
    if kind == 'persona':
        _index(value['persona_candidates'], 'id')
        return
    _require(value['mode'] == kind)
    topics = _index(value['topics'], 'key')
    if kind == 'exchange':
        now = _stamp(value['as_of'])
        _index(value['episodes'], 'episode_id')
        targets = _index(value['withdrawal_candidates'], 'source_id', 'key')
        for (source, key), item in targets.items():
            _require((key,) in topics and source != value['source_id'])
            _require(_stamp(item['occurred_at']) < now and _stamp(item['available_at']) <= now)
        return
    now = _stamp(value['basis']['as_of'])
    sources = _index(value['basis']['sources'], 'source_id')
    for source in sources.values():
        _require(_stamp(source['occurred_at']) <= now)
        world = source['world']
        _require(_digest(_json(world)) == source['source_hash'])
        _require(source['appraisal']['quote'] in world['note'] + '\n' + _json(world))


def _validate_response(kind, value, response, input_digest):
    _json_value(response)
    _RESPONSES[kind].validate(response)
    _require(response['input_digest'] == input_digest)
    _require(type(response['api_calls']) is int and type(response['usage']['input_tokens']) is int)
    decision = response['decision']
    if kind == 'persona':
        _require(set(decision['persona_ids']) <= {c['id'] for c in value['persona_candidates']})
        return
    candidates = decision['candidates']
    topics = {topic['key']: topic for topic in value['topics']}
    if kind == 'exchange':
        _index(candidates, 'key')
        episodes = {episode['episode_id'] for episode in value['episodes']}
        targets = {(item['source_id'], item['key']): item for item in value['withdrawal_candidates']}
        for item in candidates:
            _require(value['origin'] == 'user' and item['key'] in topics)
            _require(item['user_quote'] in value['user_text'] and item['character_quote'] in value['character_text'])
            _require(item['experience_quote'] in item['user_quote'])
            _require(item['episode_id'] is None or item['episode_id'] in episodes)
            if item['withdraws'] is None:
                _require(value['relationship_kind'] == 'shared_experience')
            else:
                target = targets.get((item['withdraws'], item['key']))
                _require(target is not None and item['stance'] == target['stance'])
        return
    _index(candidates, 'source_id', 'key')
    sources = {source['source_id']: source for source in value['basis']['sources']}
    for item in candidates:
        _require(item['source_id'] in sources and item['key'] in topics)
        world = sources[item['source_id']]['world']
        _require(world.get('activity_kind') not in (None, 'rest'))
        _require(item['quote'] in world['note'])
        if topics[item['key']]['kind'] == 'taste':
            meals = world.get('meals', [])
            _require(isinstance(meals, list) and any(isinstance(meal, dict) and meal.get('status') == 'eaten' for meal in meals))


@dataclass(frozen=True)
class FrozenCompanionDutyResult:
    decision_json: str | None = field(default=None, repr=False)
    input_digest: str | None = None
    error_code: str | None = None
    input_json: str | None = field(default=None, repr=False)
    response_json: str | None = field(default=None, repr=False)
    response_digest: str | None = None
    schema_digest: str = SCHEMA_DIGEST

    def __post_init__(self):
        if ((self.decision_json is None) == (self.error_code is None)
                or self.error_code is not None and self.error_code not in ERROR_CODES):
            raise ValueError('result must contain a decision or a known error')

    @property
    def decision(self):
        return json.loads(self.decision_json) if self.decision_json is not None else None


class JevDutiesPort:
    def __init__(self, endpoint=DEFAULT_ENDPOINT, *, token='', timeout_seconds=50):
        # Reuse the established loopback, token, timeout, no-proxy/redirect rules.
        self._transport = JevDecisionPort(endpoint, token=token, timeout_seconds=timeout_seconds)

    def _request(self, kind, input_json):
        route = {'persona': 'persona-selection', 'exchange': 'experience-appraisal',
                 'world': 'experience-appraisal', 'proactive': 'proactive-decision'}[kind]
        endpoint = self._transport.endpoint.rsplit('/', 1)[0] + '/' + route
        body = input_json.encode('utf-8')
        if len(body) > _MAX_INPUT_BYTES:
            raise CompanionDecisionError('JEV_INPUT_TOO_LARGE')
        headers = self._transport.request_headers(_digest(input_json))
        request = urllib.request.Request(endpoint, data=body, headers=headers, method='POST')
        return self._transport.json_response(request)

    async def evaluate(self, kind, packet):
        # Freeze before the first await; later edits cannot rebind a valid result.
        try:
            _validate_input(kind, packet)
            encoded = _json(packet)
            if len(encoded.encode('utf-8')) > _MAX_INPUT_BYTES:
                return FrozenCompanionDutyResult(error_code='JEV_INPUT_TOO_LARGE')
            value = json.loads(encoded)
            input_digest = _digest(encoded)
        except (ValueError, TypeError, KeyError, ValidationError, OverflowError, RecursionError):
            return FrozenCompanionDutyResult(error_code='JEV_INPUT_INVALID')
        try:
            response = await asyncio.to_thread(self._request, kind, encoded)
            billing = response.pop('billing', None) if isinstance(response, dict) else None
            try:
                _validate_response(kind, value, response, input_digest)
                response_json = _json(response)
            except (ValueError, TypeError, KeyError, ValidationError, OverflowError, RecursionError):
                raise CompanionDecisionError('JEV_RESPONSE_INVALID') from None
            await settle_receipt(billing, input_digest)
            return FrozenCompanionDutyResult(decision_json=_json(response['decision']), input_digest=input_digest,
                input_json=encoded, response_json=response_json, response_digest=_digest(response_json))
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
        return FrozenCompanionDutyResult(input_digest=input_digest, input_json=encoded, error_code=code)


def configured_duties():
    endpoint = os.environ.get('OLIVIA_JEV_DECISION_URL', '').strip()
    if not endpoint:
        return None
    return JevDutiesPort(endpoint, token=os.environ.get('COMPANION_CLASSIFIER_TOKEN', ''))
