"""She knows what she owns: clothes the user picked or bought, today's outfit, cameras and cats."""
import json
from datetime import datetime, timezone

from runtime.diary.diary import DiaryStore

STATE = {'wardrobe_styles': [
             {'style_id': 'original', 'label': '原版日常', 'looks': []},
             {'style_id': 'home_knit', 'label': '居家针织', 'looks': [
                 {'look_id': 'home_knit-1', 'label': '炭灰针织'}, {'look_id': 'home_knit-2', 'label': '奶白针织'}]},
             {'style_id': 'stage', 'label': '舞台演出', 'looks': [{'look_id': 'stage-1', 'label': '舞台演出'}]}],
         'purchases': {'owned': ['home_knit-2', 'stage-1'], 'free_remaining': 1},
         'daily_outfit': {'date': '2026-10-08', 'style_id': 'home_knit', 'look_id': 'home_knit-2'}}


def test_owned_clothes_and_todays_outfit_reach_her_reply_context(tmp_path):
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    now = datetime(2026, 10, 8, 4, tzinfo=timezone.utc)
    store.remember_wardrobe(STATE, now)
    value = json.loads(store.context(now))
    assert value['clothes_from_user'] == ['居家针织·奶白针织', '舞台演出']
    assert value['wearing_today'] == '居家针织·奶白针织'
    assert '衣柜' in value['clothes_meaning']


def test_yesterdays_outfit_is_not_worn_today(tmp_path):
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    store.remember_wardrobe(STATE, datetime(2026, 10, 8, 4, tzinfo=timezone.utc))
    value = json.loads(store.context(datetime(2026, 10, 9, 4, tzinfo=timezone.utc)))
    assert value['clothes_from_user'] and 'wearing_today' not in value


def test_nothing_owned_adds_nothing(tmp_path):
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    now = datetime(2026, 10, 8, 4, tzinfo=timezone.utc)
    store.remember_wardrobe({'wardrobe_styles': [], 'purchases': {'owned': []}}, now)
    assert store.context(now) is None
    store.remember_gifts([{'id': 'contax-t2', 'name': 'Contax T2'}], now)
    value = json.loads(store.context(now))
    assert value['gifts_from_user'][0]['name'] == 'Contax T2' and 'clothes_from_user' not in value
