"""Dice and rock-paper-scissors: what his throw reads as, her throw back, and who won (2.5.0, owner 2026-10-24)."""
import asyncio

import pytest

from runtime.personal_chat import qq_games
from runtime.personal_chat.events import PersonalMessage
from tests.http.test_qq_gestures import napcat
from tests.http.test_qq_upgrade_inbound import decode, qq, read


@pytest.mark.parametrize('segment, text', [
    ({'type': 'dice', 'data': {'result': '4'}}, '[骰子:4点]'),
    ({'type': 'rps', 'data': {'result': '1'}}, '[猜拳:布]'),
    ({'type': 'rps', 'data': {'result': '2'}}, '[猜拳:剪刀]'),
    ({'type': 'rps', 'data': {'result': '3'}}, '[猜拳:石头]'),
    ({'type': 'dice', 'data': {'result': '7'}}, '[骰子]'),
    ({'type': 'rps', 'data': {'result': 'x'}}, '[猜拳]'),
])
def test_his_throw_reads_as_its_result(segment, text):
    assert read(qq(segment)).text == text


def test_her_throw_is_read_back_from_the_message_qq_sent():
    outcomes, seen = napcat([lambda s: s.game('dice'), lambda s: s.game('chess')], {
        'send_private_msg': {'status': 'ok', 'retcode': 0, 'data': {'message_id': 77}},
        'get_msg': {'status': 'ok', 'retcode': 0, 'data': {'message': [{'type': 'dice', 'data': {'result': '5'}}]}}})
    assert outcomes == ['5', 'ValueError:QQ_REPLY_INVALID']
    assert seen == [{'action': 'send_private_msg', 'params': {'user_id': 200, 'message': [{'type': 'dice', 'data': {}}]}},
                    {'action': 'get_msg', 'params': {'message_id': 77}}]


def context(text, results, *, enabled=True, row=None):
    thrown = []
    async def throw(kind):
        thrown.append(kind)
        value = results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    async def scenario():
        token = qq_games.THROWER.set(throw)
        try:
            return await qq_games.game_context(PersonalMessage('qq', '100', '200', '7', text), row, enabled=enabled)
        finally:
            qq_games.THROWER.reset(token)
    row = {} if row is None else row
    return asyncio.run(scenario()), row, thrown


def test_she_throws_the_same_game_back_and_knows_who_won():
    note, row, thrown = context('来[骰子:4点]', ['6'])
    assert thrown == ['dice'] and row['qq_game'] == {'kind': 'dice', 'his': '4', 'hers': '6', 'outcome': 'win',
                                                     'status': 'SENT'}
    assert '他掷了骰子，4点' in note and 'QQ给你掷出6点' in note and '你赢了' in note
    note, row, _ = context('[猜拳:剪刀]', ['3'])
    assert row['qq_game']['hers'] == '石头' and '石头赢剪刀，你赢了' in note
    note, row, _ = context('[猜拳:石头]', ['1'])
    assert row['qq_game']['outcome'] == 'win'
    note, row, _ = context('[猜拳:布]', ['3'])
    assert row['qq_game']['outcome'] == 'lose' and '布赢石头，他赢了' in note
    note, row, _ = context('[骰子:3点]', ['3'])
    assert row['qq_game']['outcome'] == 'tie' and '平局' in note


def test_a_turn_written_again_keeps_its_throw():
    _, row, _ = context('[骰子:2点]', ['5'])
    note, row, thrown = context('[骰子:2点]\n再来', [RuntimeError('never asked')], row=row)
    assert thrown == [] and 'QQ给你掷出5点' in note


def test_no_throw_without_his_game_the_setting_or_a_result():
    assert context('晚安', ['6'])[2] == []
    note, row, thrown = context('[骰子:4点]', ['6'], enabled=False)
    assert (note, row, thrown) == ('', {}, [])
    note, row, thrown = context('[骰子:4点]', [RuntimeError('QQ_ACTION_UNCONFIRMED')])
    assert note == '' and row['qq_game'] == {'kind': 'dice', 'status': 'FAILED'}
    note, row, thrown = context('[猜拳:布]', ['9'])
    assert note == '' and row['qq_game']['status'] == 'FAILED'
    assert context('[骰子]', ['6'])[2] == []  # QQ sent no result: nothing to answer against


def test_a_throw_is_never_text_or_a_blind_face():
    assert decode(text='[骰子:6点]我赢啦')['text'] == '我赢啦'
    assert decode(text='再来一局[表情:骰子][包剪锤]')['text'] == '再来一局'
    assert decode(text='[QQ小游戏：你赢了]哈哈')['text'] == '哈哈'
