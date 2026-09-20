import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime


def test_quote_retry_names_the_invalid_field_without_accepting_ellipsis(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    user, reply = '今天练琴有进步。', '我正在练琴，稍后休息。'
    calls = []
    class Model:
        async def complete(self, messages, **kwargs):
            data = json.loads(messages[1]['content'])
            calls.append(data)
            if len(calls) == 2:
                assert data['validation_details']['invalid_quotes'] == ['reply_quote']
                assert not store.has_source('reply:synthetic')
            return SimpleNamespace(text=json.dumps({'updates': [], 'relationship': {
                'kind': 'meaningful_exchange', 'user_quote': user,
                'reply_quote': '我正在...休息。' if len(calls) == 1 else reply}}))
    runtime = DailyLifeRuntime(store, lambda: Model(), lambda: '')
    assert asyncio.run(runtime.consume_exchange('reply:synthetic', user, reply,
                      occurred_at=datetime(2026, 9, 20, 7, tzinfo=timezone.utc)))
    assert len(calls) == 2
    assert store.has_source('reply:synthetic')
