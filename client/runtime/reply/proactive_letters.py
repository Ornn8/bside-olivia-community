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
from runtime.personal_chat.contact_activity import blocks_initiative

DEFAULTS = {'enabled': True, 'allow_voice': True, 'login_check_enabled': False}
DAY = 86400


def settings(value: dict) -> dict:
    return {key: True if key == 'enabled' else value.get(key) if type(value.get(key)) is bool else default
            for key, default in DEFAULTS.items()}


def load_settings(data_root: Path) -> dict:
    """Keep the legacy field readable; it no longer controls contact eligibility."""
    return settings(read_json(data_root / 'proactive' / 'settings.json'))


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
                 profile: InitiativeProfile | None = None, medium: str = 'letter') -> dict:
    """App-owned opportunity projection; workers never write the mailbox."""
    profile = profile if profile is not None else profile_from_rows(rows)
    delivered = [row for row in rows if row.get('origin') == 'proactive'
                 and not _is_im(row) and row.get('letter_status') == 'COMPLETED']
    remaining = max(0, profile.letter_daily_limit - sum(
        _stamp(row.get('published_at', row.get('created_at'))) > now - DAY for row in delivered))
    blocked = any(row.get('delivery_status') != 'FAILED' and blocks_initiative(row, now) for row in rows)
    unread = sum(not row.get('is_read', 0) and
                 now - 2 * DAY <= _stamp(row.get('published_at', row.get('created_at'))) <= now
                 for row in delivered) >= 2
    latest = max((row for row in rows if row.get('origin') != 'proactive' and row.get('content')
                  and _stamp(row.get('created_at')) <= now
                  and (row.get('delivery_status') == 'DELIVERED' if _is_im(row) else row.get('letter_status') == 'COMPLETED')),
                 key=lambda row: _stamp(row.get('created_at')), default=None)
    # Preserve world selection itself; a frequency promotion cannot introduce
    # affection opportunities or grant intimate expression permissions.
    from dataclasses import replace
    tone_profile = replace(profile, tier=profile.tone_tier or profile.tier)
    candidates = _life_opportunities(world or {}, tone_profile, latest, now)
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
    used_sources = set()
    for row in rows:
        if row.get('origin') != 'proactive' or not (row.get('delivery_status') == 'DELIVERED' if _is_im(row) else row.get('letter_status') == 'COMPLETED'):
            continue
        opportunity = row.get('proactive_opportunity') or {}
        used.add(opportunity.get('id'))
        source = row.get('proactive_source_id')
        try:
            source = source or json.loads(opportunity.get('description', '{}')).get('source_id')
        except (ValueError, TypeError, AttributeError):
            pass
        if source and _is_im(row) != (medium != 'letter'):
            used_sources.add(source)
    from runtime.reply.daily_proactive import daily_candidate
    daily = daily_candidate(rows, now)
    if daily:
        candidates.insert(0, daily)
        unread = False  # One daily letter replaces the unread backlog gate, never the daily cap.
    for item in candidates:
        identity = {key: value for key, value in item.items()
                    if key not in {'not_before', 'expires_at', 'relationship_tier', 'relationship_caution'}}
        if item['kind'] == 'affection_checkin':
            # A refreshed life observation or new dialogue anchor is context,
            # not permission to repeat the same current-window contact.
            identity = {'kind': item['kind'], 'window_start': item['window_start']}
        item['id'] = (daily['id'] if item['kind'] == 'daily_checkin' else
                      hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32])
        item['relationship_tier'] = profile.tone_tier or profile.tier
        item['relationship_caution'] = profile.caution
    return {'updated_at': now, 'remaining': remaining, 'blocked': blocked, 'unread': unread,
            # Keep the old boolean for diagnostics/backward-compatible clients.
            'relationship_high': profile.tone_rank >= 3,
            'initiative_profile': profile.public_view(),
            'candidates': [item for item in candidates if item['id'] not in used and item['source_id']
                           and item['source_id'] not in used_sources]}


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
