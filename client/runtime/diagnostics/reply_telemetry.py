"""Content-free delivery observations. Never part of the reply transaction."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from .telemetry_codes import ERROR_CODES

TRANSPORT_STATUSES = frozenset({'connecting', 'connected', 'reconnecting', 'closed', 'auth_required'})
TRANSPORT_ERRORS = frozenset({'NONE', 'TIMEOUT', 'CONNECTION', 'CLIENT', 'OTHER'})
IN_FLIGHT = frozenset({'none', 'queued', 'generation', 'media', 'send', 'unknown'})
NAPCAT_STATES = frozenset({'IDLE', 'DOWNLOADING', 'READY', 'STARTING', 'AWAITING_QQ_LOGIN',
                         'ONEBOT_PROBING', 'ONEBOT_CONFIG_PENDING', 'ONEBOT_READY', 'FAILED', 'UNKNOWN'})
NAPCAT_REASONS = frozenset({'probe', 'status_probe_failed', 'confirmed_offline_restart',
                          'process_exit_restart', 'watchdog_failed'})
V2_DEFAULTS = dict(error_code='UNKNOWN', close_code=None, transport_error=None,
                   in_flight=None, napcat_state=None, napcat_reason=None)

STAGES = frozenset({'app', 'binding', 'recharge_page', 'received', 'generation',
                    'validation', 'media', 'send', 'reply'})
STATUSES = frozenset({'started', 'ok', 'failed', 'unknown', 'skipped'})
CHANNELS = frozenset({'client', 'qq', 'wechat', 'letter', 'proactive'})
MODELS = frozenset({'claude-sonnet-5-5', 'claude-opus-5-5', 'claude-opus-4-6',
    'claude-fable-5-1', 'claude-haiku-5-5', 'gemini-3.8-flash', 'qwen3.7-flash',
    'qwen3.8-max', 'gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-luna', 'unknown'})
ERRORS = frozenset({'none', 'timeout', 'balance', 'validation', 'provider', 'media', 'send', 'other'})
CURRENT = ContextVar('reply_telemetry', default=None)
_collector = None


def error_group(code):
    # Classification is intentionally lossy; arbitrary exception prose never leaves the device.
    if not code:
        return 'none'
    code = str(code)
    if not re.fullmatch(r'[A-Z_0-9]{1,96}', code):
        return 'other'
    if 'TIMEOUT' in code: return 'timeout'
    if 'BALANCE' in code or 'QUOTA' in code: return 'balance'
    if any(s in code for s in ('INVALID', 'VALIDATION', 'REVIEW', 'REWRITE', 'SCHEMA', 'QUALITY')): return 'validation'
    if 'DELIVERY' in code or 'SEND' in code: return 'send'
    if any(s in code for s in ('IMAGE', 'VIDEO', 'AUDIO', 'TTS', 'MEDIA')): return 'media'
    if code.startswith(('LLM_', 'PROVIDER_', 'JEV_PROVIDER_')): return 'provider'
    return 'other'


class Collector:
    def __init__(self, root, *, version='unknown', clock=time.time):
        self.path = Path(root) / 'reply-telemetry.sqlite3'
        self.clock = clock
        self.version = version if re.fullmatch(r'\d+\.\d+\.\d+(?:[+.-][a-zA-Z0-9.-]{1,40})?', version) else 'unknown'
        self.lock = threading.RLock()
        self.last_error = None
        self.schema = 1
        self.model = lambda: 'unknown'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS queue (seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE, at REAL, payload TEXT);
                CREATE TABLE IF NOT EXISTS observed (trace TEXT PRIMARY KEY, at REAL, value TEXT);''')
            self.enabled = self._get(db, 'enabled', 'true') == 'true' and os.environ.get('OLIVIA_TELEMETRY_ENABLED', '1') != '0'
            self.since = float(self._get(db, 'since', '0'))
            self.owner = self._get(db, 'owner', '')
            self.salt = self._get(db, 'salt', uuid.uuid4().hex)
            if self.enabled and self._get(db, 'was_disabled', 'false') == 'true':
                self.since, self.salt = self.clock(), uuid.uuid4().hex
                self._put(db, 'since', str(self.since))
                db.execute('DELETE FROM queue')
                db.execute('DELETE FROM observed')
            self._put(db, 'was_disabled', json.dumps(not self.enabled))
            self._put(db, 'salt', self.salt)
            if not self.enabled:
                db.execute('DELETE FROM queue')
                db.execute('DELETE FROM observed')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=.05)
        try:
            with db: yield db
        finally: db.close()

    @staticmethod
    def _get(db, key, fallback):
        row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else fallback

    @staticmethod
    def _put(db, key, value):
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, value))

    def account(self, key):
        digest = hashlib.sha256(key.encode()).hexdigest() if key else ''
        with self.lock:
            if digest == self.owner: return False
            since, salt = self.clock(), uuid.uuid4().hex
            with self._db() as db:
                self._put(db, 'owner', digest)
                self._put(db, 'since', str(since))
                self._put(db, 'salt', salt)
                db.execute('DELETE FROM queue')
                db.execute('DELETE FROM observed')
            self.owner, self.since, self.salt = digest, since, salt
            self.schema = 1
            if digest: self.emit('app', 'ok')
            return True

    def set_enabled(self, enabled):
        if type(enabled) is not bool or enabled and (not self.owner or os.environ.get('OLIVIA_TELEMETRY_ENABLED', '1') == '0'):
            raise ValueError('TELEMETRY_SETTING_INVALID')
        with self.lock:
            if enabled == self.enabled: return
            since, salt = self.clock(), uuid.uuid4().hex
            with self._db() as db:
                for key, value in {'enabled': json.dumps(enabled), 'was_disabled': json.dumps(not enabled),
                                   'since': str(since), 'salt': salt}.items():
                    self._put(db, key, value)
                db.execute('DELETE FROM queue')
                db.execute('DELETE FROM observed')
            self.enabled, self.since, self.salt = enabled, since, salt
            if enabled: self.emit('app', 'ok')

    def status(self):
        return {'enabled': self.enabled, 'last_error': self.last_error}

    def trace(self, source):
        return hmac.new(self.salt.encode(), str(source).encode(), hashlib.sha256).hexdigest()

    def _event(self, stage, status, *, source='', channel='client', attempt=0, duration_ms=None,
               error=None, model=None, transport=None):
        valid = status in TRANSPORT_STATUSES if stage == 'transport' else stage in STAGES and status in STATUSES
        if stage == 'transport' and channel != 'qq':
            return None
        if not self.enabled or not self.owner or not valid: return None
        if model is None:
            try: model = self.model()
            except Exception: model = 'unknown'
        extra = dict(V2_DEFAULTS, error_code='NONE' if not error else
                     error if isinstance(error, str) and error in ERROR_CODES else 'UNKNOWN')
        if stage == 'transport':
            fields = transport if isinstance(transport, dict) else {}
            code = fields.get('close_code')
            extra['close_code'] = code if type(code) is int and 1000 <= code <= 4999 else None
            for key, allowed in [('transport_error', TRANSPORT_ERRORS), ('in_flight', IN_FLIGHT),
                                 ('napcat_state', NAPCAT_STATES), ('napcat_reason', NAPCAT_REASONS)]:
                value = fields.get(key)
                extra[key] = value if isinstance(value, str) and value in allowed else None
        return dict(event_id=uuid.uuid4().hex, trace_id=self.trace(source or uuid.uuid4().hex),
            at=int(self.clock() * 1000), stage=stage, status=status,
            channel=channel if channel in CHANNELS else 'client',
            attempt=attempt if type(attempt) is int and 0 <= attempt <= 100 else 0,
            duration_ms=duration_ms if type(duration_ms) is int and 0 <= duration_ms <= 86400000 else None,
            error=error_group(error), model=model if model in MODELS else 'unknown', version=self.version, **extra)

    def _insert(self, db, event):
        if event:
            db.execute('INSERT INTO queue(event_id,at,payload) VALUES (?,?,?)',
                       (event['event_id'], self.clock(), json.dumps(event)))

    def _prune(self, db):
        db.execute('DELETE FROM queue WHERE at < ?', (self.clock() - 7 * 86400,))
        db.execute('DELETE FROM queue WHERE seq NOT IN (SELECT seq FROM queue ORDER BY seq DESC LIMIT 2000)')
        db.execute('DELETE FROM observed WHERE at < ?', (self.clock() - 30 * 86400,))
        db.execute('DELETE FROM observed WHERE trace NOT IN (SELECT trace FROM observed ORDER BY at DESC LIMIT 10000)')

    def emit(self, stage, status, **fields):
        try:
            with self.lock:
                event = self._event(stage, status, **fields)
                if event:
                    with self._db() as db:
                        self._insert(db, event); self._prune(db)
        except (OSError, sqlite3.Error):
            self.last_error = 'TELEMETRY_WRITE_FAILED'

    def observe(self, rows):
        if not self.enabled: return
        try:
            with self.lock, self._db() as db:
                if not self.enabled: return
                for row in rows:
                    created = row.get('created_at', 0)
                    if not isinstance(created, (int, float)) or created < max(self.since, self.clock()-30*86400): continue
                    source = row.get('letter_id')
                    if not source: continue
                    channel = 'proactive' if row.get('origin') == 'proactive' else row.get('channel', 'letter')
                    trace = self.trace(source)
                    previous = db.execute('SELECT value FROM observed WHERE trace=?', (trace,)).fetchone()
                    previous = json.loads(previous[0]) if previous else {}
                    state = row.get('delivery_status') or row.get('letter_status')
                    attempt = row.get('generation_attempts', row.get('diagnostic_attempt', 0))
                    status = {'RECEIVED': 'started', 'PROCESSING': 'started', 'GENERATING': 'started',
                              'GENERATED': 'started', 'SENDING': 'started', 'MEDIA_PENDING': 'started',
                              'DELIVERED': 'ok', 'COMPLETED': 'ok', 'FAILED': 'failed', 'SKIPPED': 'skipped',
                              'DELIVERY_UNCONFIRMED': 'unknown'}.get(state, 'unknown')
                    if state == 'SENDING' and row.get('error_code'): status = 'unknown'
                    fields = dict(source=source, channel=channel, attempt=attempt, model=row.get('diagnostic_model', 'unknown'))
                    current = dict(state=state, attempt=attempt, status=status)
                    if not previous:
                        self._insert(db, self._event('received', 'ok', **fields))
                    if any(previous.get(k) != v for k, v in current.items()):
                        current['changed_at'] = self.clock()
                        self._insert(db, self._event('reply', status, error=row.get('error_code'), **fields))
                        if state in {'SENDING', 'DELIVERED', 'DELIVERY_UNCONFIRMED'}:
                            elapsed = int((self.clock()-previous['changed_at'])*1000) if previous.get('state')=='SENDING' and 'changed_at' in previous else None
                            self._insert(db, self._event('send', status, duration_ms=elapsed, error=row.get('error_code'), **fields))
                    else:
                        current['changed_at'] = previous.get('changed_at', self.clock())
                    for field in ('image_status', 'media_status', 'voice_prepare_status', 'speech_delivery_status'):
                        value = row.get(field)
                        current[field] = value
                        current[field+'_at'] = self.clock() if value != previous.get(field) else previous.get(field+'_at', self.clock())
                        if value and value != previous.get(field):
                            outcome = {'COMPLETED': 'ok', 'READY': 'ok', 'DELIVERED': 'ok', 'FAILED': 'failed',
                                       'UNAVAILABLE': 'failed', 'UNKNOWN': 'unknown', 'NOT_REQUESTED': 'skipped', 'SKIPPED': 'skipped'}.get(value, 'started')
                            elapsed = int((self.clock()-previous[field+'_at'])*1000) if outcome!='started' and field+'_at' in previous else None
                            code = row.get(field.replace('_status', '_error_code')) or ('MEDIA_FAILED' if outcome=='failed' else None)
                            self._insert(db, self._event('media', outcome, duration_ms=elapsed, error=code, **fields))
                    db.execute('INSERT OR REPLACE INTO observed VALUES (?,?,?)', (trace, self.clock(), json.dumps(current)))
                self._prune(db)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            self.last_error = 'TELEMETRY_WRITE_FAILED'

    def batch(self, schema=1):
        with self.lock, self._db() as db:
            self._prune(db)
            events = [dict(json.loads(row[1]), seq=row[0]) for row in
                    db.execute('SELECT seq,payload FROM queue ORDER BY seq LIMIT 100')] if self.enabled else []
            for event in events:
                if schema == 2:
                    for key, value in V2_DEFAULTS.items():
                        event.setdefault(key, value)
                else:
                    for key in V2_DEFAULTS:
                        event.pop(key, None)
                    if event['stage'] == 'transport':
                        event['stage'] = 'binding'
                        event['status'] = 'ok' if event['status'] == 'connected' else 'unknown'
            return events

    def ack(self, ids):
        with self.lock, self._db() as db:
            db.executemany('DELETE FROM queue WHERE event_id=?', [(i,) for i in ids])

    async def upload(self, send):
        try:
            events = self.batch(self.schema)
            if not events: return
            result = await send(events)
            expected = [e['event_id'] for e in events]
            if (not isinstance(result, dict) or set(result) not in ({'accepted'}, {'accepted', 'supported_schemas'})
                    or result.get('accepted') != expected):
                raise ValueError('bad acknowledgment')
            supported = result.get('supported_schemas', [])
            if (not isinstance(supported, list) or any(type(value) is not int for value in supported)):
                raise ValueError('bad capabilities')
            self.ack(expected)
            self.schema = 2 if 2 in supported else 1
            self.last_error = None
        except (OSError, sqlite3.Error, ValueError, RuntimeError, TimeoutError):
            self.schema = 1
            self.last_error = 'TELEMETRY_UPLOAD_FAILED'


def configure(collector):
    global _collector
    _collector = collector


def observe_rows(rows):
    if _collector: _collector.observe(rows)


def emit(stage, status, **fields):
    if _collector: _collector.emit(stage, status, **fields)


def selected_model():
    try:
        model = _collector.model() if _collector else 'unknown'
        return model if model in MODELS else 'unknown'
    except Exception:
        return 'unknown'


@contextmanager
def scope(source, channel, attempt=0):
    token = CURRENT.set(dict(source=source, channel=channel, attempt=attempt))
    try: yield
    finally: CURRENT.reset(token)


def stage(stage, status, *, duration_ms=None, error=None):
    fields = CURRENT.get()
    if fields: emit(stage, status, duration_ms=duration_ms, error=error, **fields)


async def measure(name, work):
    started = time.monotonic()
    stage(name, 'started')
    try:
        result = await work
    except BaseException as exc:
        stage(name, 'unknown' if isinstance(exc, asyncio.CancelledError) else 'failed',
              duration_ms=int((time.monotonic() - started) * 1000), error=str(exc))
        raise
    error = getattr(result, 'error_code', None)
    if name == 'validation' and getattr(result, 'accepted', True) is False:
        error = error or 'REPLY_QUALITY_BLOCKED'
    stage(name, 'failed' if error else 'ok', duration_ms=int((time.monotonic() - started) * 1000), error=error)
    return result
