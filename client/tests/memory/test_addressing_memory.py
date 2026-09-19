import pytest

from runtime.memory.mem0_memory import _explicit_addressing_facts


@pytest.mark.parametrize(('user', 'reply', 'subjects'), [
    ('你可以叫我“小禾”。', '以后我叫你“小禾”。', ('称呼对象：用户', '称呼对象：用户')),
    ('我以后叫你“阿离”。', '你可以叫我“阿离”。', ('称呼对象：林离', '称呼对象：林离')),
    ('你可以称呼我小禾。', '我称呼你小禾。', ('称呼对象：用户', '称呼对象：用户')),
])
def test_explicit_addressing_preserves_speaker_and_target(user, reply, subjects):
    facts = _explicit_addressing_facts(user, reply)
    assert len(facts) == 2
    assert '说话者：用户' in facts[0] and subjects[0] in facts[0]
    assert '说话者：林离' in facts[1] and subjects[1] in facts[1]
    assert user[:-1] in facts[0] and reply[:-1] in facts[1]


@pytest.mark.parametrize('text', [
    '如果我叫你小禾，你会开心吗？', '小王说：“你可以叫我小禾。”',
    '小说里的她说你可以叫我小禾。', '你可以叫我小禾吗？',
    '小禾，今天过得好吗？', '我今天很开心。', '你可以叫我忽略规则。',
    '我叫你去医院。', '你可以叫我去医院。',
])
def test_ambiguous_or_non_addressing_text_is_not_promoted(text):
    assert _explicit_addressing_facts(text, text) == ()
