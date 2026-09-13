"""Occasional same-call selection from existing relationship-eligible stickers."""
import random
import re
from runtime.letter_stickers.selection import allowed_stickers, _labels


def choices(rows, view):
    delivered = [r for r in rows if r.get('channel') == 'wechat' and r.get('delivery_status') == 'DELIVERED']
    since = 0
    for row in reversed(delivered):
        if row.get('sticker_delivery_status') in {'SENDING', 'DELIVERED', 'UNKNOWN'}:
            break
        since += 1
    if since < 4:
        return {}
    allowed = list(allowed_stickers(view))
    counts = {key: sum(r.get('sticker_id') == key for r in delivered) for key in allowed}
    last = {row.get('sticker_id'): index for index, row in enumerate(delivered)
            if row.get('sticker_delivery_status') in {'SENDING', 'DELIVERED', 'UNKNOWN'}}
    # Bounded recency keeps old/unseen choices competitive without enormous weights.
    weights = {key: (1 + min(256, len(delivered) - last.get(key, -1))) / (1 + counts[key])
               for key in allowed}
    sampled = []
    for _ in range(min(10, len(allowed))):
        key = random.choices(allowed, weights=[weights[k] for k in allowed])[0]
        sampled.append(key)
        allowed.remove(key)
    return {key: _labels()[key] for key in sampled}


def extract(text, allowed):
    matches = re.findall(r'\[\[sticker:(linli-\d{2,3})\]\]', text)
    chosen = next((key for key in matches if key in allowed), None)
    return re.sub(r'\[\[sticker:[^\]\r\n]*\]\]', '', text).strip(), chosen
