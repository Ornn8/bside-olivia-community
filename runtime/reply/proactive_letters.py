"""Cheap, file-only opportunity preparation; never generates or delivers letters."""
from __future__ import annotations

import hashlib
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import time

DEFAULTS = {'enabled': False, 'allow_voice': True, 'login_check_enabled': False}
DAY = 86400


def settings(value: dict) -> dict:
    return {key: value.get(key) if type(value.get(key)) is bool else default
            for key, default in DEFAULTS.items()}


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


def make_context(rows: list[dict], *, now: float, world: dict | None = None) -> dict:
    """App-owned opportunity projection; workers never write the mailbox."""
    delivered = [row for row in rows if row.get('origin') == 'proactive'
                 and row.get('letter_status') == 'COMPLETED']
    remaining = max(0, 3 - sum(_stamp(row.get('published_at', row.get('created_at'))) > now - DAY
                              for row in delivered))
    blocked = any(row.get('letter_status') in {'PENDING', 'PROCESSING'} for row in rows)
    unread = any(not row.get('is_read', 0) for row in delivered)
    latest = max((row for row in rows if row.get('origin') != 'proactive' and row.get('content')
                  and row.get('letter_status') == 'COMPLETED'),
                 key=lambda row: _stamp(row.get('created_at')), default=None)
    candidates = []
    if latest:
        source = f"reply:{latest['letter_id']}:{latest.get('reply_revision', 1)}"
        candidates.append({'source_id': source, 'kind': 'correspondence_followup',
                           'not_before': _stamp(latest.get('created_at')) + 1800,
                           'expires_at': _stamp(latest.get('created_at')) + 7 * DAY})
    # Only user-backed shared matters are triggers. Self-generated life updates
    # are context for expression, not an engine that sends itself another letter.
    for item in (world or {}).get('shared', []):
        if item.get('actor') == 'user' and item.get('status') in {'planned', 'ongoing', 'awaiting_user'}:
            try:
                changed_at = datetime.fromisoformat(item['updated_at']).timestamp()
            except (KeyError, TypeError, ValueError):
                continue
            candidates.append({'source_id': item.get('source_id', ''),
                               'kind': 'shared_followup', 'project_id': item.get('id'),
                               'not_before': changed_at + 1800, 'expires_at': changed_at + 7 * DAY,
                               'version': item.get('updated_at')})
    used = {row.get('proactive_candidate_id') for row in delivered}
    for item in candidates:
        identity = {key: value for key, value in item.items() if key not in {'not_before', 'expires_at'}}
        item['id'] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
    return {'updated_at': now, 'remaining': remaining, 'blocked': blocked, 'unread': unread,
            'candidates': [item for item in candidates if item['id'] not in used and item['source_id']]}


def scan_pending(data_root: Path, *, now: float | None = None,
                 excluded_ids: set[str] | None = None) -> dict:
    now = time.time() if now is None else now
    root = data_root / 'proactive'
    prefs = settings(read_json(root / 'settings.json'))
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
