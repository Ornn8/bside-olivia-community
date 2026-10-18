"""Mailbox maintenance projections: originals and model memory remain untouched.

PR #508's comparison policy (whitespace normalization, bigram Dice >= .85,
at least 50 characters) is used only for suggestions. No process or SQL writes.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re

from .letter_backup import MAX_BYTES, MAX_LETTERS, _soul_backup, validate_backup
from runtime.ui_i18n_backend import tr

TERMINAL = {'COMPLETED', 'FAILED', 'CANCELED', 'CANCELLED'}
FAILED = TERMINAL - {'COMPLETED'}
NEAR_BUDGET = 20000


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf8')).hexdigest()


def pair(row):
    return tuple(re.sub(r'\s+', ' ', row.get(k) or '').strip()
                 for k in ('content', 'reply_text'))


def key(row):
    return digest([row.get('source', 'current'), row.get('letter_id'),
                   row.get('content'), row.get('reply_text')])


def stamp(value):
    try:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, str):
            date = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if date.tzinfo is None:
                return None
            value = date.timestamp()
        value = float(value)
        datetime.fromtimestamp(value, timezone.utc)
        return value
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def project(rows, edits, *, include_hidden=False):
    if not edits:
        return list(rows)
    result = []
    for row in rows:
        change = edits.get(key(row), {})
        if change.get('hidden') and not include_hidden:
            continue
        if not change:
            result.append(row)
            continue
        item = deepcopy(row)
        if 'created_at' in change:
            item['created_at'] = change['created_at']
            item['occurred_at'] = datetime.fromtimestamp(change['created_at'], timezone.utc).isoformat()
            item['_maintenance_order'] = change['order']
            metadata = item.get('metadata') or {}
            if isinstance(metadata.get('backup_record'), dict):
                metadata['backup_record']['created_at'] = item['occurred_at']
        result.append(item)
    return result


def source_rows(backup):
    if isinstance(backup, str):
        if len(backup.encode('utf8')) > MAX_BYTES:
            raise ValueError('LETTER_BACKUP_TOO_LARGE')
        backup = json.loads(backup.lstrip('\ufeff'))
    if isinstance(backup, list):
        from .offline_letter_pairs import parse_offline_letter_pair_bytes
        _, pairs = parse_offline_letter_pair_bytes(json.dumps(backup, ensure_ascii=False).encode('utf8'))
        if len(pairs) > MAX_LETTERS:
            raise ValueError('LETTER_BACKUP_TOO_LARGE')
        return tuple({'content': a, 'reply_text': b, 'created_at': None} for a, b in pairs)
    if isinstance(backup, dict) and backup.get('format') == 'soul':
        backup = _soul_backup(backup.get('manifest'))
    return validate_backup(backup)


def excerpt(row):
    return {'key': key(row), 'truncated': any(len(row.get(k) or '') > 400 for k in ('content', 'reply_text'))} | {k: row.get(k) for k in ('letter_id', 'created_at', 'letter_status')} | {
        k: (row.get(k) or '')[:400] for k in ('content', 'reply_text')}


def bigrams(text):
    text = re.sub(r'\s+', '', text)
    return {text[i:i + 2] for i in range(len(text) - 1)}


def preview(rows, edits, backup=None):
    source = source_rows(backup) if backup is not None else None
    result = {'status': 'READY', 'items': [], 'ambiguous': 0, 'near_limited': False,
              'source_count': len(source) if source is not None else None, 'provider_calls': 0,
              'token': digest([rows, edits, source]), '_changes': {}}

    def add(kind, left, right=None, options=()):
        item = {'kind': kind, 'left': excerpt(left),
                'right': excerpt(right) if right else None, 'options': []}
        for label, target, change, keep in options:
            action = {'target': key(target), 'change': change, 'keep': key(keep) if keep else None}
            action_id = digest(action)
            result['_changes'][action_id] = action
            item['options'].append({'id': action_id, 'label': label})
        result['items'].append(item)

    visible = []
    projected = project(rows, edits, include_hidden=True)
    for raw, row in zip(rows, projected):
        change = edits.get(key(raw), {})
        if change:
            add('restore', row, options=[(tr('恢复整理前的显示和时间'), raw, None, None)])
        if change.get('hidden'):
            continue
        if (row.get('letter_status', 'COMPLETED') not in TERMINAL
                or row.get('media_status') in {'PENDING', 'PROCESSING'}
                or row.get('image_status') in {'PENDING', 'PROCESSING', 'RETRY_PENDING'}):
            continue
        visible.append(row)
        if row.get('letter_status') in FAILED:
            add('failed', row, options=[(tr('从信箱收起这封失败信'), row, {'hidden': 'failed'}, None)])
        # Unlike the standalone heuristic, absence of native fields is not proof
        # of import. Only explicit external-tool provenance permits bulk hiding.
        if row.get('imported_by'):
            add('imported', row, options=[(tr('收起旧工具导入信'), row, {'hidden': 'imported'}, None)])

    index = defaultdict(list)
    for row in visible:
        if any(pair(row)):
            index[pair(row)].append(row)
    for group in index.values():
        completed = [r for r in group if r.get('letter_status', 'COMPLETED') == 'COMPLETED']
        completed.sort(key=lambda r: (not r.get('read_only', False), stamp(r.get('created_at')) is not None), reverse=True)
        for row in completed[1:]:
            add('duplicate', row, completed[0], [(tr('保留右侧，收起左侧'), row, {'hidden': 'duplicate'}, completed[0])])

    # Cache bigrams and bound expensive comparisons. Exact matching always covers
    # the entire mailbox; partial near scans are explicitly reported to the UI.
    candidates = [row for group in index.values()
                  for row in group[:1]
                  if row.get('letter_status', 'COMPLETED') == 'COMPLETED'
                  and any(len(text) >= 50 for text in pair(row))]
    grams = {}
    budget = NEAR_BUDGET

    def similar(a, b):
        nonlocal budget
        if budget <= 0:
            result['near_limited'] = True
            return False
        budget -= 1
        for field in ('content', 'reply_text'):
            av, bv = a.get(field) or '', b.get(field) or ''
            if min(len(av), len(bv)) < 50:
                continue
            for text in (av, bv):
                if text not in grams:
                    grams[text] = bigrams(text)
            ga, gb = grams[av], grams[bv]
            if ga and gb and 2 * len(ga & gb) / (len(ga) + len(gb)) >= .85:
                return True
        return False

    if source is None:
        for i, a in enumerate(candidates):
            if budget <= 0:
                result['near_limited'] = True
                break
            for b in candidates[i + 1:]:
                if budget <= 0:
                    result['near_limited'] = True
                    break
                if similar(a, b):
                    add('near', a, b, [(tr('保留左侧，收起右侧'), b, {'hidden': 'near'}, a),
                                       (tr('保留右侧，收起左侧'), a, {'hidden': 'near'}, b)])
    else:
        sources = defaultdict(list)
        for position, row in enumerate(source):
            sources[pair(row)].append((position, row))
        for match, group in sources.items():
            position, row = group[0]
            matches = index.get(match, [])
            if not matches:
                near = next((r for r in candidates if budget > 0 and similar(row, r)), None) if any(len(t) >= 50 for t in match) else None
                add('source_near' if near else 'missing', row, near)
                if budget <= 0:
                    result['near_limited'] = True
                continue
            times = {stamp(r.get('created_at')) for _, r in group}
            if len(times) != 1:
                result['ambiguous'] += 1
                add('ambiguous', row, matches[0])
                continue
            time = times.pop()
            for existing in matches:
                if time is not None and (stamp(existing.get('created_at')) != time or existing.get('_maintenance_order') != position):
                    add('time', existing, row, [(tr('按备份修复时间和顺序'), existing,
                                                {'created_at': time, 'order': position}, None)])
                else:
                    add('same', existing, row)
        for match, group in index.items():
            if match not in sources:
                add('library_only', group[0])
    return result


def apply_selection(plan, selected, edits):
    if not isinstance(selected, list) or any(not isinstance(i, str) or i not in plan['_changes'] for i in selected):
        raise ValueError('LETTER_MAINTENANCE_INVALID')
    chosen = [plan['_changes'][i] for i in dict.fromkeys(selected)]
    hidden = {a['target'] for a in chosen if (a['change'] or {}).get('hidden')}
    reset = {a['target'] for a in chosen if a['change'] is None}
    if any(a['keep'] in hidden for a in chosen) or any(a['target'] in reset and a['change'] is not None for a in chosen):
        raise ValueError('LETTER_MAINTENANCE_CONFLICT')
    updated = deepcopy(edits)
    for action in chosen:
        target, change = action['target'], action['change']
        if change is None:
            updated.pop(target, None)
        else:
            updated.setdefault(target, {}).update(change)
    return updated
