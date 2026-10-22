"""Stock phrases she has overused lately are named for the writer; the user is always 你 in her narration."""
from runtime.reply.style_reminders import overused, note, PERSPECTIVE


def test_a_phrase_in_many_replies_is_named_once_at_its_full_length():
    replies = ['今天别硬撑了，早点睡。', '工作的事别硬撑，慢慢来。', '你呀，别硬撑。', '累了就说，别硬撑着。',
               '这个我认，是我的错。', '好吧这个我认。', '这个我认，下次注意。', '嗯，这个我认的。',
               '外面下雨了。', '我刚练完琴。']
    found = overused(replies)
    assert '别硬撑' in found and '这个我认' in found
    assert '硬撑' not in found and '个我认' not in found      # parts of a chosen phrase are not repeated
    assert '外面下雨' not in found


def test_rare_phrases_and_everyday_words_are_left_alone():
    replies = ['为什么今天好冷。', '猫又跑了，为什么呀。', '为什么我做的蛋糕塌了。', '为什么下雪了。']
    assert overused(replies) == []                            # an everyday word is not a verbal habit
    assert overused(['回家再算'] * 3) == []                    # three replies are not yet a habit


def test_only_the_recent_replies_count():
    old = ['回家再算！'] * 10
    recent = [f'第{i}句话。' for i in range(40)]
    assert overused(old + recent) == []


def test_note_always_keeps_the_perspective_rule_and_skips_forms_of_address():
    replies = ['小笨蛋，别硬撑。'] * 5
    text = note(replies, address_terms=['小笨蛋'])
    assert PERSPECTIVE in text and '「别硬撑」' in text and '小笨蛋' not in text
    assert note([]).count('\n') == 2                          # only the perspective rule
