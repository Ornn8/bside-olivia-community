"""One frozen full decision from the optional development Jev sidecar.

This port neither calls the vendor directly nor performs any proposed action.
The checked-in schema is exported from the verified sidecar contract; no runtime
dependency on a developer's release directory or training environment exists.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import json
import math
import re
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import uuid

from jsonschema import Draft202012Validator, ValidationError

from runtime.reply.jev_billing import billing_headers, settle_receipt


MODEL = 'jev-1.13.0'
DEFAULT_ENDPOINT = 'http://127.0.0.1:8097/v1/companion/decide'
_SIDECAR_ERRORS = {
    'inconsistent_affect_labels': 'JEV_AFFECT_INCONSISTENT',
    'inconsistent_affect_slots': 'JEV_AFFECT_INCONSISTENT',
    'missing_affect_evidence': 'JEV_AFFECT_INCONSISTENT',
    'missing_required_media': 'JEV_MEDIA_PLAN_INCONSISTENT',
    'unfulfilled_media_requirement': 'JEV_MEDIA_PLAN_INCONSISTENT',
    'unsupported_video_rap': 'JEV_MEDIA_PLAN_INCONSISTENT',
    'unsupported_delivery_shape': 'JEV_MEDIA_PLAN_INCONSISTENT',
    'invalid_plan_contract': 'JEV_PLAN_CONTRACT_INVALID',
    'too_many_intent': 'JEV_PLAN_INTENT_CAPACITY',
    'too_many_need': 'JEV_PLAN_NEED_CAPACITY',
    'invalid_asset_bounds': 'JEV_PLAN_ASSET_INVALID',
    'invalid_content_reference': 'JEV_REFERENCE_UNAVAILABLE',
    'ambiguous_composed_reuse': 'JEV_PLAN_COMPOSITION_INVALID',
    'invalid_composition': 'JEV_PLAN_COMPOSITION_INVALID',
    'invalid_group_reference': 'JEV_PLAN_GROUP_INVALID',
    'invalid_provider_response': 'JEV_RESPONSE_INVALID',
    'invalid_provider_usage': 'JEV_PROVIDER_USAGE_INVALID',
    'provider_request_too_large': 'JEV_INPUT_TOO_LARGE',
    'provider_response_too_large': 'JEV_RESPONSE_INVALID',
    'decision_timeout': 'JEV_TIMEOUT',
    'insufficient_balance': 'JEV_BALANCE_INSUFFICIENT',
    'provider_unavailable': 'JEV_UNAVAILABLE',
    'provider_connect_failed': 'JEV_PROVIDER_CONNECT_FAILED',
    'provider_read_failed': 'JEV_PROVIDER_READ_FAILED',
    'provider_json_invalid': 'JEV_PROVIDER_JSON_INVALID',
    **{f'provider_http_{n}': f'JEV_PROVIDER_HTTP_{n}'
       for n in (400, 401, 403, 404, 413, 422, 429, 500, 502, 503, 504, 529)},
}
ERROR_CODES = frozenset({
    *_SIDECAR_ERRORS.values(),
    'JEV_CONFIGURATION_INVALID', 'JEV_INPUT_INVALID', 'JEV_INPUT_TOO_LARGE', 'JEV_BALANCE_INSUFFICIENT',
    'JEV_RESPONSE_INVALID', 'JEV_TIMEOUT', 'JEV_UNAVAILABLE', 'JEV_HTTP_ERROR',
    'JEV_AFFECT_INCONSISTENT', 'JEV_MEDIA_PLAN_INCONSISTENT', 'JEV_REFERENCE_UNAVAILABLE',
    'JEV_WORLD_SELECTION_UNAVAILABLE', 'JEV_WORLD_SELECTION_CAPACITY',
    'JEV_WORLD_SELECTION_BUDGET', 'JEV_WORLD_SELECTION_INVALID',
    'JEV_BILLING_UNAVAILABLE', 'JEV_BILLING_RECEIPT_INVALID', 'JEV_BILLING_RESPONSE_INVALID',
    'JEV_BILLING_ACCOUNT_UNAVAILABLE', 'JEV_BILLING_TURN_INVALID',
    *(f'JEV_BILLING_HTTP_{status}' for status in (401, 402, 403, 409, 429, 502, 503, 504)),
    *(f'JEV_HTTP_{status}' for status in (400, 401, 404, 413, 429, 503)),
})
from runtime.reply.jev_limits import JEV_MAX_INPUT_BYTES as _MAX_INPUT_BYTES
_MAX_RESPONSE_BYTES = 262144
_SCHEMA = json.loads(Path(__file__).with_name('companion_decision_schema.json').read_text('utf-8'))
_INPUT = Draft202012Validator(_SCHEMA['input'])
_OUTPUT = Draft202012Validator(_SCHEMA['output'])
_OPERATIONS = _SCHEMA['control_operations']


class CompanionDecisionError(ValueError):
    def __init__(self, code):
        if code not in ERROR_CODES:
            raise ValueError('unknown companion decision error code')
        self.code = code
        super().__init__(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True, allow_nan=False)


def _http_error_code(error):
    code = f'JEV_HTTP_{error.code}'
    if code not in ERROR_CODES:
        code = 'JEV_HTTP_ERROR'
    try:
        raw = error.read(4097)
        detail = json.loads(raw) if len(raw) <= 4096 else {}
        reason = detail.get('error') if isinstance(detail, dict) else None
        if isinstance(reason, str):
            code = _SIDECAR_ERRORS.get(reason, code)
            if reason.startswith('empty_reference_catalog:'):
                code = 'JEV_REFERENCE_UNAVAILABLE'
    except (ValueError, OSError, RecursionError, http.client.HTTPException):
        pass
    finally:
        error.close()
    return code


def _as_of(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
        raise ValueError('aware time required')
    return stamp.astimezone(timezone.utc).isoformat()


def _validate_input(value):
    _INPUT.validate(value)
    messages = value['messages']
    ids = [m['id'] for m in messages]
    if (len(set(ids)) != len(ids) or ids[-1] != value['current_turn_id']
            or messages[-1]['role'] != 'user'):
        raise ValueError('invalid current turn')


def _daily_video_experience(value):
    if value is None:
        return None
    kinds = {'housework', 'walk', 'bath_finished', 'shopping', 'meal', 'wake_up'}
    if (not isinstance(value, dict) or set(value) != {'event_ids', 'event_kinds', 'max_seconds'}
            or type(value['max_seconds']) is not int or value['max_seconds'] != 15
            or not isinstance(value['event_ids'], list) or not 1 <= len(value['event_ids']) <= 6
            or not all(isinstance(item, str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', item)
                       for item in value['event_ids'])
            or len(set(value['event_ids'])) != len(value['event_ids'])
            or not isinstance(value['event_kinds'], list) or not 1 <= len(value['event_kinds']) <= 6
            or not all(isinstance(item, str) and item in kinds for item in value['event_kinds'])
            or len(set(value['event_kinds'])) != len(value['event_kinds'])):
        raise ValueError('invalid daily video experience')
    return json.loads(_json(value))


@dataclass(frozen=True)
class FrozenCompanionTurn:
    input_json: str
    source_ids: tuple[tuple[str, str], ...]
    as_of: str
    input_revision: int | str
    input_digest: str
    speech_enabled: bool = False
    bedtime_offer: bool = False
    daily_video_json: str | None = None

    @classmethod
    def create(cls, *, messages, current_source_id, capabilities, environment,
               forbidden_kinds, as_of, input_revision=0, speech_enabled=False, bedtime_offer=False,
               daily_video_experience=None):
        try:
            if type(bedtime_offer) is not bool:
                raise ValueError('invalid bedtime offer scope')
            if (type(input_revision) not in (int, str)
                    or isinstance(input_revision, int) and input_revision < 0
                    or isinstance(input_revision, str) and not 1 <= len(input_revision) <= 128):
                raise ValueError('invalid revision')
            if not isinstance(messages, (list, tuple)) or not 1 <= len(messages) <= 32:
                raise ValueError('invalid messages')
            wire, sources = [], []
            for position, message in enumerate(messages, 1):
                if not isinstance(message, dict) or set(message) != {'source_id', 'role', 'text'}:
                    raise ValueError('invalid message')
                source = message['source_id']
                if not isinstance(source, str) or not 1 <= len(source) <= 256:
                    raise ValueError('invalid source')
                short_id = f't{position}'
                sources.append((short_id, source))
                wire.append(dict(id=short_id, role=message['role'], text=message['text']))
            if len({source for _, source in sources}) != len(sources) or current_source_id != sources[-1][1]:
                raise ValueError('invalid source binding')
            value = dict(messages=wire, current_turn_id=sources[-1][0], capabilities=capabilities,
                         environment=environment, forbidden_kinds=forbidden_kinds)
            _validate_input(value)
            encoded = _json(value)
            when = _as_of(as_of)
            binding = dict(input=value, source_id_map=sources, as_of=when, input_revision=input_revision,
                           contract=_SCHEMA['source_sha256'])
            if speech_enabled:
                binding['speech_experience'] = True
            if bedtime_offer:
                if not speech_enabled:
                    raise ValueError('bedtime offer requires speech capability')
                binding['bedtime_offer'] = True
            daily = _daily_video_experience(daily_video_experience)
            if daily is not None:
                if 'video_speech' not in capabilities['kinds']:
                    raise ValueError('daily video capability missing')
                binding['daily_video_experience'] = daily
            digest = hashlib.sha256(_json(binding).encode('utf-8')).hexdigest()
        except (ValueError, TypeError, KeyError, ValidationError, OverflowError):
            raise CompanionDecisionError('JEV_INPUT_INVALID') from None
        if len(_json({'input': value}).encode('utf-8')) > _MAX_INPUT_BYTES:
            raise CompanionDecisionError('JEV_INPUT_TOO_LARGE')
        return cls(encoded, tuple(sources), when, input_revision, digest, speech_enabled,
                   bedtime_offer=bedtime_offer,
                   daily_video_json=_json(daily) if daily is not None else None)

    @property
    def input(self):
        return json.loads(self.input_json)

    @property
    def daily_video_experience(self):
        return json.loads(self.daily_video_json) if self.daily_video_json is not None else None


@dataclass(frozen=True)
class FrozenCompanionDecision:
    plan_json: str
    input_digest: str
    source_ids: tuple[tuple[str, str], ...]
    input_revision: int | str
    as_of: str
    model: str = MODEL
    metering_json: str | None = None
    profile: str = 'full'
    speech_json: str | None = None
    speech_offer: str | None = None
    daily_video_json: str | None = None

    @classmethod
    def from_response(cls, turn, response):
        try:
            _validate_response(turn.input, response)
            if turn.speech_enabled and 'speech_request' not in response:
                raise ValueError('missing speech decision')
            if turn.bedtime_offer and 'speech_offer' not in response:
                raise ValueError('missing bedtime decision')
            encoded = _json(response['plan'])
        except (ValueError, TypeError, KeyError, ValidationError, OverflowError):
            raise CompanionDecisionError('JEV_RESPONSE_INVALID') from None
        metering = _json({name: response[name] for name in ('usage', 'api_calls', 'latency_ms')})
        return cls(encoded, turn.input_digest, turn.source_ids, turn.input_revision, turn.as_of,
                   metering_json=metering, profile=response.get('evaluation', {}).get('profile', 'full'),
                   speech_json=_json(_speech_request(response.get('speech_request'))) if turn.speech_enabled else None,
                   speech_offer=_speech_offer(response['speech_offer']) if turn.bedtime_offer else None,
                   daily_video_json=turn.daily_video_json)

    @classmethod
    def from_record(cls, turn, record, *, profile='full'):
        """Restore only against the caller's freshly rebuilt complete turn."""
        try:
            fields = {'schema_version', 'plan', 'model', 'input_digest', 'source_id_map', 'input_revision', 'as_of'}
            if (not isinstance(record, dict) or set(record) - {
                    'metering', 'evaluation', 'speech_request', 'speech_offer', 'daily_video_experience'} != fields
                    or record['schema_version'] != 'companion-decision/1' or record['model'] != MODEL
                    or record['input_digest'] != turn.input_digest or record['as_of'] != turn.as_of
                    or type(record['input_revision']) is not type(turn.input_revision)
                    or record['input_revision'] != turn.input_revision
                    or record['source_id_map'] != dict(turn.source_ids)):
                raise ValueError('record does not match frozen turn')
            if _daily_video_experience(record.get('daily_video_experience')) != turn.daily_video_experience:
                raise ValueError('record daily video capability mismatch')
            _validate_evaluation(record)
            if turn.speech_enabled and 'speech_request' not in record:
                raise ValueError('missing speech decision')
            if turn.bedtime_offer and 'speech_offer' not in record:
                raise ValueError('missing bedtime decision')
            if 'speech_offer' in record:
                _speech_offer(record['speech_offer'])
            record_profile = record.get('evaluation', {}).get('profile', 'full')
            if profile not in ('full', 'single_delivery') or record_profile == 'single_delivery' and profile != record_profile:
                raise ValueError('decision evaluation profile mismatch')
            _validate_plan(turn.input, record['plan'])
            encoded = _json(record['plan'])
            metering = record.get('metering')
            if metering is not None:
                if (set(metering) != {'usage', 'api_calls', 'latency_ms'}
                        or set(metering['usage']) != {'input_tokens'}
                        or type(metering['usage']['input_tokens']) is not int or metering['usage']['input_tokens'] < 0
                        or type(metering['api_calls']) is not int or metering['api_calls'] < 0
                        or type(metering['latency_ms']) not in (int, float)
                        or not math.isfinite(metering['latency_ms']) or metering['latency_ms'] < 0):
                    raise ValueError('invalid metering')
        except (ValueError, TypeError, KeyError, ValidationError, OverflowError):
            raise CompanionDecisionError('JEV_RESPONSE_INVALID') from None
        return cls(encoded, turn.input_digest, turn.source_ids, turn.input_revision, turn.as_of,
                   metering_json=_json(metering) if metering is not None else None, profile=record_profile,
                   speech_json=_json(_speech_request(record.get('speech_request'))) if 'speech_request' in record else None,
                   speech_offer=record.get('speech_offer') if turn.bedtime_offer else None,
                   daily_video_json=turn.daily_video_json)

    @property
    def plan(self):
        return json.loads(self.plan_json)

    def matches(self, turn):
        return (isinstance(turn, FrozenCompanionTurn) and self.input_digest == turn.input_digest
                and self.source_ids == turn.source_ids and self.input_revision == turn.input_revision
                and self.as_of == turn.as_of and self.model == MODEL)

    def record(self):
        result = dict(schema_version='companion-decision/1', plan=self.plan, model=self.model,
                    input_digest=self.input_digest, source_id_map=dict(self.source_ids),
                    input_revision=self.input_revision, as_of=self.as_of)
        if self.metering_json is not None:
            result['metering'] = json.loads(self.metering_json)
        if self.profile == 'single_delivery':
            result['evaluation'] = dict(profile=self.profile, not_evaluated=['control'])
        if self.speech_json is not None:
            result['speech_request'] = json.loads(self.speech_json)
        if self.speech_offer is not None:
            result['speech_offer'] = self.speech_offer
        if self.daily_video_json is not None:
            result['daily_video_experience'] = json.loads(self.daily_video_json)
        return result

    def writer_projection(self):
        plan = self.plan
        understanding, proposal = plan['understanding'], plan['proposal']
        return dict(intents=understanding['intents'], needs=understanding['needs'],
                    affect=understanding['affect'], moves=proposal['moves'],
                    resolution=plan['resolution'], timing=proposal['timing'],
                    clarify_fields=proposal['clarify_fields'])


