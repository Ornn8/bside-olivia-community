"""One evening opportunity, shared by QQ and letters; no offline backlog."""
from datetime import datetime

from runtime.personal_chat.engagement import BEIJING, stamp


def daily_candidate(rows, now):
    local = datetime.fromtimestamp(now, BEIJING)
    if not 19 <= local.hour < 22:
        return None
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    failed = [row for row in rows if row.get('origin') == 'proactive'
              and row.get('delivery_status') in {'FAILED', 'SKIPPED'}
              and start + 19 * 3600 <= stamp(row.get('created_at')) <= now]
    if len(failed) >= 3 or any(now - stamp(row.get('created_at')) < 1800 for row in failed):
        return None
    for row in rows:
        if row.get('origin') != 'proactive':
            continue
        im = row.get('channel') in {'qq', 'wechat'} or row.get('reply_mode') == 'future_im'
        delivered = row.get('delivery_status') == 'DELIVERED' if im else row.get('letter_status') == 'COMPLETED'
        at = next((stamp(row.get(k)) for k in ('delivered_at', 'published_at', 'replied_at', 'created_at')
                   if stamp(row.get(k))), 0)
        if delivered and start <= at <= now:
            return None
        # An uncertain send must not become a second message on another channel.
        if row.get('delivery_status') in {'SENDING', 'DELIVERY_UNCONFIRMED'}:
            return None
    return dict(id='daily:' + local.date().isoformat(), source_id='daily:' + local.date().isoformat(),
                kind='daily_checkin', not_before=start + 19 * 3600, expires_at=start + 22 * 3600)


def server_daily(server, now):
    return daily_candidate([*server.store.letters, *server.store.personal_chats], now)


def daily_retry_ids(attempts, now):
    """At most three attempts per evening, spaced thirty minutes, surviving restart."""
    ids = {item['id'] for item in attempts if not item['id'].startswith('daily:')}
    daily = [item for item in attempts if item['id'].startswith('daily:')]
    for item in daily:
        if sum(other['id'] == item['id'] for other in daily) >= 3 or now - item['at'] < 1800:
            ids.add(item['id'])
    return ids
