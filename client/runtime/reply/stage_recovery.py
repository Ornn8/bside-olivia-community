"""Private successful stages for one exactly frozen received input.

Only the optional writer store persists an unapproved candidate, outside rows
and diagnostic exports. This does not retry, accept a reply, or make billing idempotent. The
caller owns its bounded retry, lifetime, final quality decision and send ACK.
"""

from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime
from enum import Enum
import hashlib
import json
from pathlib import Path, PurePath
import os
import sqlite3
import time
from threading import RLock
from collections.abc import Mapping
from contextlib import closing

from .reply_orchestrator import ReplyResult, ReplyState
from .reply_reviewer import ReviewResult, ReviewStatus, ReviewVerdict, TrustedReviewEvidence


def _canonical(value):
    if isinstance(value, Enum):
        return ('enum', type(value).__module__, type(value).__qualname__, _canonical(value.value))
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, datetime):
        return ('datetime', value.isoformat())
    if isinstance(value, PurePath):
        return ('path', str(value))
    if is_dataclass(value) and not isinstance(value, type):
        return ('dataclass', type(value).__module__, type(value).__qualname__,
                [(item.name, _canonical(getattr(value, item.name))) for item in fields(value)])
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        return ('mapping', [(key, _canonical(value[key])) for key in sorted(value)])
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, [_canonical(item) for item in value])
    if isinstance(value, (set, frozenset)):
        items = [_canonical(item) for item in value]
        return (type(value).__name__, sorted(items, key=lambda item: json.dumps(item, sort_keys=True)))
    raise TypeError('unsupported stage cache key type')


def canonical_hash(*parts):
    """Return a precise hash, or disable caching for an unsupported key value."""
    try:
        encoded = json.dumps(_canonical(parts), ensure_ascii=False, allow_nan=False,
                             separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, RecursionError):
        return None
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class _ReviewEntry:
    result: ReviewResult
    confirmed: tuple | None = None


class WriterRecoveryStore:
    """Bounded private candidates; no review verdicts or delivery receipts."""
    def __init__(self, root):
        self.path = Path(root) / 'reply-recovery' / 'private-candidates.sqlite3'

    def _connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        connection = sqlite3.connect(self.path, timeout=2)
        try:
            os.chmod(self.path, 0o600)
            connection.execute('CREATE TABLE IF NOT EXISTS candidates '
                '(key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, created REAL NOT NULL, body TEXT NOT NULL)')
            connection.execute('DELETE FROM candidates WHERE created < ?', (time.time() - 86400,))
            connection.commit()
            return connection
        except BaseException:
            connection.close()
            raise

    def load(self, key, fingerprint):
        try:
            with closing(self._connection()) as connection, connection:
                row = connection.execute('SELECT fingerprint, body FROM candidates WHERE key=?', (key,)).fetchone()
            if row is not None and row[0] == fingerprint and isinstance(row[1], str) and 0 < len(row[1]) <= 262144:
                return ReplyResult('', ReplyState.COMPLETED, text=row[1])
        except (OSError, sqlite3.Error):
            pass  # Optional recovery never makes the canonical store unavailable.
        return None

    def save(self, key, fingerprint, result):
        if len(result.text) > 262144:
            return
        try:
            with closing(self._connection()) as connection, connection:
                connection.execute('INSERT OR REPLACE INTO candidates VALUES (?, ?, ?, ?)',
                    (key, fingerprint, time.time(), result.text))
                connection.execute('DELETE FROM candidates WHERE key NOT IN '
                    '(SELECT key FROM candidates ORDER BY created DESC LIMIT 64)')
        except (OSError, sqlite3.Error):
            pass

    def delete(self, key):
        try:
            with closing(self._connection()) as connection, connection:
                connection.execute('DELETE FROM candidates WHERE key=?', (key,))
        except (OSError, sqlite3.Error):
            pass