@dataclass(frozen=True)
class CompanionDecisionResult:
    decision: FrozenCompanionDecision | None = None
    error_code: str | None = None
    failure_context: dict = field(default_factory=dict)

    def __post_init__(self):
        if ((self.decision is None) == (self.error_code is None)
                or self.error_code is not None and self.error_code not in ERROR_CODES):
            raise ValueError('result must contain a decision or a known error')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _endpoint(value):
    from original_client_relay_api import RELAY_BASE
    if value == RELAY_BASE + '/companion/decide' and value.startswith('https://'):
        return value
    parsed = urllib.parse.urlsplit(value)
    host = parsed.hostname
    if host == 'localhost':
        host = '127.0.0.1'  # Avoid DNS/proxy reinterpreting a local-only configuration.
    address = ipaddress.ip_address(host)
    port = parsed.port if parsed.port is not None else 80
    if (parsed.scheme != 'http' or not address.is_loopback or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path != '/v1/companion/decide' or not 0 < port <= 65535):
        raise ValueError('invalid endpoint')
    host = f'[{address.compressed}]' if address.version == 6 else address.compressed
    return f'http://{host}:{port}/v1/companion/decide'


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _nonfinite(_):
    raise ValueError('nonfinite JSON')


class JevDecisionPort:
    def __init__(self, endpoint=DEFAULT_ENDPOINT, *, token='', timeout_seconds=50, profile='full'):
        try:
            self.endpoint = _endpoint(endpoint)
            if (not isinstance(token, str) or len(token) > 4096 or any(c.isspace() for c in token)
                    or type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                    or not 0 < timeout_seconds <= 120 or profile not in ('full', 'single_delivery')):
                raise ValueError('invalid configuration')
            token.encode('ascii')
        except (ValueError, TypeError, AttributeError, UnicodeError):
            raise CompanionDecisionError('JEV_CONFIGURATION_INVALID') from None
        self._token = token
        self.profile = profile
        self.timeout_seconds = timeout_seconds
        handlers = [urllib.request.ProxyHandler({}), _NoRedirect()]
        if self.endpoint.startswith('https://'):
            from runtime.remote_generation import gpu_tls_context
            handlers.append(urllib.request.HTTPSHandler(context=gpu_tls_context()))
        self._opener = urllib.request.build_opener(*handlers)

    def request_headers(self, body_digest):
        headers = {'Content-Type': 'application/json', 'X-Olivia-Usage-Id': body_digest,
                   'X-Olivia-Request-Id': str(uuid.uuid4())}
        if self.endpoint.startswith('https://'):
            from .jev_billing import cloud_request_headers
            headers.update(cloud_request_headers(body_digest))
        else:
            headers.update(billing_headers())
            if self._token:
                headers['Authorization'] = 'Bearer ' + self._token
        return headers

    def _packet(self, turn):
        return {'input': turn.input, **({'profile': self.profile} if self.profile != 'full' else {}),
                **({'speech_experience': True} if turn.speech_enabled else {}),
                **({'bedtime_offer': True} if turn.bedtime_offer else {}),
                **({'daily_video_experience': turn.daily_video_experience} if turn.daily_video_json is not None else {})}

    def _request(self, turn):
        value = turn.input
        _validate_input(value)
        body = _json(self._packet(turn)).encode('utf-8')
        if len(body) > _MAX_INPUT_BYTES:
            raise CompanionDecisionError('JEV_INPUT_TOO_LARGE')
        headers = self.request_headers(hashlib.sha256(body).hexdigest())
        request = urllib.request.Request(self.endpoint, data=body, headers=headers, method='POST')
        return self.json_response(request)

    def json_response(self, request):
        stage = 'request'
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                stage = 'http_response'
                if response.status != 200:
                    raise CompanionDecisionError('JEV_HTTP_ERROR')
                if response.headers.get_content_type() != 'application/json':
                    raise CompanionDecisionError('JEV_RESPONSE_INVALID')
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise CompanionDecisionError('JEV_RESPONSE_INVALID')
            stage = 'response_json'
            return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                              parse_constant=_nonfinite)
        except urllib.error.HTTPError as exc:
            status, code = exc.code, _http_error_code(exc)
            stage = 'http_response'
            self._raise_transport_failure(request, code, stage, status, exc)
        except CompanionDecisionError as exc:
            self._raise_transport_failure(request, exc.code, stage, None, exc)
        except (TimeoutError, urllib.error.URLError) as exc:
            code = 'JEV_TIMEOUT' if isinstance(exc, TimeoutError) or isinstance(getattr(exc, 'reason', None), TimeoutError) else 'JEV_UNAVAILABLE'
            self._raise_transport_failure(request, code, stage, None, exc)
        except (OSError, http.client.HTTPException) as exc:
            self._raise_transport_failure(request, 'JEV_UNAVAILABLE', stage, None, exc)
        except (ValueError, UnicodeError, RecursionError) as exc:
            self._raise_transport_failure(request, 'JEV_RESPONSE_INVALID', stage, None, exc)

    def _raise_transport_failure(self, request, code, stage, status, exc):
        from runtime.diagnostics.failure_context import record_jev_failure
        failure = CompanionDecisionError(code)
        failure.failure_context = record_jev_failure(exc, code, stage,
            request_id=request.get_header('X-olivia-request-id'), http_status=status)
        raise failure from exc

    async def decide(self, turn):
        if not isinstance(turn, FrozenCompanionTurn):
            return CompanionDecisionResult(error_code='JEV_INPUT_INVALID')
        try:
            response = await asyncio.to_thread(self._request, turn)
            billing = response.pop('billing', None) if isinstance(response, dict) else None
            decision = FrozenCompanionDecision.from_response(turn, response)
            if decision.profile != self.profile:
                raise CompanionDecisionError('JEV_RESPONSE_INVALID')
            await settle_receipt(billing, hashlib.sha256(_json(self._packet(turn)).encode('utf-8')).hexdigest())
            return CompanionDecisionResult(decision=decision)
        except CompanionDecisionError as error:
            return CompanionDecisionResult(error_code=error.code,
                failure_context=getattr(error, 'failure_context', {}))
        except urllib.error.HTTPError as error:
            code = _http_error_code(error)
        except TimeoutError:
            code = 'JEV_TIMEOUT'
        except urllib.error.URLError as error:
            code = 'JEV_TIMEOUT' if isinstance(error.reason, TimeoutError) else 'JEV_UNAVAILABLE'
        except (OSError, ValueError, TypeError, ValidationError):
            code = 'JEV_UNAVAILABLE'
        return CompanionDecisionResult(error_code=code)


