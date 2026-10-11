"""Local contact cadence only: never writes relationship scores or permissions."""
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import math
import time

from .initiative_profile import _BASE, _TIERS, _with_caution, profile_from_rows, profile_from_snapshot

BEIJING = timezone(timedelta(hours=8))
WEEK = 7 * 86400


def stamp(value):
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            value = parsed.timestamp() if parsed.utcoffset() is not None else 0
        return float(value) if type(value) in (int, float) and math.isfinite(value) else 0.0
    except (ValueError, OverflowError):
        return 0.0


class Engagement:
    """Cache append-ordered receipt tails, not copies of the whole archive.

    Store lists append new receipts; merged QQ rows retain their original receipt
    rows. On first use walk backwards only to the seven-day boundary. Subsequent
    polls inspect the count/tail and recent rows (including in-place merges).
    Expiry and time-of-day changes are evaluated even without a new receipt.
    """
    def __init__(self, window=WEEK):
        self.window = window
        self.tails = {}
        self.signature = None
        self.events = {}

    @staticmethod
    def fingerprint(rows):
        def boundary(row):
            return (id(row), row.get('created_at'), row.get('user_sent_at'),
                    tuple((row.get('source_messages') or {}).keys()))
        return (id(rows), len(rows), boundary(rows[0]) if rows else None,
                boundary(rows[-1]) if rows else None)

    def recent_rows(self, rows, now, *, newest_first=False):
        if newest_first:
            # Native user letters are inserted at index zero; proactive letters
            # append at the other end and never count as user engagement.
            end = 0
            while end < len(rows) and stamp(rows[end].get('created_at')) > now - self.window:
                end += 1
            return rows[:end]
        key = id(rows)
        cached = self.tails.get(key)
        if (cached is None or cached[0] is not rows or len(rows) < cached[1]
                or len(rows) == cached[1] and cached[3] != (id(rows[-1]) if rows else None)):
            start = len(rows)
            while start and stamp(rows[start - 1].get('created_at')) > now - self.window:
                start -= 1
        else:
            start = cached[2]
        while start < len(rows) and stamp(rows[start].get('created_at')) <= now - self.window:
            start += 1
        self.tails[key] = (rows, len(rows), start, id(rows[-1]) if rows else None)
        return rows[start:]

    def activity(self, letters, chats, now):
        signature = self.fingerprint(letters), self.fingerprint(chats)
        if signature != self.signature:
            self.events = self._read_events(letters, chats, now)
            self.signature = signature
        self.events = {key: at for key, at in self.events.items() if now - WEEK < at <= now}
        dates = set()
        hours = defaultdict(set)
        for at in self.events.values():
            local = datetime.fromtimestamp(at, BEIJING)
            dates.add(local.date())
            hours[local.hour].add(local.date())
        count = len(self.events)
        last_day = sum(at > now - 86400 for at in self.events.values())
        from runtime.cloud_policy import get_policy
        limits = get_policy('proactive')['engagement']
        rank = (4 if last_day >= limits['committed_day_count'] or count >= limits['committed_count'] and len(dates) >= limits['committed_days'] else
                3 if count >= limits['close_count'] and len(dates) >= limits['close_days'] else
                2 if count >= limits['trusted_count'] and len(dates) >= limits['trusted_days'] else
                1 if count >= limits['familiar_count'] else 0)
        usual = len(hours[datetime.fromtimestamp(now, BEIJING).hour]) >= limits['usual_days']
        return rank, usual

    def _read_events(self, letters, chats, now):
        # A canonical merged row repeats source IDs; the original receipt gives
        # each source its timestamp. max preserves newer source receipt dates.
        events = {}
        for medium, rows in (('letter', letters), ('qq', chats)):
            for row in self.recent_rows(rows, now, newest_first=medium == 'letter'):
                if row.get('origin') == 'proactive' or not row.get('content'):
                    continue
                if medium == 'qq' and row.get('channel') != 'qq':
                    continue
                at = stamp(row.get('user_sent_at')) or stamp(row.get('life_received_at')) or stamp(row.get('created_at'))
                if not now - self.window < at <= now:
                    continue
                sources = row.get('source_messages') if medium == 'qq' else None
                ids = sources.keys() if isinstance(sources, dict) and sources else (row.get('letter_id'),)
                for source in ids:
                    key = (medium, row.get('binding_id'), source)
                    events[key] = max(at, events.get(key, 0))
        return events


def adapt_profile(relationship, rank, usual):
    # intense maps to committed *frequency*, not relationship or intimacy.
    # Off-hours reduce only engagement's contribution; existing relationship
    # cadence remains the floor. Tone can never rise above trusted via usage.
    frequency = max(relationship.rank, rank if usual else max(0, rank - 1))
    tone = max(relationship.rank, min(rank, 2))
    result = replace(_BASE[_TIERS[frequency]], reason_floor=_BASE[_TIERS[tone]].reason_floor,
                     tone_tier=_TIERS[tone])
    return _with_caution(result, relationship.caution)


def effective_profile(server, now=None):
    now = time.time() if now is None else now
    store = server.store
    letters, chats = getattr(store, 'letters', []), getattr(store, 'personal_chats', [])
    port = getattr(server, 'private_world_port', None)
    from runtime.private_world.port import NullPrivateWorldPort
    from runtime.memory.effective_relationship import relationship_history
    relationship = (profile_from_snapshot(port.snapshot(), relationship_history(store, now)) if port is not None and not isinstance(port, NullPrivateWorldPort)
                    else profile_from_rows(letters + chats))
    cache = getattr(server, '_contact_engagement', None)
    if cache is None:
        cache = server._contact_engagement = Engagement()
    # Store replacement (account switch/import) must not retain another store.
    if getattr(server, '_contact_engagement_store', None) is not store:
        cache.tails.clear()
        cache.signature = None
        server._contact_engagement_store = store
    return adapt_profile(relationship, *cache.activity(letters, chats, now))
