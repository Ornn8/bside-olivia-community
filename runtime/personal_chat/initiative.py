"""Bounded owner-wide IM initiative; relationship changes behavior, not consent."""
from datetime import datetime
import random
import sys
import time

from runtime.private_world.life_rhythm import LOCAL
from runtime.reply.initiative_policy import (
    initiative_context,
    profile_from_public,
    profile_from_snapshot,
)


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


def _runtime_snapshot():
    """Read an already-loaded composition root only; never import it from here."""
    for name in ('local_server', '__main__'):
        server = sys.modules.get(name)
        port = getattr(server, 'private_world_port', None) if server is not None else None
        if port is None:
            continue
        try:
            return port.snapshot()
        except Exception:
            continue
    return None


class Initiative:
    def __init__(self, rows, clock=time.time, interval=None, snapshot=None):
        self.rows, self.clock = rows, clock
        self.interval = interval
        self.snapshot = snapshot
        self.target = None
        self.due = clock() + self._next_interval()

    def _snapshot(self):
        if self.snapshot is not None:
            try:
                return self.snapshot()
            except Exception:
                return None
        return _runtime_snapshot()

    def _profile(self):
        snapshot = self._snapshot()
        if snapshot is not None:
            return profile_from_snapshot(snapshot)
        for row in reversed(self.rows):
            if isinstance(row.get('initiative_profile'), dict):
                return profile_from_public(row['initiative_profile'])
        return profile_from_snapshot(None)

    def _next_interval(self):
        if self.interval is not None:
            return self.interval()
        profile = self._profile()
        return random.uniform(profile.im_check_min_seconds, profile.im_check_max_seconds)

    def context(self):
        return initiative_context(self.rows, self._snapshot(), self.clock())

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
        if not self.target or (not scheduled and now < self.due):
            return False
        local = datetime.fromtimestamp(now, LOCAL)
        if local.hour < 8 or (local.hour == 8 and local.minute < 30):
            return False
        profile = self._profile()
        # Reserve budget for attempts too, including SKIP/failure/uncertain sends.
        attempts = [r for r in self.rows if r.get('origin') == 'proactive'
                    and float(r.get('created_at', 0)) > now - 86400]
        if len(attempts) >= profile.im_attempt_cap and not scheduled:
            return False
        history = [r for r in self.rows if delivered(r)]
        if any(r.get('delivery_status') == 'SENDING' for r in self.rows):
            return False
        preference = next((r for r in reversed(history) if r.get('initiative_preference')), {})
        if not scheduled and preference.get('initiative_preference') == 'pause' and (
                preference.get('pause_until') is None or now < preference['pause_until']):
            return False

        trailing = []
        for row in reversed(history):
            if row.get('origin') != 'proactive':
                break
            trailing.append(row)
        if not scheduled and len(trailing) >= profile.unanswered_limit:
            # One unanswered cluster may reopen after a relationship-appropriate pause,
            # but each additional unanswered message doubles the quiet period. This
            # avoids both the old permanent lock and repeated nudging every few hours.
            extra = len(trailing) - profile.unanswered_limit
            quiet = min(7 * 86400,
                        profile.unanswered_reset_seconds * (2 ** min(extra, 4)))
            last_at = max((float(row.get('created_at', 0)) for row in trailing), default=0)
            if now - last_at < quiet:
                return False
        return True

    def attempted(self):
        self.due = self.clock() + self._next_interval()