def _task_projection(plan):
    """The sidecar's positional compatibility view must agree with its full plan."""
    def fields(value, names):
        return [value[key] for key in names.split()]
    u, p, r = (plan[key] for key in ('understanding', 'proposal', 'resolution'))
    a = u['affect']
    return dict(intent=fields(u, 'intents needs extras_allowed'),
        affect=[a['status'], a['coverage'], [fields(e, 'label intensity target basis evidence_turn_ids') for e in a['states']]],
        response=[fields(m, 'id act tones intensity') for m in p['moves']],
        requirements=[[q['id'], q['fulfillment'], [fields(x, 'kinds min_assets max_assets') for x in q['alternatives']], q['evidence_turn_ids']] for q in u['requirements']],
        media=[p['timing'], [fields(x, 'id purpose move_ids allowed_kinds derived_from') for x in p['contents']],
               [[s['id'], s['medium'], [fields(x, 'kind content_ref') for x in s['parts']],
                 [fields(x, 'step_id event') for x in s['after']], s['requirement_ids']] for s in p['steps']],
               p['deliver_together'], p['synchronize'], p['clarify_fields']],
        control=[[fields(x, 'domain operation scope evidence_turn_ids') for x in u['control_candidates']],
                 fields(r, 'status blocked_steps uncertain_fields action_executed')])


