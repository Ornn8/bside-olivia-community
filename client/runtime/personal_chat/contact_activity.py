"""Bound stale persisted work without changing delivery or retry authority."""
from datetime import datetime
import math

IM_PENDING_SECONDS = 30 * 60
LETTER_PENDING_SECONDS = 2 * 3600
# A failed reply quiets her own initiative only briefly. While replies keep failing, every new
# user turn would otherwise extend a long quiet period, and initiative would never come back.
FAILED_TURN_INITIATIVE_SECONDS = 30 * 60
PENDING_IM = frozenset({'RECEIVED', 'GENERATING', 'GENERATED', 'MEDIA_PENDING', 'SENDING', 'DELIVERY_UNCONFIRMED'})


def recent(row, now, seconds):
    stamps = []
    for key in ('created_at', 'life_received_at'):
        value = row.get(key)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                value = parsed.timestamp() if parsed.utcoffset() is not None else None
            except (ValueError, OverflowError):
                continue
        if type(value) in (int, float) and 0 < value <= now + 300 and math.isfinite(value):
            stamps.append(value)
    # Undated old rows cannot hold the scheduler forever. Live tasks
    # have a separate guard, including tasks resumed from these old records.
    return bool(stamps) and now - max(stamps) < seconds


def blocks_initiative(row, now, *, im=False):
    if row.get('superseded_by'):
        return False
    im = im or row.get('channel') in {'qq', 'wechat'} or row.get('reply_mode') == 'future_im'
    if im:
        if row.get('delivery_status') == 'FAILED' and row.get('origin') != 'proactive':
            return recent(row, now, FAILED_TURN_INITIATIVE_SECONDS)
        return row.get('delivery_status') in PENDING_IM and recent(row, now, IM_PENDING_SECONDS)
    return row.get('letter_status') in {'PENDING', 'PROCESSING'} and recent(row, now, LETTER_PENDING_SECONDS)


def cross_channel_reason(server, *, channel, now, profile, exclude_id=None):
    """Shared local gate, also called immediately before either publication."""
    from .engagement import stamp
    if getattr(server, '_letter_composing_until', 0) > now:
        return 'user_turn_open'
    reason = 'in_flight' if channel == 'letter' else 'user_turn_open'
    if (any(key != exclude_id and not task.done() for key, task in getattr(server, 'reply_jobs', {}).items())
            or getattr(getattr(server, '_personal_chat_service', None), 'active_user_turns', set()) - {exclude_id}):
        return reason
    window = max(600, profile.im_interval_min)
    cooldown = max(1800, profile.im_interval_min)
    from runtime.call.store import quiet_reason
    if quiet_reason(server, now, window):  # on the phone with her, or just hung up
        return reason
    for rows in (getattr(server.store, 'letters', []), getattr(server.store, 'personal_chats', [])):
        for row in reversed(rows):
            if row.get('letter_id') == exclude_id:
                continue
            # FAILED is no longer in flight. The existing latest-failed-turn
            # backoff is owned by the schedulers, not every earlier failure.
            if row.get('delivery_status') != 'FAILED' and blocks_initiative(row, now):
                return reason
            im = row.get('channel') in {'qq', 'wechat'} or row.get('reply_mode') == 'future_im'
            delivered = row.get('delivery_status') == 'DELIVERED' if im else row.get('letter_status') == 'COMPLETED'
            if row.get('origin') != 'proactive':
                received = max(stamp(row.get(k)) for k in ('created_at', 'life_received_at', 'user_sent_at', 'read_boundary_created_at'))
                if 0 <= now - received < window:
                    return reason
            if delivered:
                at = next((stamp(row.get(k)) for k in ('delivered_at', 'published_at', 'private_world_occurred_at', 'replied_at', 'created_at')
                           if stamp(row.get(k))), 0)
                if row.get('origin') != 'proactive' and 0 <= now - at < window:
                    return reason
                if row.get('origin') == 'proactive' and im != (channel != 'letter') and 0 <= now - at < cooldown:
                    return 'not_due'
    return None
