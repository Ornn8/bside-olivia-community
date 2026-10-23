"""What he sends on QQ that used to be dropped whole: cards, stickers, other kinds, and his pokes (2.5.0)."""
import json

import pytest

from runtime.personal_chat import decision
from runtime.personal_chat.events import owner_message


def qq(*segments, **changes):
    return {'post_type': 'message', 'message_type': 'private', 'self_id': 100, 'user_id': 200, 'message_id': 7,
            'time': 1792780000, 'message': list(segments), **changes}


def read(payload):
    return owner_message('qq', payload, account_id='100', owner_id='200')


def card(value):
    return {'type': 'json', 'data': {'data': json.dumps(value, ensure_ascii=False)}}


BILIBILI = {'app': 'com.tencent.miniapp_01', 'prompt': '[QQ小程序]哔哩哔哩',
            'meta': {'detail_1': {'appid': '1109937557', 'title': '哔哩哔哩', 'desc': '肖邦夜曲 Op.9 No.2 现场',
                                  'qqdocurl': 'https://b23.tv/AbCdEf1', 'preview': 'https://example.invalid/p.png'}}}
NEWS = {'app': 'com.tencent.structmsg', 'prompt': '[分享]猫咪的一天',
        'meta': {'news': {'title': '猫咪的一天', 'desc': '一只橘猫', 'jumpUrl': 'https://www.xiaohongshu.com/x/1',
                          'tag': '小红书'}}}


def test_a_bilibili_mini_program_card_keeps_source_title_and_link():
    event = read(qq(card(BILIBILI)))
    assert event.text == '[分享卡片：哔哩哔哩 · 肖邦夜曲 Op.9 No.2 现场] https://b23.tv/AbCdEf1'


def test_a_link_card_keeps_its_tag_title_and_link_with_his_words():
    event = read(qq({'type': 'text', 'data': {'text': '你看这个'}}, card(NEWS)))
    assert event.text == '你看这个[分享卡片：小红书 · 猫咪的一天] https://www.xiaohongshu.com/x/1'


@pytest.mark.parametrize('value, expected', [
    ({'prompt': '[分享]只有提示'}, '[分享卡片：[分享]只有提示]'),
    ({'meta': {'news': {'title': 'x', 'jumpUrl': 'http://plain.example/x'}}}, '[分享卡片：x]'),  # not https: no link
    ({'meta': {'news': {'title': 'x', 'jumpUrl': 'https://a b'}}}, '[分享卡片：x]'),
    ('not an object', '[分享卡片]'),
])
def test_a_card_never_passes_on_raw_json_or_an_unsafe_link(value, expected):
    assert read(qq(card(value))).text == expected
    assert read(qq({'type': 'json', 'data': {'data': '{broken'}})).text == '[分享卡片]'


def test_a_sticker_keeps_its_caption_and_a_face_its_name():
    event = read(qq({'type': 'mface', 'data': {'summary': '[开心]', 'emoji_id': 'x'}},
                    {'type': 'face', 'data': {'id': '264'}}))
    assert event.text == '[表情包:开心][表情:捂脸]'
    assert read(qq({'type': 'mface', 'data': {}})).text == '[表情包]'


@pytest.mark.parametrize('kind, marker', [('forward', '[聊天记录]'), ('dice', '[骰子]'), ('rps', '[猜拳]'),
                                          ('contact', '[名片]'), ('location', '[位置]'), ('music', '[音乐分享]'),
                                          ('something-new', '[QQ消息]')])
def test_other_kinds_arrive_as_a_marker_instead_of_losing_the_message(kind, marker):
    assert read(qq({'type': kind, 'data': {}})).text == marker


def test_the_owner_boundary_still_holds_for_new_kinds():
    assert read(qq(card(BILIBILI), user_id=300)) is None
    assert read(qq(card(BILIBILI), message_type='group', group_id=5)) is None
    assert read(qq({'type': 'json', 'data': 'not a dict'})) is None


def poke(**changes):
    return {'post_type': 'notice', 'notice_type': 'notify', 'sub_type': 'poke', 'self_id': 100, 'user_id': 200,
            'sender_id': 200, 'target_id': 100, 'time': 1792780000, **changes}


def test_his_poke_is_a_turn_of_its_own():
    event = read(poke())
    assert event.text == '[他戳了戳你]' and event.message_id == 'poke-1792780000' and event.sent_at


@pytest.mark.parametrize('changes', [{'sender_id': 100, 'target_id': 200}, {'target_id': 200}, {'user_id': 300},
                                     {'group_id': 9}, {'self_id': 101}, {'sub_type': 'lucky_king'}, {'time': None}])
def test_her_own_poke_others_and_other_notices_are_not_turns(changes):
    assert read(poke(**changes)) is None


def decode(**fields):
    value = {'text': '好呀', 'delivery': 'text', 'text_reason': None, 'listening': 'keep', 'initiative': 'keep',
             'pause_until': None, 'letter': 'keep', 'letter_until': None, 'followup_at': None, 'evidence': '',
             'sticker': None, 'skip': False, **fields}
    from datetime import datetime, timezone
    return decision.decode(json.dumps(value, ensure_ascii=False), user='hi', now=datetime.now(timezone.utc))


def test_her_poke_and_reaction_are_kept_only_when_well_formed():
    assert decode(poke=True, reaction='月亮')['poke'] is True
    assert decode(reaction='[月亮]')['reaction'] == '月亮'
    for value in ({'poke': 'yes'}, {'poke': False}, {'reaction': '不存在的表情'}, {'reaction': 5}):
        data = decode(**value)
        assert 'poke' not in data and 'reaction' not in data, value


def test_his_face_copied_back_becomes_the_face_itself():
    assert decode(text='哈哈[表情:捂脸]')['text'] == '哈哈[捂脸]'
    assert decode(text='嗯[表情:不存在]')['text'] == '嗯'
    assert decode(text='[他戳了戳你]干嘛呀')['text'] == '干嘛呀'


def test_the_instruction_lists_only_faces_the_catalog_can_send():
    from runtime.personal_chat.qq_faces import FACE_IDS, HER_FACES
    assert all(name.casefold() in FACE_IDS for name in HER_FACES)
    assert '戳了戳你' in decision.INSTRUCTION and 'reaction' in decision.INSTRUCTION
