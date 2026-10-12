"""Durable queue of signed JEV receipts that settle off the reply path.

A judgment's answers are returned as soon as its signed receipt has been
verified and written here. Worker threads then post each receipt to the relay,
which settles every operation exactly once however often it is replayed. A
receipt leaves the queue only after the relay confirmed it, so a crash, a lost
acknowledgement or a network failure never drops or doubles a charge: whatever
is left is retried, and settled again on the next start.
"""
import collections
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

DIRECTORY = 'jev-settlements'
SCHEMA = 'jev-settlement/1'
BACKOFF_SECONDS = (2.0, 10.0, 30.0, 120.0, 600.0)
HELD_RETRY_SECONDS = 1800.0
OTHER_ACCOUNT_RETRY_SECONDS = 300.0
WORKERS = 2
RECENT_LIMIT = 512
# How many receipts of one account may keep failing before judgments stop being
# answered ahead of their settlement.
MAX_STUCK = 4
# Rejections that repeat until the account itself changes, or the relay's answer
# disagrees with the receipt. They are kept and retried slowly.
HELD_CODES = frozenset({'JEV_BILLING_HTTP_401', 'JEV_BILLING_HTTP_402', 'JEV_BILLING_HTTP_403',
                        'JEV_BILLING_HTTP_409', 'JEV_BILLING_RESPONSE_INVALID'})
_DIGEST = re.compile(r'[0-9a-f]{64}')
_NEVER = float('inf')


