"""Bounded owner-wide IM initiative; no offline backlog or stored transport tokens."""
from datetime import datetime
import random
import time


def delivered(row):
    return row.get('delivery_status') == 'DELIVERED'


def letter_invitation_allowed(chats, letters, now):
    # Both transports share this history. Failed delivery does not count as an invitation.
    recent = [r for r in chats if delivered(r)]
    last_invite = max((float(r.get('created_at', 0)) for r in recent
                       if r.get('letter_invitation')), default=0)
    last_letter = max((float(r.get('created_at', 0)) for r in letters
                       if r.get('content') and r.get('origin') != 'proactive'), default=0)
    preference = next((r for r in reversed(recent) if r.get('letter_preference')), {})
    paused = preference.get('letter_preference') == 'pause' and (
        preference.get('letter_until') is None or now < preference['letter_until'])
    return not paused and now - max(last_invite, last_letter) >= 7 * 86400


class Initiative:
    def __init__(self, rows, clock=time.time, interval=None):
        self.rows, self.clock = rows, clock
        self.interval = interval or (lambda: random.uniform(20 * 60, 45 * 60))
        self.target = None
        self.due = clock() + self.interval()

    def received(self, event, send):
        self.target = (event, send)
        self.due = self.clock() + self.interval()

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
        local = datetime.fromtimestamp(now)
        if local.hour < 8 or (local.hour == 8 and local.minute < 30):
            return False
        # Reserve budget for attempts too, including SKIP/failure/uncertain sends.
        attempts = [r for r in self.rows if r.get('origin') == 'proactive'
                    and float(r.get('created_at', 0)) > now - 86400]
        if len(attempts) >= 12 and not scheduled:
            return False
        history = [r for r in self.rows if delivered(r)]
        if any(r.get('delivery_status') == 'SENDING' for r in self.rows):
            return False
        preference = next((r for r in reversed(history) if r.get('initiative_preference')), {})
        if not scheduled and preference.get('initiative_preference') == 'pause' and (
                preference.get('pause_until') is None or now < preference['pause_until']):
            return False
        unanswered = 0
        for row in reversed(history):
            if row.get('origin') != 'proactive':
                break
            unanswered += 1
        return scheduled or unanswered < 2

    def attempted(self):
        self.due = self.clock() + self.interval()
