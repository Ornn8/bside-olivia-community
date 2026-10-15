"""Bounded server behavior controls with a last-valid local cache."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import re
from runtime.relationship_growth import DEFAULTS as RELATIONSHIP_GROWTH_DEFAULTS
from runtime.relationship_growth import CRITERIA as RELATIONSHIP_CRITERIA_DEFAULTS
from runtime.story_policy import DEFAULTS as STORY_DEFAULTS


BALANCE_CODES = ['JEV_BALANCE_INSUFFICIENT', 'JEV_BILLING_HTTP_402',
                 'PERSONAL_CHAT_PROVIDER_QUOTA_EXHAUSTED', 'LLM_QUOTA_EXHAUSTED',
                 'GPU_INSUFFICIENT_BALANCE']
_BALANCE_NOTICE = '【系统提示】Olivia 账户余额不足，这条消息没能回复。请在“回信服务 → Olivia 账户”充值后再聊天。'
_PENDING_NOTICE = '【系统提示】这条消息暂时没能回复，用量仍在核对。请勿反复重发同一条；你可以发送新消息继续聊天。'
DEFAULTS = {
    'relationship_stages': {'familiar_days': 3, 'friend_days': 8, 'trusted_days': 21,
        'familiar_score': 3, 'friend_score': 35, 'trusted_score': 70, 'trusted_deep': 35},
    'relationship_story': STORY_DEFAULTS,
    'relationship_criteria': RELATIONSHIP_CRITERIA_DEFAULTS,
    'relationship_growth': RELATIONSHIP_GROWTH_DEFAULTS,
    'qq_default_voice': {'mode': 'voice_only', 'wait_seconds': 60},
    'proactive': {
        'intervals': {tier: {'min_minutes': low, 'max_minutes': high} for tier, low, high in (
            ('reserved', 120, 360), ('familiar', 60, 180), ('trusted', 30, 90),
            ('close', 10, 25), ('committed', 5, 12))},
        'engagement': {'familiar_count': 5, 'trusted_count': 20, 'trusted_days': 3,
                       'close_count': 60, 'close_days': 5, 'committed_count': 560,
                       'committed_days': 5, 'committed_day_count': 100, 'usual_days': 2},
    },
    'generation_retry': {'no_retry_codes': BALANCE_CODES},
    'notices': {**{code: _BALANCE_NOTICE for code in BALANCE_CODES},
                'PERSONAL_CHAT_PROVIDER_USAGE_PENDING': _PENDING_NOTICE,
                'PERSONAL_CHAT_PROVIDER_REQUEST_DUPLICATE': _PENDING_NOTICE,
                'PERSONAL_CHAT_PROVIDER_BUSY': '【系统提示】服务暂时无法受理这条回复，请稍后再试。',
                'DEFAULT': '【系统提示】这条消息的回复生成失败，暂时没能回复。可以稍后重新发一下。'},
    'media_share': {'photo_daily_limit': None, 'video_daily_limit': None},
}
VERSION_PATTERN = r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}'
CODE_PATTERN = r'[A-Z][A-Z0-9_]{0,95}'


def _number(value, low, high, strict):
    if type(value) is not int or (strict and not low <= value <= high):
        raise ValueError('CLIENT_POLICY_INVALID')
    return max(low, min(high, value))


def _object(value, allowed, strict):
    if not isinstance(value, dict) or (strict and set(value) - set(allowed)):
        raise ValueError('CLIENT_POLICY_INVALID')


def _validate_key(key, value, strict):
    default = DEFAULTS[key]
    _object(value, default, strict and key != 'notices')
    if key == 'relationship_stages':
        return {name: _number(value.get(name, fallback), 1, 100, strict) for name, fallback in default.items()}
    if key == 'relationship_story':
        from runtime.story_policy import validate_texts
        return validate_texts(value, default)
    if key == 'relationship_criteria':
        result = deepcopy(default)
        for question, criteria in value.items():
            if question not in default:
                continue
            _object(criteria, default[question], True)
            for choice, description in criteria.items():
                if not isinstance(description, str) or not 1 <= len(description) <= 1000:
                    raise ValueError('CLIENT_POLICY_INVALID')
                result[question][choice] = description
        return result
    if key == 'relationship_growth':
        result = deepcopy(default)
        for name in ('everyday_daily', 'deep_daily', 'deep_weekly'):
            result[name] = _number(value.get(name, default[name]), 1, 100, strict)
        deltas = value.get('deltas', {})
        _object(deltas, default['deltas'], strict)
        for signal, dimensions in deltas.items():
            if signal not in result['deltas']:
                continue
            _object(dimensions, default['deltas'][signal], True)
            for dimension, delta in dimensions.items():
                negative = signal.startswith('friction_') or dimension == 'tension'
                result['deltas'][signal][dimension] = _number(delta, -10 if negative else 0, 10, strict)
        return result
    if key == 'qq_default_voice':
        mode = value.get('mode', default['mode'])
        if not isinstance(mode, str) or mode not in {'voice_only', 'text_then_voice', 'text_only'}:
            raise ValueError('CLIENT_POLICY_INVALID')
        return {'mode': mode, 'wait_seconds': _number(value.get('wait_seconds', 60), 5, 120, strict)}
    if key == 'proactive':
        intervals, engagement = value.get('intervals', {}), value.get('engagement', {})
        _object(intervals, default['intervals'], strict)
        _object(engagement, default['engagement'], strict)
        result = deepcopy(default)
        for tier, limits in result['intervals'].items():
            row = intervals.get(tier, limits)
            _object(row, limits, strict)
            for name in limits:
                limits[name] = _number(row.get(name, limits[name]), 1, 1440, strict)
            if limits['min_minutes'] > limits['max_minutes']:
                raise ValueError('CLIENT_POLICY_INVALID')
        for name, number in result['engagement'].items():
            result['engagement'][name] = _number(engagement.get(name, number), 1,
                                                 7 if name.endswith('_days') else 10000, strict)
        return result
    if key == 'generation_retry':
        codes = value.get('no_retry_codes', default['no_retry_codes'])
        if (not isinstance(codes, list) or len(codes) > 128 or any(
                not isinstance(code, str) or not re.fullmatch(CODE_PATTERN, code) for code in codes)):
            raise ValueError('CLIENT_POLICY_INVALID')
        return {'no_retry_codes': list(dict.fromkeys(codes))}
    if key == 'notices':
        if len(value) > 128 or any(not isinstance(code, str) or not re.fullmatch(CODE_PATTERN, code)
                or not isinstance(text, str) or not 1 <= len(text.strip()) <= 500
                or any(ord(c) < 32 for c in text) for code, text in value.items()):
            raise ValueError('CLIENT_POLICY_INVALID')
        return {**default, **value}
    return {name: None if value.get(name, number) is None else
            _number(value[name], 0, 1000, strict) for name, number in default.items()}


def validate_policy(value, *, strict=False):
    _object(value, DEFAULTS, strict)
    result = deepcopy(DEFAULTS)
    for key in DEFAULTS.keys() & value.keys():
        try:
            result[key] = _validate_key(key, value[key], strict)
        except ValueError:
            if strict:
                raise
    return result


def validate_envelope(value):
    if (not isinstance(value, dict) or not isinstance(value.get('version'), str)
            or not re.fullmatch(VERSION_PATTERN, value['version']) or not isinstance(value.get('policy'), dict)):
        raise ValueError('CLIENT_POLICY_INVALID')
    return value['version'], validate_policy(value['policy'])


class CloudPolicy:
    def __init__(self, root, client_version):
        self.path = Path(root) / 'client-policy.json'
        self.client_version = client_version
        self.version, self.policy = 'builtin-2.2.15', deepcopy(DEFAULTS)
        try:
            cached = json.loads(self.path.read_text('utf-8'))
            if cached.get('client_version') == client_version:
                self.version, self.policy = validate_envelope(cached)
        except (OSError, ValueError, AttributeError):
            pass

    async def refresh(self, fetch):
        try:
            version, policy = validate_envelope(await asyncio.wait_for(fetch(), 3))
        except Exception:
            return False
        self.version, self.policy = version, policy
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            staging = self.path.with_suffix('.tmp')
            staging.write_text(json.dumps({'version': version, 'policy': policy,
                'client_version': self.client_version}, ensure_ascii=False), encoding='utf-8')
            staging.replace(self.path)
        except OSError:
            pass
        return True


_active = None


def get_policy(key):
    return deepcopy((_active.policy if _active else DEFAULTS)[key])


def policy_version():
    return _active.version if _active else 'builtin-2.2.15'


async def fetch_policy(key, client_version):
    from aiohttp import ClientSession, ClientTimeout
    from runtime.official_endpoints import API_BASE
    from runtime.remote_generation import gpu_tls_context
    if not key:
        raise ValueError('CLIENT_POLICY_NO_ACCOUNT')
    async with ClientSession(timeout=ClientTimeout(total=3)) as session:
        async with session.get(API_BASE.rstrip('/') + '/client-policy',
                headers={'Authorization': 'Bearer ' + key, 'X-Olivia-Client-Version': client_version},
                allow_redirects=False, ssl=gpu_tls_context()) as response:
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                raw.extend(chunk)
                if len(raw) > 65536:
                    raise ValueError('CLIENT_POLICY_TOO_LARGE')
            return json.loads(raw)


def mount(app, root, stored_key):
    from original_client_update_api import running_component_version
    manager = CloudPolicy(root, running_component_version().get('version') or 'unknown')

    async def fetch():
        return await fetch_policy(stored_key(), manager.client_version)

    async def lifecycle(app):
        global _active
        _active = manager
        # Startup never waits for the network: the cache or built-in defaults apply until it answers.
        async def run():
            while True:
                await manager.refresh(fetch)
                await asyncio.sleep(600)
        task = asyncio.create_task(run())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if _active is manager:
                _active = None
    app.cleanup_ctx.append(lifecycle)
