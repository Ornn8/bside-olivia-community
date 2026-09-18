import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime


@pytest.mark.parametrize(
    "conduct,target,quote",
    [
        ("none", "unclear", ""),
        ("none", "self", "我睡不着，想听听建议。"),
        ("pressure", "linli", "你必须现在陪我，不许睡。"),
    ],
)
def test_conflict_requires_user_conduct_independent_of_generated_reproach(tmp_path, conduct, target, quote):
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
                payload = {"conduct": conduct, "target": target, "quote": quote}
            return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))
    runtime = DailyLifeRuntime(store, Model, lambda: "")
    now = datetime(2026, 9, 7, 18, tzinfo=timezone.utc)
    assert asyncio.run(runtime.consume_exchange("reply:case:1", user, reply, occurred_at=now))
    assert len(requests) == 2
    signal = store.exchange_relationship("reply:case:1", user, reply)
    assert (signal is None) == (conduct == "none" or target != "linli")
    assert store.snapshot(now)["current"]["note"] == "我正在读书。"
    assert not asyncio.run(runtime.consume_exchange("reply:case:1", user, reply, occurred_at=now))
    assert len(requests) == 2


@pytest.mark.parametrize("proof", [
    {"conduct": "pressure", "target": "linli", "quote": "not in user"},
    {"conduct": "none", "target": "unclear", "quote": "未提供的原文"},
    {"conduct": "boundary_violation", "target": "linli", "quote": "你好"},
    {"conduct": "pressure", "target": "linli", "quote": "你好", "extra": True},
    {"conduct": "pressure", "quote": "你好"},
    {"conduct": "pressure", "target": "everyone", "quote": "你好"},
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


@pytest.mark.parametrize(
    "user,conduct,target",
    [
        ("我爸以前总逼我按他的想法做。", "pressure", "other"),
        ("老板今天当着所有人的面骂我，真的很烦。", "denigration", "other"),
        ("前任以前总说我必须按她安排来。", "pressure", "other"),
        ("我那时候一直逼自己必须做到最好。", "pressure", "self"),
        ("朋友转述他爸说‘你必须现在回家’。", "pressure", "other"),
        ("有个人一直骂另一个人，我听着很不舒服。", "denigration", "unclear"),
    ],
)
def test_third_party_or_self_story_never_becomes_interpersonal_conflict(tmp_path, user, conduct, target):
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    reply = "听起来那段经历确实挺难受的。"

    class Model:
        async def complete(self, messages, request_id=None, **kwargs):
            if request_id and ":conduct" in request_id:
                return SimpleNamespace(text=json.dumps(
                    {"conduct": conduct, "target": target, "quote": user},
                    ensure_ascii=False,
                ))
            return SimpleNamespace(text=json.dumps({
                "updates": [],
                "current_quote": None,
                # Deliberately simulate a first-pass false positive. The
                # independent target verifier must neutralize it.
                "relationship": {
                    "kind": "conflict",
                    "user_quote": user,
                    "reply_quote": reply,
                },
            }, ensure_ascii=False))

    runtime = DailyLifeRuntime(store, Model, lambda: "")
    now = datetime(2026, 9, 18, 3, tzinfo=timezone.utc)
    assert asyncio.run(runtime.consume_exchange(
        "reply:story:1", user, reply, occurred_at=now
    ))
    assert store.exchange_relationship("reply:story:1", user, reply) is None


def test_high_frequency_story_telling_does_not_accumulate_conflicts(tmp_path):
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    stories = [
        f"第{i}件事：老板以前总逼我必须按他的安排来，我只是跟你讲讲这段经历。"
        for i in range(20)
    ]

    class Model:
        async def complete(self, messages, request_id=None, **kwargs):
            data = json.loads(messages[-1]["content"])
            user = data["user_letter"]
            if request_id and ":conduct" in request_id:
                return SimpleNamespace(text=json.dumps({
                    "conduct": "pressure",
                    "target": "other",
                    "quote": user,
                }, ensure_ascii=False))
            return SimpleNamespace(text=json.dumps({
                "updates": [],
                "current_quote": None,
                "relationship": {
                    "kind": "conflict",
                    "user_quote": user,
                    "reply_quote": "嗯，我在听，你继续说。",
                },
            }, ensure_ascii=False))

    runtime = DailyLifeRuntime(store, Model, lambda: "")
    now = datetime(2026, 9, 18, 3, tzinfo=timezone.utc)
    for index, user in enumerate(stories):
        reply = "嗯，我在听，你继续说。"
        assert asyncio.run(runtime.consume_exchange(
            f"reply:story:{index}", user, reply, occurred_at=now
        ))
        assert store.exchange_relationship(
            f"reply:story:{index}", user, reply
        ) is None
