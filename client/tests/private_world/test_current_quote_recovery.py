from datetime import datetime, timedelta, timezone

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


def test_current_meal_claim_outside_its_local_window_is_not_published(tmp_path):
    life = DailyLifeStore(tmp_path / "life.sqlite3")
    # 05:32 UTC = 13:32 in Shanghai/Beijing time.
    now = datetime(2026, 9, 18, 5, 32, tzinfo=timezone.utc)
    quote = "我这边的晚饭已经彻底收尾"
    assert life.record_exchange(
        "reply:meal:1", "吃晚饭了吗？", quote, [],
        occurred_at=now, current_quote=quote,
    )
    snapshot = life.snapshot(now)
    assert snapshot["current"] is None
    moment = next(item for item in snapshot["moments"] if item["id"] == "reply:meal:1")
    assert moment["content"]["current"] is None


def test_current_meal_claim_inside_its_local_window_can_be_published(tmp_path):
    life = DailyLifeStore(tmp_path / "life.sqlite3")
    # 10:32 UTC = 18:32 in Shanghai/Beijing time.
    now = datetime(2026, 9, 18, 10, 32, tzinfo=timezone.utc)
    quote = "我这边的晚饭已经彻底收尾"
    assert life.record_exchange(
        "reply:meal:2", "吃晚饭了吗？", quote, [],
        occurred_at=now, current_quote=quote,
    )
    assert life.snapshot(now)["current"]["note"] == quote


def test_existing_bad_meal_observation_from_older_version_is_hidden(tmp_path):
    life = DailyLifeStore(tmp_path / "life.sqlite3")
    now = datetime(2026, 9, 18, 5, 32, tzinfo=timezone.utc)
    bad = {
        "location": None,
        "activity": None,
        "note": "我这边的晚饭已经彻底收尾",
        "source_id": "reply:legacy-meal:1",
        "occurred_at": now.isoformat(),
    }
    from runtime.private_world.daily_life import _json
    with life._db() as db:
        db.execute("INSERT OR REPLACE INTO life_current VALUES (1,?)", (_json(bad),))
    assert life.snapshot(now + timedelta(minutes=1))["current"] is None
