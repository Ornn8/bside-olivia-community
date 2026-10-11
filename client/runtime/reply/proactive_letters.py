"""Cheap, file-only opportunity preparation; never generates or delivers letters."""
from __future__ import annotations

import hashlib
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import time

from runtime.personal_chat.initiative_profile import InitiativeProfile, profile_from_rows

DEFAULTS = {'enabled': True, 'allow_voice': True, 'login_check_enabled': False}
DAY = 86400


def settings(value: dict) -> dict:
    return {key: value.get(key) if type(value.get(key)) is bool else default
            for key, default in DEFAULTS.items()}


def load_settings(data_root: Path) -> dict:
    """Letters are on by default since 2.2.7; a stored 'off' from before came from the old default."""
    path = data_root / 'proactive' / 'settings.json'
    value = read_json(path)
    marker = data_root / 'proactive' / 'letters-default-on'
    try:
        if not marker.exists():
            if value.get('enabled') is False:
                value = {**value, 'enabled': True}
                write_json(path, settings(value))
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
    except OSError:
        pass
    return settings(value)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _stamp(value) -> float:
    return float(value) if type(value) in (int, float) and value >= 0 else 0.0


def _is_im(row: dict) -> bool:
    return row.get('channel') in {'qq', 'wechat'} or row.get('reply_mode') == 'future_im'


