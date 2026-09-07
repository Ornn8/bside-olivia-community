import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime


@pytest.mark.parametrize("conduct,quote", [("none", ""), ("none", "我睡不着，想听听建议。"), ("pressure", "你必须现在陪我，不许睡。")])
def test_conflict_requires_user_conduct_independent_of_generated_reproach(tmp_path, conduct, quote):
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    user = "我睡不着，想听听建议。" if conduct == "none" else quote
    reply = "别打扰我。我正在读书。"
    requests = []
    class Model:
        async def complete(self, messages, **kwargs):
            data = json.loads(messages[-1]["content"])
            requests.append(data)
            if len(requests) == 1:
                payload = {"updates": [], "current_quote": "我正在读书。", "relationship": {
                    "kind": "conflict", "user_quote": user, "reply_quote": "别打扰我。"}}
            else:
                assert not store.has_source("reply:case:1")
                assert data == {"user_letter": user, "active_boundaries": []}
                assert reply not in json.dumps(data, ensure_ascii=False)
                payload = {"conduct": conduct, "quote": quote}
            return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))
    runtime = DailyLifeRuntime(store, Model, lambda: "")
    now = datetime(2026, 9, 7, 18, tzinfo=timezone.utc)
    assert asyncio.run(runtime.consume_exchange("reply:case:1", user, reply, occurred_at=now))
    assert len(requests) == 2
    signal = store.exchange_relationship("reply:case:1", user, reply)
    assert (signal is None) == (conduct == "none")
    assert store.snapshot(now)["current"]["note"] == "我正在读书。"
    assert not asyncio.run(runtime.consume_exchange("reply:case:1", user, reply, occurred_at=now))
    assert len(requests) == 2


@pytest.mark.parametrize("proof", [
    {"conduct": "pressure", "quote": "not in user"},
    {"conduct": "none", "quote": "未提供的原文"},
    {"conduct": "boundary_violation", "quote": "你好"},
    {"conduct": "pressure", "quote": "你好", "extra": True},
])
def test_invalid_conduct_proof_never_commits_partial_world_state(tmp_path, proof):
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    class Model:
        async def complete(self, messages, request_id, **kwargs):
            result = proof if ":conduct" in request_id else {
                "updates": [], "current_quote": "我在读书。",
                "relationship": {"kind": "conflict", "user_quote": "你好", "reply_quote": "别烦我。"},
            }
            return SimpleNamespace(text=json.dumps(result))
    runtime = DailyLifeRuntime(store, Model, lambda: "")
    with pytest.raises(ValueError, match="DAILY_LIFE_CONFLICT_EVIDENCE_INVALID"):
        asyncio.run(runtime.consume_exchange("reply:case:1", "你好", "别烦我。我在读书。",
            occurred_at=datetime(2026, 9, 7, tzinfo=timezone.utc)))
    assert not store.has_source("reply:case:1")
