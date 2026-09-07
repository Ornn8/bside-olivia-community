import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime


@pytest.mark.parametrize("user,conduct", [
    ("我来给你介绍这些新东西。", "none"),
    ("你不想说的那件事，我就不追问了。", "respect"),
    ("按你说的，照片我没有发给别人。", "respect"),
])
def test_boundary_growth_needs_user_evidence_not_new_reply_condition(tmp_path, user, conduct):
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    reply = "好，不过先试用一天。我正在读书。"
    calls = []

    class Model:
        async def complete(self, messages, request_id, **kwargs):
            data = json.loads(messages[-1]["content"])
            calls.append(data)
            if ":conduct" in request_id:
                assert data == {"user_letter": user, "active_boundaries": []}
                assert not store.has_source("reply:positive:1")
                result = {"conduct": conduct, "quote": user if conduct == "respect" else ""}
            else:
                result = {"updates": [], "current_quote": "我正在读书。", "routine": None,
                          "relationship": {"kind": "boundary_respected", "user_quote": user,
                                           "reply_quote": "好，不过先试用一天。"}}
            return SimpleNamespace(text=json.dumps(result, ensure_ascii=False))

    runtime = DailyLifeRuntime(store, Model, lambda: "")
    now = datetime(2026, 9, 7, tzinfo=timezone.utc)
    assert asyncio.run(runtime.consume_exchange("reply:positive:1", user, reply, occurred_at=now))
    assert len(calls) == 2
    signal = store.exchange_relationship("reply:positive:1", user, reply)
    assert (signal is None) == (conduct == "none")
    if signal:
        assert signal["kind"] == "boundary_respected"
        assert signal["user_quote"] == user
    assert store.snapshot(now)["current"]["note"] == "我正在读书。"
    assert not asyncio.run(runtime.consume_exchange("reply:positive:1", user, reply, occurred_at=now))
    assert len(calls) == 2


@pytest.mark.parametrize("proof", [
    {"conduct": "respect", "quote": "我没有说过的话"},
    {"conduct": "respect", "quote": ""},
    {"conduct": "pressure", "quote": "你好"},
    {"conduct": "respect", "quote": "你好", "extra": True},
])
def test_invalid_positive_proof_does_not_partially_commit(tmp_path, proof):
    store = DailyLifeStore(tmp_path / "life.sqlite3")

    class Model:
        async def complete(self, messages, request_id, **kwargs):
            result = proof if ":conduct" in request_id else {
                "updates": [], "current_quote": "我正在读书。",
                "relationship": {"kind": "boundary_respected", "user_quote": "你好", "reply_quote": "好。"}}
            return SimpleNamespace(text=json.dumps(result, ensure_ascii=False))

    runtime = DailyLifeRuntime(store, Model, lambda: "")
    with pytest.raises(ValueError, match="DAILY_LIFE_BOUNDARY_EVIDENCE_INVALID"):
        asyncio.run(runtime.consume_exchange("reply:positive:1", "你好", "好。我正在读书。",
                                            occurred_at=datetime(2026, 9, 7, tzinfo=timezone.utc)))
    assert not store.has_source("reply:positive:1")