class StageRecovery:
    def __init__(self, fingerprint, guard, *, writer_store=None, storage_key=None):
        self._lock = RLock()
        self._epoch = 0
        self._results = {}
        self._actual_calls = dict(writer=0, reviewer=0, rewriter=0)
        self._cache_hits = dict(writer=0, reviewer=0, rewriter=0)
        self.fingerprint = None
        self.writer_store, self.storage_key = writer_store, storage_key
        self.bind_input(fingerprint, guard)
        if writer_store is not None and storage_key is not None and self._current(self.token()):
            result = writer_store.load(storage_key, fingerprint)
            if result is not None:
                self._results['writer', 'candidate'] = result

    def bind_input(self, fingerprint, guard):
        if not isinstance(fingerprint, str) or not fingerprint or not callable(guard):
            raise ValueError('stage cache requires a fingerprint and current-input guard')
        with self._lock:
            changed = fingerprint != self.fingerprint
            if changed:
                if self.fingerprint is not None and self.writer_store is not None:
                    self.writer_store.delete(self.storage_key)
                self._epoch += 1
                self._results.clear()
                for counters in (self._actual_calls, self._cache_hits):
                    for stage in counters:
                        counters[stage] = 0
            self.fingerprint, self.guard = fingerprint, guard
            return changed

    def token(self):
        with self._lock:
            return self._epoch

    def invalidate(self, *, preserve_writer=False):
        """Drop private results and prevent late worker completions from saving."""
        with self._lock:
            self._epoch += 1
            self._results.clear()
            self.guard = lambda: False
            if not preserve_writer and self.writer_store is not None:
                self.writer_store.delete(self.storage_key)

    close = invalidate

    @property
    def actual_calls(self):
        with self._lock:
            return dict(self._actual_calls)

    @property
    def cache_hits(self):
        with self._lock:
            return dict(self._cache_hits)

    def mark_call(self, stage):
        with self._lock:
            self._actual_calls[stage] += 1

    def _current(self, token):
        try:
            return token == self._epoch and self.guard() is True
        except Exception:
            return False

    def _require_current(self, token):
        with self._lock:
            if not self._current(token):
                raise RuntimeError('JEV_INPUT_SUPERSEDED')

    def _get(self, stage, key, token, *, count_hit=True):
        with self._lock:
            value = self._results.get((stage, key)) if key is not None and self._current(token) else None
            if value is not None and count_hit:
                self._cache_hits[stage] += 1
            return value

    def _put(self, stage, key, token, value):
        with self._lock:
            if key is None or not self._current(token):
                return False
            self._results[stage, key] = value
            return True

    def get_writer(self, request_id=None):
        result = self._get('writer', 'candidate', self.token())
        return replace(result, request_id=request_id) if result is not None and request_id is not None else result

    def put_writer(self, result, *, token=None):
        # Capture token before awaiting generation; a changed input must never
        # acquire a candidate produced for the previous input revision.
        if (not isinstance(result, ReplyResult) or result.state is not ReplyState.COMPLETED
                or not isinstance(result.text, str) or not result.text.strip() or result.error_code):
            return False
        with self._lock:
            saved = self._put('writer', 'candidate', token, result)
            if saved and self.writer_store is not None:
                self.writer_store.save(self.storage_key, self.fingerprint, result)
            return saved

    def wrap_reviewer(self, base, *, key_context=()):
        return _Reviewer(self, base, key_context)

    def wrap_rewriter(self, base, *, key_context=(), validate=None):
        return _Rewriter(self, base, key_context, validate)


