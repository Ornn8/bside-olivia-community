import json
from datetime import datetime, timedelta, timezone

from runtime.private_world.daily_life import DailyLifeStore


NOW = datetime(2026, 1, 1, 22, tzinfo=timezone.utc)


def test_correspondence_makes_an_older_moment_historical_without_erasing_it(tmp_path):
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    store.publish_day("day:sleep", {
        "location": "卧室", "activity": "还在睡", "note": "闹钟定在七点。",
    }, [{"id": "piano", "title": "练琴", "detail": "今天练琴。", "status": "planned"}], occurred_at=NOW)
    before = json.loads(store.reply_context("练琴", now=NOW))
    assert before["stale"] is False
    store.record_exchange("reply:one", "早上好。", "早。", [], occurred_at=NOW + timedelta(minutes=2))
    state = store.snapshot(NOW + timedelta(minutes=3))
    after = json.loads(store.reply_context("练琴", now=NOW + timedelta(minutes=3)))
    assert after["stale"] is True
    assert after["current"] is None
    assert after["last_observation"] == before["current"]
    assert after["threads"] == before["threads"]
    # This is a reading projection; do not shorten the autonomous refresh TTL.
    assert state["stale"] is False
    assert store.snapshot(NOW + timedelta(minutes=3)) == state


def test_new_observation_is_current_and_delayed_or_future_exchanges_do_not_age_it(tmp_path):
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    quote = "我正在整理曲谱。"
    store.record_exchange("reply:current", "早。", quote, [], current_quote=quote,
                          occurred_at=NOW + timedelta(minutes=2))
    store.record_exchange("reply:delayed", "你好。", "你好。", [], occurred_at=NOW)
    store.record_exchange("reply:future", "你好。", "你好。", [], occurred_at=NOW + timedelta(minutes=10))
    current = json.loads(store.reply_context("曲谱", now=NOW + timedelta(minutes=3)))
    assert current["stale"] is False
    assert current["current"]["source_id"] == "reply:current"
    later = json.loads(store.reply_context("曲谱", now=NOW + timedelta(minutes=11)))
    assert later["stale"] is True
    store.publish_day("day:new", {"location": "琴房", "activity": "整理曲谱", "note": "曲谱放在桌上。"},
                      [], occurred_at=NOW + timedelta(minutes=12))
    assert json.loads(store.reply_context("曲谱", now=NOW + timedelta(minutes=13)))["stale"] is False
