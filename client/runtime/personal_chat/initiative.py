"""Bounded owner-wide IM initiative; no offline backlog or stored transport tokens."""
from datetime import datetime
from statistics import median
import random
import time
import hashlib
import logging
import math
import sqlite3
from pathlib import Path
from contextlib import closing
from runtime import proactive_config
from runtime.private_world.life_rhythm import LOCAL
from runtime.personal_chat.initiative_profile import profile_from_rows, unanswered_wait
from .contact_activity import blocks_initiative


def delivered(row):
    return row.get('delivery_status') == 'DELIVERED'


def controls_available(row):
    """Current user authority is independent of an outgoing send receipt."""
    return (row.get('origin') != 'proactive' and not row.get('superseded_by')
            and (delivered(row) or row.get('user_controls_applied') is True))


def letter_invitation_allowed(chats, letters, now):
    # Both transports share these records. Failed delivery does not count as an invitation.
    recent = [r for r in chats if delivered(r)]
    last_invite = max((float(r.get('created_at', 0)) for r in recent
                       if r.get('letter_invitation')), default=0)
    last_letter = max((float(r.get('created_at', 0)) for r in letters
                       if r.get('content') and r.get('origin') != 'proactive'), default=0)
    preference = next((r for r in reversed(chats)
                       if controls_available(r) and r.get('letter_preference')), {})
    paused = preference.get('letter_preference') == 'pause' and (
        preference.get('letter_until') is None or now < preference['letter_until'])
    return not paused and now - max(last_invite, last_letter) >= 7 * 86400


def _usual_user_gap(rows):
    """Estimate this relationship's own reply rhythm instead of imposing one clock."""
    stamps = [float(r.get('created_at', 0)) for r in rows
              if delivered(r) and r.get('origin') != 'proactive' and float(r.get('created_at', 0)) > 0]
    stamps = stamps[-9:]
    gaps = [later - earlier for earlier, later in zip(stamps, stamps[1:])
            if 5 * 60 <= later - earlier <= 7 * 86400]
    return median(gaps) if gaps else None


