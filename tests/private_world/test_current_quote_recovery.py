from datetime import datetime, timezone

import pytest

from runtime.private_world.daily_life import DailyLifeStore


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def test_current_quote_recovers_entire_sentence_including_qualification(tmp_path):
    life = DailyLifeStore(tmp_path / 'life.sqlite3')
    sentence = '我准备继续练那首曲子，不过今天手指不舒服的话就先停下。'
    life.record_exchange('reply:recovery:1', '今天忙什么？', '刚吃完饭。' + sentence,
                         [], occurred_at=NOW, current_quote='我准备继续练那首曲子。')
    current = life.snapshot(NOW)['current']
    assert current['note'] == sentence
    assert current['source_id'] == 'reply:recovery:1'
    assert current['activity'] is None


@pytest.mark.parametrize('reply, quote', [
    ('如果有时间，我准备继续练那首曲子，不过还没开始。', '我准备继续练那首曲子。'),
    ('我准备继续练那首曲子，之后再说。我准备继续练那首曲子，不过还没开始。', '我准备继续练那首曲子。'),
    ('我没准备继续练那首曲子，不过还没决定。', '我准备继续练那首曲子。'),
    ('我准备继续练那首曲子，不过' + '还没有决定' * 40 + '。', '我准备继续练那首曲子。'),
    ('我准备继续练那首曲子，不过还没开始。', '我准备继续练那首曲子！'),
])
def test_current_quote_recovery_does_not_drop_prefix_conditions_or_guess_text(tmp_path, reply, quote):
    life = DailyLifeStore(tmp_path / 'life.sqlite3')
    with pytest.raises(ValueError):
        life.record_exchange('reply:invalid:1', '你好。', reply, [], occurred_at=NOW, current_quote=quote)
    assert life.snapshot(NOW)['current'] is None
