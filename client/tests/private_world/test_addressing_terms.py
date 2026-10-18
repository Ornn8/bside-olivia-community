import pytest

from runtime.private_world.addressing import address_term


@pytest.mark.parametrize('quote, term', [
    ('早安呀饭饭', '饭饭'),
    ('外边下雨了小猫咪', '小猫咪'),
    ('晚安宝贝', '宝贝'),
    ('饭饭呀', '饭饭'),
    ('小猫咪，', '小猫咪'),
    ('亲爱的', '亲爱的'),
    ('饭饭', '饭饭'),
])
def test_only_the_term_of_address_is_kept(quote, term):
    assert address_term(quote) == term


@pytest.mark.parametrize('quote', ['晚安', '早安呀', '下雨啦', '你在干嘛', '明天见', '好的', '外边下雨了'])
def test_messages_without_a_term_of_address_are_dropped(quote):
    assert address_term(quote) is None