def _life_opportunities(world: dict, profile: InitiativeProfile, latest: dict | None, now: float) -> list[dict]:
    """Current choices only; never manufacture an observation or missed windows."""
    if (profile.rank < 2 or (world.get('rhythm') or {}).get('availability') != 'open'
            or ((world.get('world') or {}).get('schedule') or {}).get('current_class')):
        return []
    current = world.get('current') if world.get('stale') is False else None
    if isinstance(current, dict):
        try:
            observed = datetime.fromisoformat(current['occurred_at'])
            if observed.utcoffset() is None or observed.timestamp() > now or not current.get('source_id'):
                current = None
        except (KeyError, TypeError, ValueError):
            current = None
    else:
        current = None
    window = profile.letter_followup_delay
    start = int(now // window) * window
    result = []
    if current is not None:
        # Window deadlines can advance, but identity depends only on this source:
        # a successful share cannot recycle the same observed activity next slot.
        result.append({'source_id': current['source_id'], 'kind': 'life_share',
                       'not_before': max(start, observed.timestamp()), 'expires_at': start + window})
    if profile.rank >= 3:
        item = {'source_id': current['source_id'] if current is not None else f'initiative:affection:{start}',
                'kind': 'affection_checkin', 'window_start': start,
                'not_before': start, 'expires_at': start + window}
        if latest is not None:
            item['previous_source_id'] = f"reply:{latest['letter_id']}:{latest.get('reply_revision', 1)}"
        result.append(item)
    return result


def make_context(rows: list[dict], *, now: float, world: dict | None = None,
                 profile: InitiativeProfile | None = None) -> dict:
    """App-owned opportunity projection; workers never write the mailbox."""
    profile = profile if profile is not None else profile_from_rows(rows)
    delivered = [row for row in rows if row.get('origin') == 'proactive'
                 and not _is_im(row) and row.get('letter_status') == 'COMPLETED']
    remaining = max(0, profile.letter_daily_limit - sum(
        _stamp(row.get('published_at', row.get('created_at'))) > now - DAY for row in delivered))
    blocked = any(row.get('letter_status') in {'PENDING', 'PROCESSING'} or (
        _is_im(row) and row.get('delivery_status') in {'RECEIVED', 'GENERATING', 'GENERATED', 'SENDING', 'DELIVERY_UNCONFIRMED'})
        for row in rows)
    unread = any(not row.get('is_read', 0) for row in delivered)
    latest = max((row for row in rows if row.get('origin') != 'proactive' and row.get('content')
                  and _stamp(row.get('created_at')) <= now
                  and (row.get('delivery_status') == 'DELIVERED' if _is_im(row) else row.get('letter_status') == 'COMPLETED')),
                 key=lambda row: _stamp(row.get('created_at')), default=None)
    candidates = _life_opportunities(world or {}, profile, latest, now)
    if latest:
        source = f"reply:{latest['letter_id']}:{latest.get('reply_revision', 1)}"
        candidates.append({'source_id': source, 'kind': 'correspondence_followup',
                           'not_before': _stamp(latest.get('created_at')) + profile.letter_followup_delay,
                           'expires_at': _stamp(latest.get('created_at')) + 7 * DAY})
        # Long silence may itself become a reason to consider contact, but only
        # after the relationship permits it. This remains an opportunity; the
        # model can still defer when the silence is ordinary for the context.
        if profile.letter_silence_delay is not None:
            quiet_at = _stamp(latest.get('created_at')) + profile.letter_silence_delay
            candidates.append({'source_id': source, 'kind': 'relationship_checkin',
                               'not_before': quiet_at,
                               'expires_at': quiet_at + 7 * DAY})
    # Shared matters retain their own evidence and identity alongside present
    # life/affection opportunities; no generated message invents new life facts.
    for item in (world or {}).get('shared', []):
        if item.get('actor') == 'user' and item.get('status') in {'planned', 'ongoing', 'awaiting_user'}:
            try:
                changed_at = datetime.fromisoformat(item['updated_at']).timestamp()
            except (KeyError, TypeError, ValueError):
                continue
            candidates.append({'source_id': item.get('source_id', ''),
                               'kind': 'shared_followup', 'project_id': item.get('id'),
                               'not_before': changed_at + profile.letter_followup_delay,
                               'expires_at': changed_at + 7 * DAY,
                               'version': item.get('updated_at')})
    used = {row.get('proactive_candidate_id') for row in rows if row.get('origin') == 'proactive'
            and (row.get('delivery_status') == 'DELIVERED' if _is_im(row) else row.get('letter_status') == 'COMPLETED')}
    for item in candidates:
        identity = {key: value for key, value in item.items()
                    if key not in {'not_before', 'expires_at', 'relationship_tier', 'relationship_caution'}}
        if item['kind'] == 'affection_checkin':
            # A refreshed life observation or new dialogue anchor is context,
            # not permission to repeat the same current-window contact.
            identity = {'kind': item['kind'], 'window_start': item['window_start']}
        item['id'] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
        item['relationship_tier'] = profile.tier
        item['relationship_caution'] = profile.caution
    return {'updated_at': now, 'remaining': remaining, 'blocked': blocked, 'unread': unread,
            # Keep the old boolean for diagnostics/backward-compatible clients.
            'relationship_high': profile.rank >= 3,
            'initiative_profile': profile.public_view(),
            'candidates': [item for item in candidates if item['id'] not in used and item['source_id']]}


def scan_pending(data_root: Path, *, now: float | None = None,
                 excluded_ids: set[str] | None = None) -> dict:
    now = time.time() if now is None else now
    root = data_root / 'proactive'
    prefs = load_settings(data_root)
    context = read_json(root / 'context.json')
    old = read_json(root / 'pending.json')
    selected = {}
    if (prefs['enabled'] and context.get('remaining', 0) > 0
            and not context.get('blocked', True) and not context.get('unread', True)
            and 0 <= now - _stamp(context.get('updated_at')) <= 7 * DAY):
        for candidate in context.get('candidates', []):
            if (isinstance(candidate, dict)
                    and candidate.get('id') not in (excluded_ids or set())
                    and _stamp(candidate.get('not_before')) <= now < _stamp(candidate.get('expires_at'))):
                selected = {**candidate, 'prepared_at': old.get('prepared_at', now)
                            if old.get('id') == candidate.get('id') else now}
                break
    if old != selected:
        write_json(root / 'pending.json', selected)
    return selected
