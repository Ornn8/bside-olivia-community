import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime


def test_refresh_receives_recent_character_observations_after_restart(tmp_path):
    now = datetime(2026, 9, 5, 10, tzinfo=timezone.utc)
    path = tmp_path / 'life.sqlite3'
    store = DailyLifeStore(path)
    for i, text in enumerate(('我在喝茶。', '我在吃葱油拌面。', '我在吃小馄饨。', '今晚已经换成白开水了。')):
        store.record_exchange(f'reply:food{i}:1', 'PRIVATE_USER_TEXT', text, [],
                              occurred_at=now - timedelta(hours=4-i), current_quote=text)
    store.record_exchange('reply:future:1', 'PRIVATE_USER_TEXT', '我在吃明天的晚饭。', [],
                          occurred_at=now + timedelta(hours=1), current_quote='我在吃明天的晚饭。')
    calls = []

    class Gateway:
        async def complete(self, messages, **kwargs):
            calls.append(messages)
            return SimpleNamespace(text=json.dumps({'current': {'location': '家里', 'activity': '休息',
                                                       'note': '暂时歇一会儿。'}, 'projects': []}))

    runtime = DailyLifeRuntime(DailyLifeStore(path), Gateway, lambda: '口味偏清淡。')
    asyncio.run(runtime.refresh(now))
    data = json.loads(calls[0][1]['content'])
    observations = data['recent_observations']
    assert len(observations) == 4
    assert observations[0]['note'] == '今晚已经换成白开水了。'
    assert all(x['actor'] == 'linli' and x['evidence_kind'] == 'character_statement' for x in observations)
    assert 'PRIVATE_USER_TEXT' not in json.dumps(data)
    assert 'reply:future:1' not in json.dumps(data)
    assert runtime.snapshot(now)['current']['note'] == '暂时歇一会儿。'
