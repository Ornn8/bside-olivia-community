"""Occasional same-call selection from existing relationship-eligible stickers."""
import re
from runtime.letter_stickers.selection import allowed_stickers, weighted_candidates, _labels


def choices(rows, view, *, channel='wechat'):
    delivered = [r for r in rows if r.get('channel') == channel and r.get('delivery_status') == 'DELIVERED']
    since = 0
    for row in reversed(delivered):
        if row.get('sticker_delivery_status') in {'SENDING', 'DELIVERED', 'UNKNOWN'}:
            break
        since += 1
    sent_count = sum(row.get('sticker_delivery_status') in {'SENDING', 'DELIVERED', 'UNKNOWN'}
                     for row in delivered)
    if since < 1 + sent_count % 2:
        return {}
    history = [row.get('sticker_id') for row in delivered]
    appeared = [row.get('sticker_id') if row.get('sticker_delivery_status') in
                {'SENDING', 'DELIVERED', 'UNKNOWN'} else None for row in delivered]
    sampled = weighted_candidates(allowed_stickers(view), history, appeared=appeared, limit=10)
    return {key: _labels()[key] for key in sampled}


def extract(text, allowed):
    matches = re.findall(r'\[\[sticker:(linli-\d{2,3})\]\]', text)
    chosen = next((key for key in matches if key in allowed), None)
    return re.sub(r'\[\[sticker:[^\]\r\n]*\]\]', '', text).strip(), chosen