class _Reviewer:
    def __init__(self, cache, base, key_context):
        self.cache, self.base, self.key_context = cache, base, key_context
        self._active = None

    def __getattr__(self, name):
        return getattr(self.base, name)

    def _review(self, method, candidate, context, messages, trusted, invoke):
        token = self.cache.token()
        key = canonical_hash(method, candidate, context, messages, trusted, self.key_context)
        entry = self.cache._get('reviewer', key, token)
        if entry is None:
            self.cache._require_current(token)
            self.cache.mark_call('reviewer')
            result = invoke()
            entry = _ReviewEntry(result)
            if (isinstance(result, ReviewResult) and result.status is ReviewStatus.COMPLETED
                    and result.verdict in {ReviewVerdict.PASS, ReviewVerdict.REWRITE, ReviewVerdict.BLOCK}
                    and result.error_code is None):
                self.cache._put('reviewer', key, token, entry)
        adjudicated_context = (replace(context, intimacy_request=entry.result.intimacy_request)
            if isinstance(entry.result, ReviewResult) and entry.result.status is ReviewStatus.COMPLETED
            else context)
        # The gate adopts only the completed request judgment before repair.
        # Every factual, relationship and permission field stays exactly bound.
        self._active = token, key, canonical_hash(candidate, adjudicated_context, entry.result)
        return entry.result

    def review(self, candidate, context):
        return self._review('review', candidate, context, (), TrustedReviewEvidence(),
                            lambda: self.base.review(candidate, context))

    def review_with_messages(self, candidate, context, generation_messages, *, trusted_evidence=TrustedReviewEvidence()):
        def invoke():
            extended = getattr(self.base, 'review_with_messages', None)
            if not callable(extended):
                return self.base.review(candidate, context)
            if trusted_evidence.character_replies:
                return extended(candidate, context, generation_messages, trusted_evidence=trusted_evidence)
            return extended(candidate, context, generation_messages)
        return self._review('review_with_messages', candidate, context, generation_messages, trusted_evidence, invoke)

    def confirmed_rewrite_evidence(self, candidate, context, review):
        signature = canonical_hash(candidate, context, review)
        active = self._active
        if active is not None and signature is not None and signature != active[2]:
            raise RuntimeError('REWRITE_EVIDENCE_INVALID')
        entry = (self.cache._get('reviewer', active[1], active[0], count_hit=False)
                 if active is not None and signature is not None and signature == active[2] else None)
        if entry is not None and entry.confirmed is not None:
            return entry.confirmed
        # The provider consumes this adjudicated evidence once. Save it with
        # the successful review so a stage retry cannot lose hard-span authority.
        from .reply_quality_gate import _confirmed_rewrite_evidence
        confirmed = _confirmed_rewrite_evidence(self.base, candidate, context, review,
                                               tuple(item.code for item in review.violations))
        if entry is not None:
            self.cache._put('reviewer', active[1], active[0], replace(entry, confirmed=confirmed))
        return confirmed


class _Rewriter:
    def __init__(self, cache, base, key_context, validate):
        self.cache, self.base, self.key_context, self.validate = cache, base, key_context, validate

    def __getattr__(self, name):
        return getattr(self.base, name)

    def _rewrite(self, method, args, invoke):
        token = self.cache.token()
        key = canonical_hash(method, args, self.key_context)
        result = self.cache._get('rewriter', key, token)
        if result is not None:
            return result
        self.cache._require_current(token)
        self.cache.mark_call('rewriter')
        result = invoke()  # Preserve fixed provider/input failure classifications.
        if self.validate is not None:
            try:
                result = self.validate(result)
            except Exception as exc:
                raise RuntimeError('REWRITE_OUTPUT_INVALID') from exc
        if isinstance(result, str) and result.strip():
            self.cache._put('rewriter', key, token, result)
        return result

    def rewrite(self, candidate, context, violation_codes):
        args = candidate, context, violation_codes
        return self._rewrite('rewrite', args, lambda: self.base.rewrite(*args))

    def rewrite_with_messages(self, candidate, context, violation_codes, generation_messages):
        args = candidate, context, violation_codes, generation_messages
        extended = getattr(self.base, 'rewrite_with_messages', None)
        return self._rewrite('rewrite_with_messages', args,
            lambda: extended(*args) if callable(extended) else self.base.rewrite(*args[:3]))

    def rewrite_with_evidence(self, candidate, context, violation_codes, generation_messages, confirmed_violations):
        args = candidate, context, violation_codes, generation_messages, confirmed_violations
        def invoke():
            evidence_aware = getattr(self.base, 'rewrite_with_evidence', None)
            if callable(evidence_aware):
                return evidence_aware(*args)
            extended = getattr(self.base, 'rewrite_with_messages', None)
            return extended(*args[:4]) if callable(extended) else self.base.rewrite(*args[:3])
        return self._rewrite('rewrite_with_evidence', args, invoke)
