"""Short style reminders for her chat replies (user report 2026-10-22).

Users noticed the same stock phrases coming back again and again ("别硬撑", "这个我认", "回家再算"), and narration that
suddenly calls the user 他 while she is talking to them. The phrases are found by the program in her own recent
replies: a 3-6 character piece of Chinese that appears in at least MIN_REPLIES different replies. Only the longest,
most frequent few are named, and forms of address are left alone.
"""
from __future__ import annotations

import re
from collections import Counter

RECENT = 40
MIN_REPLIES = 4
MAX_PHRASES = 6
_HAN = re.compile(r'[一-鿿]+')
# Everyday words that repeat in any conversation and say nothing about style.
_COMMON = frozenset({'我知道', '你知道', '没关系', '怎么了', '是不是', '不是的', '我们一起', '一下吧', '今天的', '一点点',
                     '有没有', '好不好', '要不要', '为什么', '什么时候', '的时候', '一会儿', '这样的', '那个时候'})

PERSPECTIVE = '对对方说话时，动作和神态描写里称呼对方一律用"你"，不要突然换成"他"或"她"。'


def overused(replies, *, recent=RECENT, min_replies=MIN_REPLIES, limit=MAX_PHRASES):
    """Phrases that appear in at least min_replies of her last `recent` replies, longest and most frequent first."""
    counts = Counter()
    for reply in list(replies)[-recent:]:
        if not isinstance(reply, str):
            continue
        pieces = set()
        for run in _HAN.findall(reply):
            for size in range(3, 7):
                for start in range(0, len(run) - size + 1):
                    pieces.add(run[start:start + size])
        counts.update(pieces)
    frequent = [(phrase, n) for phrase, n in counts.items() if n >= min_replies and phrase not in _COMMON]
    # Prefer longer phrases; drop a piece contained in an already chosen phrase that is just as frequent.
    frequent.sort(key=lambda item: (-len(item[0]), -item[1], item[0]))
    chosen = []
    for phrase, n in frequent:
        if any(phrase in other and n <= m for other, m in chosen):
            continue
        chosen.append((phrase, n))
    chosen.sort(key=lambda item: (-item[1], -len(item[0]), item[0]))
    return [phrase for phrase, _ in chosen[:limit]]


def note(replies, *, address_terms=()):
    """The reminder for the writer: overused phrases (forms of address excluded) and the perspective rule."""
    terms = {term for term in address_terms if isinstance(term, str) and term}
    phrases = [phrase for phrase in overused(replies) if not any(term in phrase or phrase in term for term in terms)]
    lines = [PERSPECTIVE]
    if phrases:
        lines.append('你最近的回复里这些说法用得太多了：' + '、'.join(f'「{p}」' for p in phrases)
                     + '。这次不要再用，换成别的说法；称呼不在此列。')
    return '<style_reminders>\n' + '\n'.join(lines) + '\n</style_reminders>'
