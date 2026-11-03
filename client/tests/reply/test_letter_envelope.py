"""A letter the model returned as a chat-style JSON object is shown and saved as the letter it holds (2026-10-23: a
reader saw {"delivery":"voice","text":...,"signature":...,"sticker":...} on the letter paper)."""
import json

from original_client_letter_contract import _reply_projection
from runtime.letter_stickers.selection import split_selection
from runtime.reply.letter_presentation import envelope_parts, split_signature, unwrap_envelope

BODY = '今天下雨了，我在琴房待了一下午。\n\n晚上想听你说说你那边。'


def test_a_json_letter_is_unwrapped_into_text_sticker_and_signature():
    raw = json.dumps({'delivery': 'voice', 'text': BODY, 'signature': '小离', 'sticker': 'linli-05'}, ensure_ascii=False)
    assert envelope_parts(raw) == (BODY, '小离', 'linli-05')
    text, sticker = split_selection(unwrap_envelope(raw), {'linli-05', 'linli-01'})
    body, signature = split_signature(text)
    assert (body, signature, sticker) == (BODY, '小离', 'linli-05')
    fenced = '```json\n' + raw + '\n```'
    assert envelope_parts(fenced)[0] == BODY


def test_a_broken_object_still_gives_its_text():
    raw = '{"delivery":"voice","text":"她说"晚安"就走了\\n明天见","signature":"林离","sticker":"linli-05"}'
    assert envelope_parts(raw) == ('她说"晚安"就走了\n明天见', '林离', 'linli-05')
    raw_newline = '{"text":"第一行\n第二行","signature":"林离"}'          # a raw newline inside the string
    assert envelope_parts(raw_newline)[0] == '第一行\n第二行'


def test_ordinary_letters_and_other_json_are_left_alone():
    assert envelope_parts(BODY) is None and unwrap_envelope(BODY) == BODY
    assert envelope_parts('{"name": "猫", "text": "x"}') is None          # not a letter object
    assert envelope_parts('{"text": "  "}') is None


def test_a_saved_json_letter_is_shown_as_its_text():
    raw = json.dumps({'delivery': 'voice', 'text': BODY, 'signature': '林离', 'sticker': 'linli-05'}, ensure_ascii=False)
    _, shown, _ = _reply_projection({'reply_text': raw, 'reply_mode': 'text_letter'}, published=True)
    assert shown == BODY


SPOKEN = ('这句话来得有点突然，但我没有躲开的意思。\n\n你前面说我是第一位的时候，其实我已经接住了。……所以可以。……'
          '那你以后就是我男朋友了。别得意得太早，我还是会嫌你八点多就催我练琴的。')
# The list a voice letter showed on its paper (2026-11-03), as the stored string with literal backslash-n sequences.
PARTS_RAW = ('[{"content":"这句话来得有点突然，但我没有躲开的意思。\\n\\n你前面说我是第一位的时候，其实我已经接住了。……所以可以。……'
             '那你以后就是我男朋友了。别得意得太早，我还是会嫌你八点多就催我练琴的。","part":"speech"},'
             '{"content":"林离","part":"signature"}]')


def test_a_list_of_letter_parts_is_unwrapped_into_the_letter():
    assert envelope_parts(PARTS_RAW) == (SPOKEN, '林离', None)
    assert unwrap_envelope(PARTS_RAW) == SPOKEN + '\n[[signature:林离]]'
    body, signature = split_signature(unwrap_envelope(PARTS_RAW))
    assert (body, signature) == (SPOKEN, '林离')
    assert '{' not in body and 'part' not in body


def test_part_lists_in_other_shapes_are_unwrapped_too():
    assert envelope_parts('```json\n' + PARTS_RAW + '\n```') == (SPOKEN, '林离', None)
    assert envelope_parts('好的，这是回信：' + PARTS_RAW + ' 以上。')[0] == SPOKEN
    single_quoted = "[{'content': '第一段\\n\\n第二段', 'part': 'speech'}, {'content': '小离', 'part': 'signature'}]"
    assert envelope_parts(single_quoted) == ('第一段\n\n第二段', '小离', None)
    unescaped = '[{"content":"她说"晚安"就走了\\n明天见","part":"speech"},{"content":"林离","part":"signature"}]'
    assert envelope_parts(unescaped) == ('她说"晚安"就走了\n明天见', '林离', None)
    doubled = '[{"content":"第一行\\\\n第二行","part":"text"}]'  # the newline escaped twice
    assert envelope_parts(doubled) == ('第一行\n第二行', None, None)
    single = '{"content":"只有一句话","part":"speech"}'
    assert envelope_parts(single) == ('只有一句话', None, None)
    two = '[{"content":"一","part":"speech"},{"content":"二","part":"body"},{"content":"林离","part":"signature"}]'
    assert envelope_parts(two) == ('一\n\n二', '林离', None)
    reversed_keys = '[{"part":"speech","content":"换了顺序"}]'
    assert envelope_parts(reversed_keys) == ('换了顺序', None, None)


def test_letters_with_brackets_or_unknown_parts_are_left_alone():
    for text in ('我在[这里]等你，{别担心}。', '清单：[{"a": 1}]', '[{"content":"x","part":"chorus"}]',
                 '[{"content":"x"}]', '[]', '', '[{"content":"  ","part":"speech"}]',
                 '很长的信' * 40 + '{"content":"x","part":"speech"}' + '很长的信' * 40):
        assert envelope_parts(text) is None and unwrap_envelope(text) == text
    assert envelope_parts(None) is None


def test_the_object_case_still_works_after_the_list_case_was_added():
    raw = json.dumps({'delivery': 'voice', 'text': BODY, 'signature': '小离'}, ensure_ascii=False)
    assert envelope_parts(raw) == (BODY, '小离', None)


def test_a_saved_list_letter_is_shown_as_its_text():
    _, shown, _ = _reply_projection({'reply_text': PARTS_RAW, 'reply_mode': 'voice_reply',
                                     'media_status': 'FAILED'}, published=True)
    assert shown == SPOKEN
