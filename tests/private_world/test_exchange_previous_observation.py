"""Life extraction can compare a reply with already published observations."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime


NOW = datetime(2026, 9, 9, 9, tzinfo=timezone.utc)


@pytest.mark.parametrize("minutes", [-5, 5])
def test_extractor_receives_only_observations_available_at_receipt(tmp_path, minutes):
    store = DailyLifeStore(tmp_path / "world.sqlite")
    store.publish_day("day:fixture", {
        "location": "家里", "activity": "洗好了画笔", "note": "画笔都洗好了。",
    }, [], occurred_at=NOW + timedelta(minutes=minutes))
    observed = store.snapshot(NOW)["current"]
    captured = []

    class Gateway:
        async def complete(self, messages, **kwargs):
            captured.append(json.loads(messages[1]["content"]))
            return SimpleNamespace(text=json.dumps({
                "updates": [], "current_quote": None, "relationship": None, "routine": None,
            }))

    runtime = DailyLifeRuntime(store, lambda: Gateway(), lambda: "")
    assert asyncio.run(runtime.consume_exchange(
        "reply:fixture", "忙完了吗？", "快洗完了。",
        occurred_at=NOW + timedelta(minutes=10), received_at=NOW,
    ))
    assert captured[0]["previous_observation"] == (observed if minutes < 0 else None)
    # A rejected new observation leaves the original journal and state intact.
    assert store.snapshot(NOW + timedelta(minutes=10))["current"] == observed
    assert store.exchange_state() == {"projects": [], "shared": []}
