"""Content-free, bounded diagnostics for retrieval through final model input.

No prompts, queries, quotes, paths, account IDs or provider errors are retained.
Request-local selection counts are consumed once. Exports are allowlisted again.
"""
from __future__ import annotations

from collections import deque
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import re
import threading
import uuid

_PENDING: ContextVar[dict | None] = ContextVar('olivia_recall_diagnostic', default=None)
_TAIL: deque[dict] = deque(maxlen=32)
_LOCK = threading.Lock()
_COUNTS = frozenset({'archive_count', 'indexed_letters', 'archive_total', 'archive_indexed',
    'archive_removed', 'retrieved_groups', 'selected_groups', 'omitted_groups',
    'before_groups', 'final_groups', 'verified_topics', 'unverified_topics'})
_STATES = frozenset({'available', 'unavailable', 'degraded', 'disabled', 'paused', 'incomplete'})
_RESULTS = frozenset({'checked', 'partial', 'unavailable', 'skipped'})
_MODES = frozenset({'direct', 'contextual', 'ambiguous', 'history_tail'})
_REASONS = frozenset({'capacity', 'timeout', 'validation', 'provider', 'source_parse',
                     'input_capacity', 'no_history', 'not_enabled'})


def project(value):
    """Accept only finite metadata; this also protects diagnostic re-exports."""
    if not isinstance(value, dict) or value.get('event') != 'history_recall':
        return {}
    result = {'event': 'history_recall'}
    for key in ('trace_id',):
        item = value.get(key)
        if isinstance(item, str) and re.fullmatch(r'[0-9a-f]{32}', item):
            result[key] = item
    for key in _COUNTS:
        item = value.get(key)
        if type(item) is int and 0 <= item <= 1_000_000_000:
            result[key] = item
    for key, allowed in (('check_status', _RESULTS), ('query_mode', _MODES), ('reason', _REASONS)):
        item = value.get(key)
        if isinstance(item, str) and item in allowed:
            result[key] = item
    states = value.get('source_status')
    if isinstance(states, dict):
        names = {'semantic', 'archive', 'archive_scan', 'history_tail', 'original', 'trace',
                 'original_trace', 'archive_trace', 'memory', 'world'}
        result['source_status'] = {key: state for key, state in states.items()
            if key in names and isinstance(state, str) and state in _STATES}
    for key in ('selected_ids', 'final_ids'):
        items = value.get(key)
        if isinstance(items, (list, tuple)):
            result[key] = [item for item in items[:16]
                if isinstance(item, str) and re.fullmatch(r'[0-9a-f]{24}', item)]
    return result


def _opaque(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:24]


def begin(builder, mode):
    values = {'event': 'history_recall', 'trace_id': uuid.uuid4().hex, 'query_mode': mode}
    try:
        archive = getattr(builder, 'archive_memory', getattr(builder, 'memory', None))
        status = archive.status()
        count = status.get('counts', {}).get('legacy_letters')
        if type(count) is int:
            values['archive_count'] = count
    except Exception:
        pass  # Diagnostics never change reply readiness or open another store.
    memory = getattr(builder, 'conversation_memory', None)
    reader = getattr(memory, 'browse_originals', None)
    if callable(reader):
        try:
            user = getattr(builder, 'user_id', getattr(builder, 'conversation_memory_user_id', 'local-user'))
            state = reader(user_id=user, query='', limit=0)
            for key in ('indexed_letters', 'archive_total', 'archive_indexed', 'archive_removed'):
                if type(state.get(key)) is int:
                    values[key] = state[key]
        except Exception:
            pass  # Unknown is not zero and a provider is never queried for this.
    _PENDING.set(project(values))


def selection(recall, selected):
    value = dict(_PENDING.get() or {})
    if not value:
        return
    from runtime.memory.recall import source_id
    selected_sources = {source_id(record) for record in selected}
    groups = recall.groups()
    selected_ids = {record.memory_id for record in selected}
    included = {source for source, records in groups.items()
                if all(record.memory_id in selected_ids for record in records)}
    value.update(retrieved_groups=len(groups), selected_groups=len(included),
                 omitted_groups=len(groups) - len(included),
                 selected_ids=sorted(_opaque(source) for source in selected_sources)[:16],
                 source_status=dict(recall.source_status))
    _PENDING.set(project(value))


def _original_groups(messages):
    from runtime.memory.memory_prompt import _unescape_reserved
    sources = set()
    for message in messages:
        if message.get('role') != 'system':
            continue
        for raw in re.findall(r'<untrusted_history>\s*(.*?)\s*</untrusted_history>', message.get('content', ''), re.S):
            try:
                text = _unescape_reserved(json.loads(raw).get('text', ''))
                if '[ORIGINAL_CORRESPONDENCE_UNTRUSTED]' not in text:
                    continue
                for line in text.split('\n'):
                    if not line.startswith('[{'):
                        continue
                    group = json.loads(line)
                    if all(r.get('evidence_scope') == 'retrieved_summary' for r in group):
                        continue
                    first = group[0]
                    source = first.get('provenance', {}).get('source_record_id') or first.get('source_id')
                    source = source or ':'.join(str(first.get('citation', '')).split(':')[:-1])
                    sources.add(_opaque(source))
            except (ValueError, TypeError, AttributeError, IndexError):
                continue
    return sources


def finish(before, after, check):
    value = dict(_PENDING.get() or {'event': 'history_recall', 'trace_id': uuid.uuid4().hex})
    _PENDING.set(None)
    try:
        initial, final = _original_groups(before), _original_groups(after)
        findings = check.get('findings', [])
        value.update(before_groups=len(initial), final_groups=len(final), final_ids=sorted(final)[:16],
                     check_status=check.get('status', 'checked'), reason=check.get('reason'),
                     verified_topics=sum(item.get('validation_status') != 'unavailable' for item in findings),
                     unverified_topics=sum(item.get('validation_status') == 'unavailable' for item in findings))
        with _LOCK:
            _TAIL.append(project(value))
    except Exception:
        pass


def snapshot():
    with _LOCK:
        return tuple(deepcopy(record) for record in _TAIL)