def _validate_evaluation(value):
    if 'evaluation' in value and value['evaluation'] != dict(profile='single_delivery', not_evaluated=['control']):
        raise ValueError('invalid evaluation coverage')


def _speech_request(value):
    from runtime.personal_chat.speech import validate_intent
    return validate_intent(value)


def _speech_offer(value):
    if value not in ('none', 'bedtime', 'clarify'):
        raise ValueError('invalid bedtime decision')
    return value


def _validate_response(request, value):
    expected = {'schema_version', 'tasks', 'plan', 'contract_valid', 'status', 'fallback', 'action_executed',
                'model', 'backend', 'production_approved', 'latency_ms', 'api_calls', 'usage'}
    if (not isinstance(value, dict) or set(value) - {'evaluation', 'speech_request', 'speech_offer'} != expected
            or value['schema_version'] != 'companion-shadow/1' or value['model'] != MODEL
            or value['backend'] != 'jev' or value['contract_valid'] is not True
            or value['status'] != 'valid_contract' or value['fallback'] is not False
            or value['action_executed'] is not False or value['production_approved'] is not False
            or type(value['latency_ms']) not in (int, float) or not math.isfinite(value['latency_ms'])
            or value['latency_ms'] < 0 or type(value['api_calls']) is not int or value['api_calls'] < 0
            or not isinstance(value['usage'], dict) or set(value['usage']) != {'input_tokens'}
            or type(value['usage']['input_tokens']) is not int or value['usage']['input_tokens'] < 0):
        raise ValueError('invalid complete envelope')
    _speech_request(value.get('speech_request'))
    if 'speech_offer' in value:
        _speech_offer(value['speech_offer'])
    _validate_evaluation(value)
    _validate_plan(request, value['plan'])
    if _json(value['tasks']) != _json(_task_projection(value['plan'])):
        raise ValueError('inconsistent task projection')


