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