class Initiative:
    def __init__(self, rows, clock=time.time, interval=None, *, profile_provider=None, state_path=None, active_provider=None,
                 channel_gate=None):
        self.rows, self.clock = rows, clock
        self.profile_provider = profile_provider
        # Whether this channel is the one the owner talks on most (None: no preference).
        self.channel_gate = channel_gate
        self.active_provider = active_provider or (lambda: False)
        # Tests and explicit callers may pin a deterministic interval. Normal
        # runtime derives cadence from the persisted relationship profile.
        self.interval = interval
        self.target = None
        self.state_path = Path(state_path) if state_path is not None else None
        self.binding = None
        self.catch_up = False
        self._state_ok = True
        from runtime.diagnostics.reply_telemetry import InitiativeObserver
        self.observer = InitiativeObserver()
        self._scheduled_profile = None
        self.due = clock() + self._next_interval()

    def profile(self):
        return self.profile_provider() if self.profile_provider is not None else profile_from_rows(self.rows)

    @staticmethod
    def _profile_key(profile):
        return profile.tier, profile.caution

    def _next_interval(self, profile=None):
        if self.interval is not None:
            return self.interval()
        profile = profile or self.profile()
        self._scheduled_profile = self._profile_key(profile)
        return random.uniform(profile.im_interval_min, profile.im_interval_max)

    def _recalibrate_if_relationship_changed(self, now, profile):
        """Apply a newly committed relationship state without firing immediately.

        The incoming message may itself commit a different relationship profile
        after `received()` scheduled the next check. Re-arm once from that
        commit time so a newly close relationship does not inherit a many-hour
        shallow cadence, and new tension can also create more space.
        """
        if self.interval is not None:
            return
        key = self._profile_key(profile)
        if self._scheduled_profile != key:
            self._scheduled_profile = key
            self.due = now + random.uniform(profile.im_interval_min, profile.im_interval_max)
            self.catch_up = False
            self._save()

    def _state(self, value=None):
        if self.state_path is None or self.binding is None:
            return None
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.state_path, timeout=.05)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS due (binding TEXT PRIMARY KEY, at REAL, tier TEXT, caution TEXT, catch_up INTEGER)')
            if value is None:
                return db.execute('SELECT at,tier,caution,catch_up FROM due WHERE binding=?', (self.binding,)).fetchone()
            db.execute('INSERT OR REPLACE INTO due VALUES (?,?,?,?,?)', (self.binding, *value))

    def _save(self, due=None, catch_up=None):
        try:
            profile = self._scheduled_profile or self._profile_key(self.profile())
            self._state((self.due if due is None else due, *profile,
                         int(self.catch_up if catch_up is None else catch_up)))
            self._state_ok = True
            return True
        except (OSError, sqlite3.Error, ValueError):
            self._state_ok = False
            logging.getLogger(__name__).warning('INITIATIVE_STATE_UNAVAILABLE')
            return False

    def _adopt(self, event, send):
        binding = hashlib.sha256(str(getattr(event, 'binding_id', '')).encode()).hexdigest()
        changed = self.target is None or binding != self.binding
        self.target = (event, send)
        if not changed:
            return
        self.binding = binding
        self.catch_up = False
        self.due = self.clock() + self._next_interval()
        try:
            saved = self._state()
            if saved:
                due, tier, caution, catch_up = saved
                if type(due) not in (int, float) or not math.isfinite(due) or due < 0:
                    raise ValueError('INITIATIVE_DUE_INVALID')
                self.due = due
                self._scheduled_profile = (tier, caution)
                self.catch_up = bool(catch_up)
                if self.due <= self.clock():
                    self.due = self.clock() + random.uniform(60, 300)
                    self.catch_up = True
            self._save()
        except (OSError, sqlite3.Error, ValueError):
            self._state_ok = False
            logging.getLogger(__name__).warning('INITIATIVE_STATE_UNAVAILABLE')

    def received(self, event, send):
        self._adopt(event, send)

    def restore(self, event, send):
        """Restore one due per binding; expired offline time creates one jittered opportunity."""
        if self.target is None:
            self._adopt(event, send)

    def free(self):
        """No user turn in flight and no send of uncertain outcome."""
        return not self.active_provider() and not any(r.get('delivery_status') != 'FAILED' and
            blocks_initiative(r, self.clock(), im=True) for r in self.rows)

    def pending_followup(self):
        latest = next((r for r in reversed(self.rows) if controls_available(r) and 'followup_at' in r
                       and r.get('origin') != 'proactive'), None)
        if not latest or not latest['followup_at']:
            return None
        used = [r for r in self.rows if r.get('followup_source_id') == latest['letter_id']]
        if any(r.get('delivery_status') in {'SENDING','DELIVERY_UNCONFIRMED','DELIVERED','GENERATING'} for r in used) or len(used) >= 2:
            return None
        if self.clock() > latest['followup_at'] + 7200:
            return None  # Expired appointments are not accumulated offline mail.
        return latest

    def ready(self):
        reason = self._reason()
        self.observe(reason)
        return reason == 'ready'

    def observe(self, reason):
        self.observer.record(reason, tier=self.profile().tier, now=self.clock(), due=self.due,
                             source=self.binding or '', catch_up=self.catch_up)

    def _reason(self):
        now = self.clock()
        followup = self.pending_followup()
        scheduled = followup is not None and now >= followup['followup_at']
        latest_user = next((r for r in reversed(self.rows) if r.get('origin') != 'proactive' and not r.get('superseded_by')), None)
        if (latest_user and latest_user.get('companion_timing') == 'wait_user'
                and latest_user.get('silence_reason') == 'USER_REQUESTED_WAIT' and not scheduled):
            return 'wait_user'  # A validated due appointment remains eligible.
        if self.active_provider() or latest_user and blocks_initiative(latest_user, now, im=True):
            return 'user_turn_open'
        if (not self.target or not self._state_ok or
                callable(getattr(self.target[1], 'is_available', None)) and not self.target[1].is_available()):
            return 'no_target'
        profile = self.profile()
        if not scheduled:
            self._recalibrate_if_relationship_changed(now, profile)
            if now < self.due:
                return 'not_due'
        local = datetime.fromtimestamp(now, LOCAL)
        # Quiet hours limit her own initiative; an appointment the user asked
        # for (a wake-up call, a reminder) is kept at the agreed time.
        if not scheduled and (local.hour < 8 or (local.hour == 8 and local.minute < 30)):
            return 'quiet_hours'
        # On top of that, whatever quiet hours and limits the cloud has set.
        configured = None if scheduled else proactive_config.initiative_blocked(self.rows, now)
        if configured:
            return configured
        # Reserve budget for attempts too, including SKIP/failure/uncertain sends.
        # The hard ceiling remains bounded, while shallow relationships spend
        # much less of it than close ones.
        attempts = [r for r in self.rows if r.get('origin') == 'proactive'
                    and float(r.get('created_at', 0)) > now - 86400]
        if len(attempts) >= profile.im_attempt_limit and not scheduled:
            return 'attempt_limit'
        history = [r for r in self.rows if delivered(r)]
        if any(r.get('delivery_status') in {'SENDING', 'DELIVERY_UNCONFIRMED'} and
               blocks_initiative(r, now, im=True) for r in self.rows):
            return 'sending_uncertain'
        if any(r.get('delivery_status') != 'FAILED' and blocks_initiative(r, now, im=True) for r in self.rows):
            return 'user_turn_open'
        preference = next((r for r in reversed(self.rows)
                           if controls_available(r) and r.get('initiative_preference')), {})
        if not scheduled and preference.get('initiative_preference') == 'pause' and (
                preference.get('pause_until') is None or now < preference['pause_until']):
            return 'paused'
        unanswered = 0
        last_proactive_at = 0.0
        for row in reversed(history):
            if row.get('origin') != 'proactive':
                break
            unanswered += 1
            last_proactive_at = max(last_proactive_at, float(row.get('created_at', 0)))
        if scheduled:
            return 'ready'  # A contact the owner asked for keeps its time and channel.
        if self.channel_gate is not None and not self.channel_gate():
            return 'deferred'
        # An unanswered message first increases "do not disturb" pressure. The
        # pressure decays with time and does not permanently lock initiative;
        # closer relationships recover sooner, shallow ones remain conservative.
        wait = unanswered_wait(profile, _usual_user_gap(history), unanswered)
        if unanswered and now - last_proactive_at < wait:
            return 'unanswered_wait'
        return 'ready'

    def reserve(self):
        """Durably consume this opportunity before generation, without changing eligibility."""
        return self._save(due=self.clock() + self._next_interval(), catch_up=False)

    def attempted(self):
        self.due = self.clock() + self._next_interval()
        self.catch_up = False
        self._save()
