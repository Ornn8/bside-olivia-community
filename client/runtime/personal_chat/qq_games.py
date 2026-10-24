"""Dice and rock-paper-scissors in the QQ chat (owner 2026-10-24): she answers his throw with one of her own, and she
knows who won before she writes.

QQ picks every result itself, a requested one included (acceptance 2026-10-24: asked for a 6, QQ threw a 2), so her
result is read back from the message QQ sent.
"""
from contextvars import ContextVar
import re

# QQ's result numbers for a rock-paper-scissors throw.
RPS = {'1': '布', '2': '剪刀', '3': '石头'}
_BEATS = {'石头': '剪刀', '剪刀': '布', '布': '石头'}
# The live chat's thrower for this turn, set by the service: kind -> QQ's result.
THROWER = ContextVar('qq_game_thrower', default=None)
_MARK = re.compile(r'\[骰子:([1-6])点\]|\[猜拳:(石头|剪刀|布)\]')


def marker(kind, data):
    """His throw as the input shows it: the dice points or the hand, or the bare kind when QQ sent no result."""
    result = str(data.get('result', ''))
    if kind == 'dice':
        return f'[骰子:{result}点]' if len(result) == 1 and result in '123456' else '[骰子]'
    return f'[猜拳:{RPS[result]}]' if result in RPS else '[猜拳]'


def last_throw(text):
    """His last throw in this message, as (kind, value)."""
    found = list(_MARK.finditer(text or ''))
    if not found:
        return None
    last = found[-1]
    return ('dice', last.group(1)) if last.group(1) else ('rps', last.group(2))


def outcome(kind, his, hers):
    if his == hers:
        return 'tie'
    if kind == 'dice':
        return 'win' if int(hers) > int(his) else 'lose'
    return 'win' if _BEATS[hers] == his else 'lose'


def note(game):
    his, hers, result = game['his'], game['hers'], game['outcome']
    if game['kind'] == 'dice':
        verdict = {'win': '你的点数大，你赢了', 'lose': '他的点数大，他赢了', 'tie': '点数一样，平局'}[result]
        thrown = f'他掷了骰子，{his}点。你看到后马上也掷了一个，QQ给你掷出{hers}点——{verdict}'
    else:
        verdict = {'win': f'{hers}赢{his}，你赢了', 'lose': f'{his}赢{hers}，他赢了', 'tie': '出的一样，平局'}[result]
        thrown = f'他出了{his}。你看到后马上也出了一个，QQ给你的是{hers}——{verdict}'
    return f'\n[QQ小游戏：{thrown}。这已经发生在聊天里，回复时自然接上这个结果，不另编点数或手势。]'


async def game_context(event, row, *, enabled):
    """Throw back once per turn; a turn written again keeps its throw and its result."""
    game = row.get('qq_game')
    if not isinstance(game, dict):
        his = last_throw(event.text)
        throw = THROWER.get()
        if his is None or throw is None or not enabled:
            return ''
        kind, value = his
        try:
            result = await throw(kind)
            hers = result if kind == 'dice' else RPS[result]
        except Exception:
            row['qq_game'] = {'kind': kind, 'status': 'FAILED'}  # she answers without a throw
            return ''
        game = row['qq_game'] = {'kind': kind, 'his': value, 'hers': hers, 'outcome': outcome(kind, value, hers),
                                 'status': 'SENT'}
    return note(game) if game.get('status') == 'SENT' else ''
