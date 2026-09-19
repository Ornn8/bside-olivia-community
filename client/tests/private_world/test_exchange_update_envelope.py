import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime


def item(kind, quote="我明天练新曲。"):
    return {"id": kind, "title": "练新曲", "detail": "练新曲", "status": "planned",
            "kind": kind, "actor": "linli", "quote": quote}


def run(store, payload):
    requests = []
    class Model:
        async def complete(self, messages, **kwargs):
            requests.append(messages)
            return SimpleNamespace(text=json.dumps(payload))
    runtime = DailyLifeRuntime(store, Model, lambda: "")
    asyncio.run(runtime.consume_exchange("reply:envelope:1", "想听新曲。", "我明天练新曲。练好后发给你。",
        occurred_at=datetime(2026, 9, 7, tzinfo=timezone.utc)))
    return requests


def test_grouped_extraction_preserves_both_kinds_without_another_model_call(tmp_path):
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    payload = {"projects": [item("linli")], "shared": [item("shared", "练好后发给你。")],
               "current_quote": None, "relationship": None, "routine": None}
    original = deepcopy(payload)
    assert len(run(store, payload)) == 1
    assert payload == original
    state = store.exchange_state()
    assert state["projects"][0]["quote"] == "我明天练新曲。"
    assert state["shared"][0]["quote"] == "练好后发给你。"


@pytest.mark.parametrize("payload", [
    {"projects": [], "shared": [], "updates": [item("linli")]},
    {"projects": [item("shared")], "shared": []},
    {"projects": [], "shared": [item("linli")]},
    {"projects": {}, "shared": []},
    {"projects": [], "shared": [], "unexpected": True},
    {"projects": [item("linli", "这不是原文。")], "shared": []},
])
def test_ambiguous_grouping_and_invalid_evidence_still_fail_without_writes(tmp_path, payload):
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    with pytest.raises(ValueError):
        run(store, payload)
    assert not store.has_source("reply:envelope:1")
