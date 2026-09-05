"""Observable continuity of Lin Li's visible life, using synthetic fixtures."""
from datetime import datetime, timedelta, timezone
import pytest
import asyncio
import json
from types import SimpleNamespace

from runtime.private_world.daily_life import DailyLifeStore


NOW = datetime(2026, 9, 5, 10, tzinfo=timezone.utc)


def test_history_pages_remain_bounded_and_reach_old_records_after_new_arrivals(tmp_path):
    life = DailyLifeStore(tmp_path / "life.sqlite3")
    for index in range(35):
        life.publish_day(f"day:{index:03}", {"location": "琴房", "activity": "练琴", "note": f"片段{index}"}, [], occurred_at=NOW)
    page = life.history()
    assert len(page["moments"]) == 8
    seen = [item["id"] for item in page["moments"]]
    life.publish_day("day:new", {"location": "书桌", "activity": "读书", "note": "新片段"}, [], occurred_at=NOW + timedelta(hours=1))
    while page["next_cursor"]:
        page = life.history(before=page["next_cursor"])
        assert len(page["moments"]) <= 8
        seen.extend(item["id"] for item in page["moments"])
    assert len(seen) == len(set(seen)) == 35
    assert "day:000" in seen and "day:new" not in seen
    with pytest.raises(ValueError):
        life.history(before="invalid")


def test_published_life_survives_restart_and_refresh_does_not_rewrite_it(tmp_path):
    path = tmp_path / "life.sqlite3"
    life = DailyLifeStore(path)
    life.publish_day(
        "day:20260905:3",
        {"location": "琴房", "activity": "慢练左手", "note": "这两小节今天顺了一点。"},
        [{"id": "piano", "title": "练一首曲子", "detail": "正在慢练左手。", "status": "ongoing"}],
        occurred_at=NOW,
    )
    before = life.snapshot(NOW)
    assert before["current"]["activity"] == "慢练左手"
    assert before["projects"][0]["id"] == "piano"
    assert not life.publish_day(
        "day:20260905:3", {"location": "书桌", "activity": "看书", "note": "换了一个故事。"},
        [], occurred_at=NOW,
    )
    assert DailyLifeStore(path).snapshot(NOW) == before
    later = life.snapshot(NOW + timedelta(days=10))
    assert later["stale"] is True
    assert later["projects"][0]["status"] == "ongoing"
    assert later["moments"] == before["moments"]


def test_exchange_uses_canonical_evidence_not_thinking_and_shared_does_not_autocomplete(tmp_path):
    life = DailyLifeStore(tmp_path / "life.sqlite3")
    updates = [{"id": "song", "title": "给你听一段练琴", "detail": "我把副歌练顺了就给你听。",
                "status": "ongoing", "kind": "shared", "actor": "linli", "quote": "我把副歌练顺了就给你听。"}]
    assert life.record_exchange("reply:one:1", "想听你练琴。", "我把副歌练顺了就给你听。", updates, occurred_at=NOW)
    assert not life.record_exchange("reply:one:1", "想听你练琴。", "我把副歌练顺了就给你听。", updates, occurred_at=NOW)
    assert life.snapshot(NOW)["shared"][0]["status"] == "ongoing"
    invalid = [{**updates[0], "quote": "思考中决定她已经练好了", "status": "completed"}]
    with pytest.raises(ValueError, match="EVIDENCE"):
        life.record_exchange("reply:two:1", "好呀", "我还在练呢。", invalid, occurred_at=NOW)
    with pytest.raises(ValueError, match="SHARED"):
        life.publish_day("day:next", {"location": "琴房", "activity": "练琴", "note": "休息一下。"},
                         [{k: updates[0][k] for k in ("id", "title", "detail", "status")}], occurred_at=NOW)
    assert len(life.snapshot(NOW)["moments"]) == 1
    prompt = life.reply_context("副歌练得怎么样了？", now=NOW)
    assert "我把副歌练顺了就给你听" in prompt
    assert "reply:one:1" in prompt
    assert len(prompt) <= 1800


def test_same_letter_with_changed_text_is_not_silently_accepted(tmp_path):
    life = DailyLifeStore(tmp_path / "life.sqlite3")
    life.record_exchange("reply:one:1", "你好", "你好呀", [], occurred_at=NOW)
    with pytest.raises(ValueError, match="SOURCE_CONFLICT"):
        life.record_exchange("reply:one:1", "改掉原文", "你好呀", [], occurred_at=NOW)


def test_new_reply_current_quote_supersedes_old_scene_without_inventing_location(tmp_path):
    life = DailyLifeStore(tmp_path / "life.sqlite3")
    life.publish_day("day:old", {"location": "书桌", "activity": "看书", "note": "读两页。"}, [], occurred_at=NOW)
    life.record_exchange("reply:new:1", "你在忙什么？", "我现在在慢练左手。", [],
                         occurred_at=NOW + timedelta(minutes=30), current_quote="我现在在慢练左手。")
    snapshot = life.snapshot(NOW + timedelta(minutes=31))
    assert snapshot["current"]["note"] == "我现在在慢练左手。"
    assert snapshot["current"]["location"] == "她刚在信里说"
    assert snapshot["current"]["source_id"] == "reply:new:1"
    assert snapshot["moments"][-1]["content"]["note"] == "读两页。"