# Relational checks copied from verified sidecar contract f8402144cb49;
# only the function entry and private helper names are adapted.
def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _index(items):
    result = {item['id']: item for item in items}
    _require(len(result) == len(items), 'duplicate IDs')
    return result


def _acyclic(nodes, edges):
    pending = set(nodes)
    while pending:
        ready = {n for n in pending if not any(b == n and a in pending for a, b in edges)}
        _require(ready, 'cyclic dependency')
        pending -= ready


def _medium(kind):
    return kind.split('_')[0]


def _validate_plan(request, output):
    _validate_input(request)
    _OUTPUT.validate(output)
    turns = _index(request['messages'])
    _require(request['current_turn_id'] in turns and turns[request['current_turn_id']]['role'] == 'user', 'current turn must be an input user turn')
    _require(request['messages'][-1]['id'] == request['current_turn_id'], 'context cannot contain future turns')
    u, p, r = output['understanding'], output['proposal'], output['resolution']
    affect = u['affect']
    _require((affect['status'] == 'present') == bool(affect['states']), 'affect status and labels disagree')
    _require(len({e['label'] for e in affect['states']}) == len(affect['states']), 'duplicate emotion label')
    if affect['status'] == 'no_salient':
        _require(affect['coverage'] == 'complete', 'no_salient cannot claim known missing emotion')
    if affect['status'] == 'unclear' or affect['coverage'] == 'partial':
        _require('affect' in r['uncertain_fields'], 'local affect uncertainty must be visible')
    for item in affect['states'] + u['control_candidates'] + u['requirements']:
        _require(set(item['evidence_turn_ids']) <= set(turns), 'unknown evidence turn')
    for item in u['control_candidates']:
        _require(item['operation'] in _OPERATIONS[item['domain']], 'operation/domain mismatch')
        _require(any(turns[t]['role'] == 'user' for t in item['evidence_turn_ids']), 'control candidate needs user evidence')
    moves, contents, steps, requirements = (_index(items) for items in (p['moves'], p['contents'], p['steps'], u['requirements']))
    content_edges = []
    for item in contents.values():
        _require(set(item['move_ids']) <= set(moves), 'unknown move reference')
        if item['derived_from'] is not None:
            _require(item['derived_from'] in contents, 'unknown derivation source')
            content_edges.append((item['derived_from'], item['id']))
    _acyclic(contents, content_edges)
    used_contents, edges, blocked = set(), [], set()
    spoken_refs = set()
    for sid, step in steps.items():
        _require(set(step['requirement_ids']) <= set(requirements), 'unknown requirement')
        _require(all(_medium(part['kind']) == step['medium'] for part in step['parts']), 'asset parts have incompatible media')
        if step['medium'] in ('text', 'image'):
            _require(len(step['parts']) == 1, 'text/image asset has one part')
        for part in step['parts']:
            kind, ref = part['kind'], part['content_ref']
            _require(ref in contents, 'unknown content reference')
            _require(kind in contents[ref]['allowed_kinds'], 'content projection forbidden')
            used_contents.add(ref)
            if kind in ('audio_speech', 'video_speech'):
                spoken_refs.add(ref)
            env = request['environment']
            unavailable = kind not in request['capabilities']['kinds'] or kind in request['forbidden_kinds']
            unavailable |= kind == 'text' and (env['can_read'] is False or env['can_view'] is False)
            unavailable |= _medium(kind) in ('image', 'video') and env['can_view'] is False
            unavailable |= (kind.startswith('audio_') or kind in ('video_speech', 'video_song')) and env['can_listen'] is False
            if unavailable:
                blocked.add(sid)
        if len(step['parts']) > 1 and not request['capabilities'].get('compose_' + step['medium'], False):
            blocked.add(sid)
        for dep in step['after']:
            _require(dep['step_id'] in steps, 'unknown predecessor')
            edges.append((dep['step_id'], sid))
            if dep['event'] == 'playback_finished':
                _require(steps[dep['step_id']]['medium'] in ('audio', 'video'), 'playback event requires playable asset')
                if not request['capabilities']['playback_events']:
                    blocked.add(sid)
    _acyclic(steps, edges)
    _require(used_contents == set(contents), 'unused content unit')
    covered_moves = {m for ref in used_contents for m in contents[ref]['move_ids']}
    _require(covered_moves == set(moves), 'uncovered response move')
    if len(spoken_refs) > 1 and not request['capabilities']['split_spoken_content']:
        blocked.update(sid for sid, step in steps.items() if any(part['kind'] in ('audio_speech', 'video_speech') for part in step['parts']))
    # Check cycles after collapsing every simultaneous/co-delivered component.
    group_of = {sid: sid for sid in steps}
    for name in ('deliver_together', 'synchronize'):
        seen = set()
        for group in p[name]:
            _require(len(group) >= 2 and set(group) <= set(steps), 'invalid delivery group')
            _require(not seen.intersection(group), 'overlapping groups of same type')
            seen.update(group)
            roots = {group_of[sid] for sid in group}
            representative = min(roots)
            group_of = {sid: representative if root in roots else root for sid, root in group_of.items()}
            if name == 'synchronize' and not request['capabilities']['synchronize']:
                blocked.update(group)
    grouped_edges = {(group_of[a], group_of[b]) for a, b in edges}
    _acyclic(set(group_of.values()), grouped_edges)
    # Preserve dependencies when any prerequisite or grouped asset is unsupported.
    changed = True
    while changed:
        before = set(blocked)
        blocked.update(b for a, b in edges if a in blocked)
        blocked.update(sid for sid in steps if any(group_of[sid] == group_of[b] for b in blocked))
        changed = before != blocked
    covered_steps = set()
    for rid, requirement in requirements.items():
        members = [s for s in steps.values() if rid in s['requirement_ids']]
        _require(all(alt['min_assets'] <= alt['max_assets'] for alt in requirement['alternatives']), 'invalid asset count bounds')
        if requirement['fulfillment'] == 'pending':
            _require(not members, 'pending need is not fulfilled in current delivery')
            continue
        kinds = {part['kind'] for s in members for part in s['parts']}
        _require(any(kinds == set(alt['kinds']) and alt['min_assets'] <= len(members) <= alt['max_assets'] for alt in requirement['alternatives']), 'selected assets do not match any complete alternative')
        covered_steps.update(s['id'] for s in members)
    if not u['extras_allowed']:
        _require(covered_steps == set(steps), 'unrequested extra step')
    if p['timing'] in ('wait_user', 'defer', 'no_reply'):
        _require(not steps and not moves, 'silent/deferred turn cannot deliver now')
    else:
        _require(steps, 'replying requires a delivery proposal')
    _require(set(r['blocked_steps']) == blocked, 'blocked steps must match capability resolution')
    if blocked:
        _require(r['status'] == 'unsupported', 'unsupported proposal must be explicit')
    elif p['clarify_fields']:
        _require(r['status'] == 'needs_clarification' and any(m['act'] == 'clarify' for m in moves.values()), 'clarification must name a needed decision and action')
    else:
        _require(r['status'] == ('partial' if r['uncertain_fields'] else 'ready'), 'local uncertainty/status mismatch')
