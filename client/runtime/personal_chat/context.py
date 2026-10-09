"""Frozen per-reply read window; unsent assistant drafts are never delivered turns."""
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime
from bisect import bisect_right

from runtime.reply.conversation_context import conversation_context as chat_context


READ_WINDOW = ContextVar('personal_chat_read_window', default=None)

# Freeze the read contract, not provider responses, generation traces, audio
# payloads and every other field in the mutable outbox. Text strings are immutable.
_READ_FIELDS = frozenset(('letter_id', 'channel', 'binding_id', 'created_at', 'received_sequence',
    'reply_revision', 'letter_status', 'delivery_status', 'origin', 'life_received_at', 'user_sent_at',
    'private_world_occurred_at', 'private_world_reply_sha256', 'content', 'reply_text', 'reply_mode',
    'source_messages', 'incoming_image_observations', 'incoming_media_observations',
    'image_delivery_status', 'image_delivered_at', 'image_description', 'image_plan',
    'media_deliveries', 'media_status', 'material', 'companion_decision'))


def _snapshot(row):
    return {k: deepcopy(v) if isinstance(v, (dict, list, tuple, set)) else v
            for k, v in row.items() if k in _READ_FIELDS}


def freeze_read_window(rows, *, channel, binding_id, current_id):
    rows = list(rows)
    def receipt(index, row):
        sequence = row.get('received_sequence')
        return (float(row.get('created_at', 0)), sequence if type(sequence) is int else index)
    current = next(((i, r) for i, r in enumerate(rows) if r.get('letter_id') == current_id), None)
    cutoff = (float('inf'), float('inf'))
    if current:
        index, item = current
        stamp, sequence = receipt(index, item)
        cutoff = (float(item.get('read_boundary_created_at', stamp)), item.get('read_boundary_sequence', sequence))
    def same(row):
        return row.get('channel') == channel and row.get('binding_id') == binding_id
    def timestamp(row):
        try:
            stamp = datetime.fromisoformat(row['user_sent_at'].replace('Z', '+00:00'))
            if stamp.tzinfo:
                return stamp.timestamp()
        except (KeyError, AttributeError, TypeError, ValueError):
            pass
        return float(row.get('created_at', 0))
    delivered, pending = [], []
    pending_states = {'RECEIVED', 'GENERATING', 'GENERATED', 'MEDIA_PENDING', 'FAILED',
                      'SENDING', 'SKIPPED', 'DELIVERY_UNCONFIRMED'}
    for index, row in enumerate(rows):
        if not isinstance(row.get('letter_id'), str) or row['letter_id'] == current_id or row.get('read_only'):
            continue
        order = receipt(index, row)
        if order > cutoff:
            continue
        if (row.get('delivery_status') == 'DELIVERED' if row.get('channel') in {'qq', 'wechat'}
                else row.get('letter_status') == 'COMPLETED'):
            delivered.append({**_snapshot(row), '_receipt_order': order})
        elif (same(row) and row.get('origin') != 'proactive' and not row.get('superseded_by')
              and row.get('delivery_status') in pending_states):
            item = {key: deepcopy(row[key]) for key in ('letter_id', 'channel', 'binding_id', 'created_at',
                    'life_received_at', 'user_sent_at', 'content', 'source_messages',
                    'incoming_image_observations', 'incoming_media_observations') if key in row}
            pending.append({**item, '_receipt_order': order, '_received_only': True})
    delivered.sort(key=lambda r: r['_receipt_order'])
    if pending:
        # Find the first later platform timestamp without scanning/inserting
        # through the entire delivery list for each pending input. Prefix maxima
        # preserve existing delivery order even when the platform clock reverses.
        positions, maxima = [], []
        for index, row in enumerate(delivered):
            if same(row):
                key = (timestamp(row), row['_receipt_order'])
                positions.append(index)
                maxima.append(max(maxima[-1], key) if maxima else key)
        buckets = {}
        for row in sorted(pending, key=lambda r: (timestamp(r), r['_receipt_order'])):
            slot = bisect_right(maxima, (timestamp(row), row['_receipt_order']))
            position = positions[slot] if slot < len(positions) else len(delivered)
            buckets.setdefault(position, []).append(row)
        merged = []
        for index, row in enumerate(delivered):
            merged.extend(buckets.get(index, ()))
            merged.append(row)
        merged.extend(buckets.get(len(delivered), ()))
        delivered = merged
    for index, row in enumerate(delivered):
        row['_read_order'] = index
    return tuple(delivered)