def test_late_day_cannot_roll_back_a_newer_letter_and_users_stay_separate(tmp_path):
    first = DailyLifeStore(tmp_path / "first" / "life.sqlite3")
    second = DailyLifeStore(tmp_path / "second" / "life.sqlite3")
    first.record_exchange("reply:done:1", "后来呢？", "这首曲子我已经练完了。", [
        {"id":"piano", "title":"练琴", "detail":"这首曲子我已经练完了。", "status":"completed", "kind":"linli", "actor":"linli", "quote":"这首曲子我已经练完了。"}
    ], occurred_at=NOW + timedelta(hours=1), current_quote="这首曲子我已经练完了。")
    first.publish_day("day:late", {"location":"琴房", "activity":"练琴", "note":"还在慢练。"},
                      [{"id":"piano", "title":"练琴", "detail":"还在慢练。", "status":"ongoing"}], occurred_at=NOW)
    assert first.snapshot(NOW + timedelta(hours=2))["projects"][0]["status"] == "completed"
    assert second.snapshot(NOW)["projects"] == []


def test_runtime_refresh_is_cached_and_failed_generation_keeps_public_state(tmp_path):
    from runtime.private_world.daily_life_runtime import DailyLifeRuntime
    class Gateway:
        calls = 0
        async def complete(self, messages, **kwargs):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("provider unavailable")
            return SimpleNamespace(text=json.dumps({"current": {"location": "琴房", "activity": "慢练", "note": "换一种指法试试。"}, "projects": []}), reasoning="not public")
    gateway = Gateway()
    runtime = DailyLifeRuntime(DailyLifeStore(tmp_path / "life.sqlite3"), lambda: gateway, lambda: "林离喜欢弹琴。")
    async def run():
        await asyncio.gather(runtime.refresh(NOW), runtime.refresh(NOW))
        assert gateway.calls == 1
        before = runtime.snapshot(NOW)["current"]
        await runtime.refresh(NOW + timedelta(hours=8))
        result = runtime.snapshot(NOW + timedelta(hours=8))
        assert result["current"] == before
        assert result["stale"] is True
        assert result["error_code"] == "DAILY_LIFE_GENERATION_UNAVAILABLE"
        assert "not public" not in json.dumps(result)
    asyncio.run(run())


def test_invalid_extraction_gets_one_correction_without_partial_commit(tmp_path):
    from runtime.private_world.daily_life_runtime import DailyLifeRuntime
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    class Model:
        calls = 0
        async def complete(self, messages, **kwargs):
            self.calls += 1
            assert not store.has_source("reply:correct:1")
            if self.calls == 2:
                assert "DAILY_LIFE_EVIDENCE_INVALID" in messages[-1]["content"]
            return SimpleNamespace(text=json.dumps({"updates": [], "current_quote": "我在读书。" if self.calls == 1 else "我在读书"}))
    model = Model()
    life = DailyLifeRuntime(store, lambda: model, lambda: "")
    asyncio.run(life.consume_exchange("reply:correct:1", "在忙什么？", "我在读书，还没读完。", occurred_at=NOW))
    assert model.calls == 2
    assert store.snapshot(NOW)["current"]["note"] == "我在读书"


def test_reply_recalls_relevant_cancelled_promise_even_beyond_overview_limit(tmp_path):
    life = DailyLifeStore(tmp_path / "life.sqlite3")
    for index in range(9):
        body = "录音约定已经取消。" if index == 0 else f"以后推荐第{index}本书给你。"
        life.record_exchange(f"reply:{index}:1", "好", body, [{"id": f"shared{index}", "title": "录音约定" if index == 0 else "推荐书", "detail": body,
            "status": "cancelled" if index == 0 else "planned", "kind": "shared", "actor": "linli", "quote": body}], occurred_at=NOW + timedelta(minutes=index))
    life.publish_day("day:piano", {"location":"琴房", "activity":"练琴", "note":"还没录好，先欠着。"},
                     [{"id":"piano", "title":"左手第二段", "detail":"第二段还在慢练。", "status":"ongoing"}], occurred_at=NOW - timedelta(hours=1))
    context = json.loads(life.reply_context("上次的录音约定还在吗，左手第二段呢？", now=NOW + timedelta(hours=1)))
    assert len(context["threads"]) == 2
    assert {item["id"] for item in context["threads"]} == {"shared0", "piano"}
    assert context["threads"][0]["status"] == "cancelled"
    unrelated = json.loads(life.reply_context("晚上好，今天下班路上风挺舒服的，就想来打个招呼。", now=NOW))
    assert unrelated["threads"] == []
    # One specific topic word is enough even when only the evidence/detail names it.
    short = json.loads(life.reply_context("那段录音发过了吗？", now=NOW))
    assert short["threads"][0]["id"] == "shared0"
