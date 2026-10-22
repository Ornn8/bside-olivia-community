"""Her own suggestion to play the piano or to call, at a moment that fits (owner 2026-10-22).

The client only says which suggestions are open: the feature works here, and in the last COOLDOWN_SECONDS she has
neither suggested it nor has it been used. The cloud judgment then decides whether this moment fits (media_offer).
When it does, the writer adds one natural question and the suggestion is recorded, so it is not repeated for days.
A 'none' judgment asks again only after RETRY_SECONDS, so the extra question stays rare. The user's yes is an
ordinary request: the piano is played and billed exactly as when the user asks; a call is started by the user.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from runtime.reply.proactive_letters import read_json, write_json

KINDS = ('piano', 'call')
COOLDOWN_SECONDS = 3 * 86400
RETRY_SECONDS = 3600
BEIJING = timezone(timedelta(hours=8))
# Hours (Beijing) in which a suggestion may be made: no piano before 8:00, no call after 23:30.
HOURS = {'piano': (8 * 60, 24 * 60), 'call': (9 * 60, 23 * 60 + 30)}

INSTRUCTION = {
    'piano': """<media_offer>
本轮冻结JEV决定是piano：此刻适合由你自然地提议弹一段钢琴给对方听。先正常回应最后一条用户消息，然后在结尾用一句直接的问句提议，比如"要不要我弹一段给你听？"，可以顺着此刻的心情说想弹什么感觉。这是询问：本轮不弹、不发送音频、不说已经弹好，不提时长和价格。对方答应后再弹。沿用当前人格与关系。继续输出原聊天JSON。
</media_offer>""",
    'call': """<media_offer>
本轮冻结JEV决定是call：此刻适合由你自然地提议和对方打个电话。先正常回应最后一条用户消息，然后在结尾用一句直接的问句提议，比如"要不要打个电话？想听听你的声音"，并顺带说一句对方在软件里点「打电话」就能找到你。你不能自己拨号，不说已经打过去，不提价格，不创建followup。沿用当前人格与关系。继续输出原聊天JSON。
</media_offer>""",
}


def _path(root):
    return Path(root) / 'proactive' / 'media-offers.json'


def _state(root):
    value = read_json(_path(root)) if root is not None else {}
    return {name: {k: v for k, v in (value.get(name) or {}).items() if k in KINDS and type(v) in (int, float)}
            for name in ('offered', 'asked')}


def open_kinds(root, now, *, available, last_used):
    """Suggestions that may be judged now. available: {kind: bool}; last_used: {kind: unix seconds or None}."""
    if root is None:
        return []
    state = _state(root)
    local = datetime.fromtimestamp(now, BEIJING)
    minute = local.hour * 60 + local.minute
    result = []
    for kind in KINDS:
        start, end = HOURS[kind]
        if (available.get(kind) is not True or not start <= minute < end
                or now - state['offered'].get(kind, 0) < COOLDOWN_SECONDS
                or now - (last_used.get(kind) or 0) < COOLDOWN_SECONDS
                or now - state['asked'].get(kind, 0) < RETRY_SECONDS):
            continue
        result.append(kind)
    return result


def _write(root, name, kinds, now):
    state = _state(root)
    for kind in kinds:
        if kind in KINDS:
            state[name][kind] = now
    write_json(_path(root), state)


def note_asked(root, kinds, now=None):
    if root is not None and kinds:
        _write(root, 'asked', kinds, time.time() if now is None else now)


def note_offered(root, kind, now=None):
    if root is not None and kind in KINDS:
        _write(root, 'offered', [kind], time.time() if now is None else now)


def experience(kinds):
    """The binding sent with the judgment, or None when nothing is open."""
    kinds = [kind for kind in KINDS if kind in (kinds or ())]
    return {'kinds': kinds} if kinds else None


def validate(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'kinds'} or not isinstance(value['kinds'], list)
            or not 1 <= len(value['kinds']) <= len(KINDS) or len(set(value['kinds'])) != len(value['kinds'])
            or any(kind not in KINDS for kind in value['kinds'])):
        raise ValueError('invalid offer experience')
    return {'kinds': list(value['kinds'])}


def validate_choice(value, experience):
    if value not in ('none', *experience['kinds']):
        raise ValueError('invalid media offer')
    return value