def _write(path, data):
    """Replace ``path`` with ``data`` so a reader sees the old or the new file only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    descriptor, staging = tempfile.mkstemp(dir=path.parent, prefix='.' + path.stem + '.', suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(staging, path)
                return
            except OSError as error:
                # A scanner or reader can deny the replacement briefly on Windows.
                if getattr(error, 'winerror', None) not in {5, 32, 33} or attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
    except BaseException:
        try:
            os.unlink(staging)
        except OSError:
            pass
        raise


class _Entry:
    __slots__ = ('name', 'path', 'data', 'key', 'due', 'attempts', 'inflight')

    def __init__(self, name, path, data, key, due):
        self.name = name
        self.path = path
        self.data = data
        self.key = key
        self.due = due
        self.attempts = 0
        self.inflight = False


class SettlementOutbox:
    """Receipts by account under ``<root>/jev-settlements``, settled by daemon workers.

    ``settle(key, signed, turn_id, version)`` posts one receipt and returns when the
    relay confirmed it; it raises ``ValueError(code)`` otherwise. ``resolve_key``
    maps an account digest to the saved key, or None while another account is in use.
    ``valid(signed, turn_id, account)`` re-checks a stored receipt's shape.
    """

    def __init__(self, root, settle, resolve_key, valid, *, workers=WORKERS,
                 backoff=BACKOFF_SECONDS, held_retry=HELD_RETRY_SECONDS,
                 other_account_retry=OTHER_ACCOUNT_RETRY_SECONDS, max_stuck=MAX_STUCK):
        self._root = Path(root) / DIRECTORY
        self._settle = settle
        self._resolve_key = resolve_key
        self._valid = valid
        self._workers = workers
        self._backoff = backoff
        self._held_retry = held_retry
        self._other_account_retry = other_account_retry
        self._max_stuck = max_stuck
        self._cond = threading.Condition()
        self._entries = {}
        self._recent = collections.OrderedDict()
        self._threads = []
        self._stopped = False

    # -- queue ---------------------------------------------------------------

    def submit(self, *, turn_id, account, version, signed, key):
        """Persist one verified receipt and queue it. Raises OSError if it cannot be stored."""
        name = hashlib.sha256(signed['receipt']['operation_id'].encode('utf-8')).hexdigest()
        if not _DIGEST.fullmatch(account):
            raise OSError('account digest is not usable as a directory name')
        entry = _Entry(name, self._root / account[:16] / (name + '.json'), {
            'schema': SCHEMA, 'turn_id': turn_id, 'account_key_digest': account, 'billing_version': version,
            'signed': signed, 'created': time.time(), 'state': 'pending'}, key, _NEVER)
        with self._cond:
            if self._stopped:
                raise OSError('settlement outbox is stopped')
            if name in self._entries or name in self._recent:
                return  # The relay settles an operation once; do not queue it twice.
            self._entries[name] = entry
        try:
            _write(entry.path, entry.data)
        except BaseException:
            with self._cond:
                self._entries.pop(name, None)
            raise
        with self._cond:
            entry.due = 0.0
            self._start_workers_locked()
            self._cond.notify_all()

    def recover(self):
        """Queue every receipt left on disk; a held one gets a fresh attempt."""
        found = 0
        try:
            folders = sorted(path for path in self._root.iterdir() if path.is_dir())
        except OSError:
            return 0
        for folder in folders:
            try:
                files = sorted(folder.iterdir())
            except OSError:
                continue
            for path in files:
                if path.name.startswith('.') and path.suffix == '.tmp':
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    continue
                entry = self._load(folder, path)
                if entry is None:
                    continue
                with self._cond:
                    if self._stopped or entry.name in self._entries:
                        continue
                    entry.due = 0.0
                    self._entries[entry.name] = entry
                    found += 1
        if found:
            with self._cond:
                self._start_workers_locked()
                self._cond.notify_all()
        return found

    def _load(self, folder, path):
        # An unreadable or unrecognised file stays where it is: it may be a receipt.
        if path.suffix != '.json':
            return None
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            signed = data['signed']
            account = data['account_key_digest']
            name = hashlib.sha256(signed['receipt']['operation_id'].encode('utf-8')).hexdigest()
            if (data['schema'] != SCHEMA or not isinstance(account, str) or not _DIGEST.fullmatch(account)
                    or path.stem != name or folder.name != account[:16]
                    or data['state'] not in ('pending', 'held') or not isinstance(data['billing_version'], str)
                    or not isinstance(data['turn_id'], str) or not self._valid(signed, data['turn_id'], account)):
                return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        return _Entry(name, path, data, None, 0.0)

    # -- account state -------------------------------------------------------

    def congested(self, account):
        """True while the relay refuses this account's receipts or keeps failing them.

        Judgments must not run ahead of settlement then: the caller settles in line,
        so a refusal or an outage fails the reply as it always did.
        """
        with self._cond:
            stuck = 0
            for entry in self._entries.values():
                if entry.data['account_key_digest'] != account:
                    continue
                if entry.data['state'] == 'held':
                    return True
                stuck += 1 if entry.attempts else 0
            return stuck >= self._max_stuck

    def expedite(self, account):
        """The relay just accepted a settlement: retry this account's waiting receipts now."""
        with self._cond:
            for entry in self._entries.values():
                if (entry.data['account_key_digest'] == account and not entry.inflight
                        and entry.due != _NEVER):
                    entry.due = 0.0
            self._cond.notify_all()

    # -- workers -------------------------------------------------------------

    def _start_workers_locked(self):
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        while not self._stopped and len(self._threads) < self._workers:
            thread = threading.Thread(target=self._run, daemon=True,
                                      name='jev-settlement-%d' % len(self._threads))
            self._threads.append(thread)
            thread.start()

    def _next_locked(self):
        now = time.monotonic()
        best, soonest = None, None
        for entry in self._entries.values():
            if entry.inflight:
                continue
            if entry.due <= now:
                if best is None or entry.due < best.due:
                    best = entry
            elif entry.due != _NEVER and (soonest is None or entry.due < soonest):
                soonest = entry.due
        return best, (None if soonest is None else max(0.0, soonest - now))

    def _run(self):
        while True:
            with self._cond:
                while True:
                    if self._stopped:
                        return
                    entry, wait = self._next_locked()
                    if entry is not None:
                        break
                    self._cond.wait(wait)
                entry.inflight = True
            self._attempt(entry)

    def _attempt(self, entry):
        data = entry.data
        account = data['account_key_digest']
        try:
            key = entry.key or self._resolve_key(account)
        except Exception:
            key = None
        code = None  # None: this account is not signed in right now.
        if key is not None:
            try:
                self._settle(key, data['signed'], data['turn_id'], data['billing_version'])
                code = ''
            except ValueError as error:
                code = str(error) or 'JEV_BILLING_UNAVAILABLE'
            except Exception:
                code = 'JEV_BILLING_UNAVAILABLE'
        held_data = None
        if code == '':
            try:
                entry.path.unlink()
            except OSError:
                pass  # Settled; a later start replays it and the relay answers "already settled".
        elif code in HELD_CODES:
            held_data = dict(data, state='held', code=code)
            try:
                _write(entry.path, held_data)
            except OSError:
                pass
        with self._cond:
            entry.inflight = False
            now = time.monotonic()
            if code == '':
                self._entries.pop(entry.name, None)
                self._recent[entry.name] = True
                while len(self._recent) > RECENT_LIMIT:
                    self._recent.popitem(last=False)
            elif code is None:
                entry.due = now + self._other_account_retry
            elif code in HELD_CODES:
                entry.data = held_data
                entry.due = now + self._held_retry
            elif data['state'] == 'held':
                entry.due = now + self._held_retry
            else:
                entry.attempts += 1
                entry.due = now + self._backoff[min(entry.attempts, len(self._backoff)) - 1]
            self._cond.notify_all()

    # -- lifecycle -----------------------------------------------------------

    def flush(self, timeout):
        """Wait until nothing is running or due. Returns False if it timed out."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                now = time.monotonic()
                if not any(entry.inflight or entry.due <= now for entry in self._entries.values()):
                    return True
                if deadline <= now:
                    return False
                self._cond.wait(min(deadline - now, 0.25))

    def close(self, timeout=0.0):
        """Stop the workers. Receipts still queued stay on disk for the next start."""
        with self._cond:
            self._stopped = True
            threads = list(self._threads)
            self._cond.notify_all()
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def pending(self):
        with self._cond:
            return len(self._entries)

    def held(self):
        with self._cond:
            return sum(1 for entry in self._entries.values() if entry.data['state'] == 'held')
