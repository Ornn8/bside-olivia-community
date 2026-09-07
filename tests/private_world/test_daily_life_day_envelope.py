import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime


NOW = datetime(2026, 9, 7, 4, tzinfo=timezone.utc)
SOURCE = "day:20260907:2"
CURRENT = {"location": "家里", "activity": "看书", "note": "午饭后翻几页书。"}
PROJECT = {"id": "reading", "title": "读书", "detail": "翻了几页", "status": "ongoing"}


def refresh(tmp_path, payload):
    calls = []

    class Model:
        async def complete(self, messages, **kwargs):
            calls.append(messages)
            return SimpleNamespace(text=json.dumps(payload))

    store = DailyLifeStore(tmp_path / "life.sqlite3")
    runtime = DailyLifeRuntime(store, Model, lambda: "")
    asyncio.run(runtime.refresh(NOW))
    return store, runtime, calls


@pytest.mark.parametrize("nested", [False, True])
def test_day_envelope_commits_same_content_without_extra_call(tmp_path, nested):
    payload = ({"current": {**CURRENT, "projects": [PROJECT]}} if nested
               else {"current": CURRENT, "projects": [PROJECT]})
    original = deepcopy(payload)
    store, runtime, calls = refresh(tmp_path, payload)
    assert runtime.error_code is None
    assert len(calls) == 1
    assert payload == original
    assert store.has_source(SOURCE)
    state = store.snapshot(NOW)
    assert {k: state["current"][k] for k in CURRENT} == CURRENT
    assert {k: state["projects"][0][k] for k in PROJECT} == PROJECT


@pytest.mark.parametrize("payload", [
    {"current": {**CURRENT, "projects": [PROJECT]}, "projects": []},
    {"current": {**CURRENT, "projects": [PROJECT], "unexpected": True}},
    {"current": {**CURRENT, "projects": [PROJECT]}, "unexpected": True},
    {"current": {"location": "家里", "activity": "看书", "projects": [PROJECT]}},
    {"current": {**CURRENT, "projects": {}}},
    {"current": {**CURRENT, "projects": [PROJECT, {**PROJECT, "id": "other", "status": "invalid"}]}},
    {"current": {**CURRENT, "note": None, "projects": [PROJECT]}},
    {"current": []},
])
def test_invalid_day_envelope_keeps_store_empty(tmp_path, payload):
    store, runtime, calls = refresh(tmp_path, payload)
    assert runtime.error_code == "DAILY_LIFE_GENERATION_UNAVAILABLE"
    assert len(calls) == 1
    assert not store.has_source(SOURCE)
    state = store.snapshot(NOW)
    assert state["current"] is None
    assert state["projects"] == []
