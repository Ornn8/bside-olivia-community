"""Bounded owner-wide IM initiative; no offline backlog or stored transport tokens."""
from datetime import datetime
from statistics import median
import random
import time
from runtime.private_world.life_rhythm import LOCAL
from runtime.personal_chat.initiative_profile import profile_from_rows, unanswered_wait


def delivered(row):
    return row.get('delivery_status') == 'DELIVERED'


def letter_invitation_allowed(chats, letters, now):
    # Both transports share these records. Failed delivery does not count as an invitation.
    recent = [r for r in chats if delivered(r)]
    last_invite = max((float(r.get('created_at', 0)) for r in recent
                       if r.get('letter_invitation')), default=0)
    last_letter = max((float(r.get('created_at', 0)) for r in letters
                       if r.get('content') and r.get('origin') != 'proactive'), default=0)
    preference = next((r for r in reversed(recent) if r.get('letter_preference')), {})
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
    def __init__(self, rows, clock=time.time, interval=None):
        self.rows, self.clock = rows, clock
        # Tests and explicit callers may pin a deterministic interval. Normal
        # runtime derives cadence from the persisted relationship profile.
        self.interval = interval
        self.target = None
        self._scheduled_profile = None
        self.due = clock() + self._next_interval()

    def profile(self):
        return profile_from_rows(self.rows)

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

    def received(self, event, send):
        self.target = (event, send)
        self.due = self.clock() + self._next_interval()

    def pending_followup(self):
        latest = next((r for r in reversed(self.rows) if delivered(r) and 'followup_at' in r
                       and r.get('origin') != 'proactive'), None)
        if not latest or not latest['followup_at']:
            return None
        used = [r for r in self.rows if r.get('followup_source_id') == latest['letter_id']]
        if any(r.get('delivery_status') in {'SENDING','DELIVERED','GENERATING'} for r in used) or len(used) >= 2:
            return None
        if self.clock() > latest['followup_at'] + 7200:
            return None  # Expired appointments are not accumulated offline mail.
        return latest

    def ready(self):
        now = self.clock()
        latest_user = next((r for r in reversed(self.rows) if r.get('origin') != 'proactive'), None)
        if latest_user and latest_user.get('delivery_status') in {'FAILED','GENERATING','GENERATED','SENDING'}:
            return False  # Do not initiate over an unresolved user request (possibly a cancellation).
        followup = self.pending_followup()
        scheduled = followup is not None and now >= followup['followup_at']
        if not self.target:
            return False
        profile = self.profile()
        if not scheduled:
            self._recalibrate_if_relationship_changed(now, profile)
            if now < self.due:
                return False
        local = datetime.fromtimestamp(now, LOCAL)
        if local.hour < 8 or (local.hour == 8 and local.minute < 30):
            return False
        # Reserve budget for attempts too, including SKIP/failure/uncertain sends.
        # The hard ceiling remains bounded, while shallow relationships spend
        # much less of it than close ones.
        attempts = [r for r in self.rows if r.get('origin') == 'proactive'
                    and float(r.get('created_at', 0)) > now - 86400]
        if len(attempts) >= profile.im_attempt_limit and not scheduled:
            return False
        history = [r for r in self.rows if delivered(r)]
        if any(r.get('delivery_status') == 'SENDING' for r in self.rows):
            return False
        preference = next((r for r in reversed(history) if r.get('initiative_preference')), {})
        if not scheduled and preference.get('initiative_preference') == 'pause' and (
                preference.get('pause_until') is None or now < preference['pause_until']):
            return False
        unanswered = 0
        last_proactive_at = 0.0
        for row in reversed(history):
            if row.get('origin') != 'proactive':
                break
            unanswered += 1
            last_proactive_at = max(last_proactive_at, float(row.get('created_at', 0)))
        if scheduled:
            return True
        # An unanswered message first increases "do not disturb" pressure. The
        # pressure decays with time and does not permanently lock initiative;
        # closer relationships recover sooner, shallow ones remain conservative.
        wait = unanswered_wait(profile, _usual_user_gap(history), unanswered)
        if unanswered and now - last_proactive_at < wait:
            return False
        return True

    def attempted(self):
        self.due = self.clock() + self._next_interval()
