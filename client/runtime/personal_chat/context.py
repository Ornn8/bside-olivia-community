"""Frozen per-reply read window; unsent assistant drafts are never delivered turns."""
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime

from runtime.reply.conversation_context import conversation_context as chat_context


READ_WINDOW = ContextVar('personal_chat_read_window', default=None)


def freeze_read_window(rows, *, channel, binding_id, current_id):
    rows = deepcopy(list(rows))
    for index, row in enumerate(rows):
        sequence = row.get('received_sequence')
        row['_receipt_order'] = (float(row.get('created_at', 0)),
                                 sequence if type(sequence) is int else index)
    current = next((r for r in rows if r.get('letter_id') == current_id), None)
    cutoff = ((float(current.get('read_boundary_created_at', current['_receipt_order'][0])),
               current.get('read_boundary_sequence', current['_receipt_order'][1]))
              if current else (float('inf'), float('inf')))
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
    delivered = [r for r in rows if isinstance(r.get('letter_id'), str)
                 and r['letter_id'] != current_id and not r.get('read_only')
                 and r['_receipt_order'] <= cutoff
                 and (r.get('delivery_status') == 'DELIVERED' if r.get('channel') in {'qq', 'wechat'}
                      else r.get('letter_status') == 'COMPLETED')]
    delivered.sort(key=lambda r: r['_receipt_order'])
    pending = [r for r in rows if same(r) and isinstance(r.get('letter_id'), str) and r['letter_id'] != current_id
               and not r.get('read_only') and r.get('origin') != 'proactive'
               and not r.get('superseded_by')
               and r.get('delivery_status') in {'RECEIVED', 'GENERATING', 'GENERATED', 'MEDIA_PENDING', 'FAILED', 'SENDING', 'SKIPPED', 'DELIVERY_UNCONFIRMED'}
               and r['_receipt_order'] <= cutoff]
    for row in sorted(pending, key=lambda r: (timestamp(r), r['_receipt_order'])):
        # Preserve incoming evidence only. No unsent reply/media fields cross the boundary.
        item = {key: row[key] for key in ('letter_id', 'channel', 'binding_id', 'created_at',
                'life_received_at', 'user_sent_at', 'content', 'source_messages',
                'incoming_image_observations', 'incoming_media_observations', '_receipt_order') if key in row}
        item['_received_only'] = True
        position = next((i for i, old in enumerate(delivered) if same(old)
                         and (timestamp(old), old['_receipt_order'])
                         > (timestamp(row), row['_receipt_order'])), len(delivered))
        delivered.insert(position, item)
    for index, row in enumerate(delivered):
        row['_read_order'] = index
    return tuple(delivered)
