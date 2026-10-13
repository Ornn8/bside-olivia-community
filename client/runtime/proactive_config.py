"""Wording and rules for the messages she starts, set by the cloud.

Until the cloud says otherwise every value is the one built into the client, so
nothing changes. A field that fails validation falls back to its built-in value on
its own; the rest of the answer still applies. The last valid answer is kept in
proactive/config.json and used when the cloud cannot be reached.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import copy
import re
import time

from runtime.reply.proactive_letters import read_json, write_json

REFRESH_SECONDS = 6 * 3600
RETRY_SECONDS = 900
BEIJING = timezone(timedelta(hours=8))
TEMPLATES = ('initiative_planning', 'initiative_write', 'diary_due', 'contact_invitation')
MEDIA = ('text', 'audio_speech')
CHANNEL_POLICIES = ('preferred', 'qq_first', 'letter_first')
DEFAULT_QUIET = ('00:00', '08:30')  # The hours her own initiative already keeps.
DEFAULT_RULES = {
    'initiative_media': {'letter': ['text'], 'qq': ['audio_speech']},
    'initiative_photo': False,
    'channel_policy': 'preferred',
    'max_per_day': None,
    'min_gap_minutes': None,
    'quiet_hours': None,
    'contact_enabled': True,
}
_VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}')
_CLOCK = re.compile(r'([01]\d|2[0-3]):[0-5]\d')
_state = {'version': 'builtin', 'source': 'defaults', 'templates': {}, 'rules': {}}
_path = None
_next_attempt = 0.0


def minutes(clock):
    """'HH:MM' as minutes after midnight."""
    if not isinstance(clock, str) or not _CLOCK.fullmatch(clock):
        raise ValueError('PROACTIVE_CONFIG_INVALID')
    return int(clock[:2]) * 60 + int(clock[3:])


def clock_range(value):
    """Two different 'HH:MM' strings, as a pair of minutes."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError('PROACTIVE_CONFIG_INVALID')
    start, end = minutes(value[0]), minutes(value[1])
    if start == end:
        raise ValueError('PROACTIVE_CONFIG_INVALID')
    return start, end


def inside(window, moment):
    """Whether a local time falls in a daily range; a range may run past midnight."""
    start, end = window
    now = moment.hour * 60 + moment.minute
    return start <= now < end if start < end else now >= start or now < end


def _template(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 3000
            or any(ord(char) < 32 and char not in '\n\r\t' for char in value)):
        raise ValueError('PROACTIVE_CONFIG_INVALID')
    return value


def _initiative_media(value):
    if not isinstance(value, dict):
        raise ValueError('PROACTIVE_CONFIG_INVALID')
    result = {}
    for channel in ('letter', 'qq'):
        kinds = value.get(channel)
        if (isinstance(kinds, list) and all(isinstance(kind, str) and kind in MEDIA for kind in kinds)
                and len(set(kinds)) == len(kinds)):
            result[channel] = list(kinds)
    if not result:
        raise ValueError('PROACTIVE_CONFIG_INVALID')
    return result


def _flag(value):
    if type(value) is not bool:
        raise ValueError('PROACTIVE_CONFIG_INVALID')
    return value


def _policy(value):
    if not isinstance(value, str) or value not in CHANNEL_POLICIES:
        raise ValueError('PROACTIVE_CONFIG_INVALID')
    return value


def _count(low, high):
    def check(value):
        if type(value) is not int or not low <= value <= high:
            raise ValueError('PROACTIVE_CONFIG_INVALID')
        return value
    return check


def _quiet(value):
    if value is None:
        return None
    clock_range(value)
    return list(value)


_RULES = {'initiative_media': _initiative_media, 'initiative_photo': _flag, 'channel_policy': _policy,
          'max_per_day': _count(0, 10), 'min_gap_minutes': _count(0, 1440), 'quiet_hours': _quiet,
          'contact_enabled': _flag}


def validate(payload):
    """(version, templates, rules) with only the valid fields; unknown ones are ignored."""
    if (not isinstance(payload, dict) or not isinstance(payload.get('version'), str)
            or not _VERSION.fullmatch(payload['version'])):
        raise ValueError('PROACTIVE_CONFIG_INVALID')
    templates, rules = {}, {}
    sent = payload.get('templates') if isinstance(payload.get('templates'), dict) else {}
    for name in TEMPLATES:
        try:
            templates[name] = _template(sent[name])
        except (KeyError, ValueError):
            pass
    sent = payload.get('rules') if isinstance(payload.get('rules'), dict) else {}
    for name, check in _RULES.items():
        try:
            rules[name] = check(sent[name])
        except (KeyError, ValueError):
            pass
    return payload['version'], templates, rules


def configure(root):
    """Start from the cached answer, or the built-in values when there is none."""
    global _path, _state, _next_attempt
    _path = Path(root) / 'proactive' / 'config.json' if root is not None else None
    _next_attempt = 0.0
    _state = {'version': 'builtin', 'source': 'defaults', 'templates': {}, 'rules': {}}
    if _path is not None:
        try:
            version, templates, rules = validate(read_json(_path))
            _state = {'version': version, 'source': 'cache', 'templates': templates, 'rules': rules}
        except ValueError:
            pass


def adopt(payload):
    """Use a fetched answer and keep it for when the cloud cannot be reached."""
    global _state
    version, templates, rules = validate(payload)
    _state = {'version': version, 'source': 'cloud', 'templates': templates, 'rules': rules}
    if _path is not None:
        try:
            write_json(_path, {'version': version, 'templates': templates, 'rules': rules})
        except OSError:
            pass


async def refresh_if_due(now=None):
    """Fetch at start and every six hours; on any error the answer in use stays."""
    global _next_attempt
    now = time.time() if now is None else now
    if now < _next_attempt:
        return False
    from runtime import cloud_events
    try:
        adopt(await cloud_events.request('GET', '/proactive/config'))
    except Exception:
        _next_attempt = now + RETRY_SECONDS
        return False
    _next_attempt = now + REFRESH_SECONDS
    return True


def status():
    return {'version': _state['version'], 'source': _state['source']}


def template(name):
    """The cloud's text for one stage, or None for the built-in one."""
    return _state['templates'].get(name)


def rule(name):
    value = _state['rules'].get(name, DEFAULT_RULES[name])
    if name == 'initiative_media':
        value = {**DEFAULT_RULES[name], **value}
    return copy.deepcopy(value)


def media(channel):
    """What she may start with on this channel ('letter' or 'qq')."""
    return rule('initiative_media')[channel]


def quiet_window():
    """The quiet hours a cloud contact keeps: the cloud's, else the ones her own initiative keeps."""
    return clock_range(rule('quiet_hours') or DEFAULT_QUIET)


def initiative_blocked(rows, now):
    """Why the cloud's rules hold back her own next message, or None.

    Only messages she started herself count; a cloud contact has its own fields.
    """
    local = datetime.fromtimestamp(now, BEIJING)
    quiet = rule('quiet_hours')
    if quiet is not None and inside(clock_range(quiet), local):
        return 'quiet_hours'
    limit, gap = rule('max_per_day'), rule('min_gap_minutes')
    if limit is None and not gap:
        return None
    sent = [float(row.get('created_at', 0)) for row in rows
            if row.get('origin') == 'proactive' and not row.get('contact_id')
            and (row.get('delivery_status') or row.get('letter_status')) not in {'SKIPPED', 'CANCELED', 'FAILED'}]
    if limit is not None and sum(at > now - 86400 for at in sent) >= limit:
        return 'attempt_limit'
    if gap and sent and now - max(sent) < gap * 60:
        return 'not_due'
    return None
