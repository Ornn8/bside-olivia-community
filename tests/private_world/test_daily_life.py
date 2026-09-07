"""Observable continuity of Lin Li's visible life, using synthetic fixtures."""
from datetime import datetime, timedelta, timezone
import pytest


@pytest.mark.parametrize("actor", ["user", "linli"])
def test_joined_exact_sentences_restore_complete_source_including_conditions(tmp_path, actor):
    life = DailyLifeStore(tmp_path / "life.sqlite3")
    source = "我会练一下。暂时不保证能弹好。\n\n练好后再发给你。"
    life.record_exchange("reply:restore:1", source if actor == "user" else "谢谢。",
                         source if actor == "linli" else "谢谢。", [{
        "id": "practice", "title": "练曲子", "detail": "尝试练曲子",
        "status": "planned", "kind": "shared", "actor": actor,
        "quote": "我会练一下。练好后再发给你。",
    }], occurred_at=NOW)
    assert life.exchange_state()["shared"][0]["quote"] == source


@pytest.mark.parametrize("source, quote", [
    ("我会练一下。练好后再发给你。", "我已练好了。练好后再发给你。"),
    ("我会练一下。还没定。我会练一下。还没定。练好后再发给你。", "我会练一下。练好后再发给你。"),
    ("练好后再发给你。我会练一下。", "我会练一下。练好后再发给你。"),
    ("我会练一下。" + "还没确定。" * 50 + "练好后再发给你。", "我会练一下。练好后再发给你。"),
])
def test_source_quote_recovery_rejects_fabrication_ambiguity_reordering_and_oversize(tmp_path, source, quote):
    life = DailyLifeStore(tmp_path / "life.sqlite3")
    with pytest.raises(ValueError):
        life.record_exchange("reply:invalid:1", "谢谢。", source, [{
            "id": "practice", "title": "练曲子", "detail": "尝试练曲子",
            "status": "planned", "kind": "shared", "actor": "linli", "quote": quote,
        }], occurred_at=NOW)
    assert life.exchange_state()["shared"] == []
import asyncio
import json
from types import SimpleNamespace

from runtime.private_world.daily_life import DailyLifeStore


NOW = datetime(2026, 9, 5, 10, tzinfo=timezone.utc)


@pytest.mark.parametrize("actor", ["user", "linli"])
def test_next_exchange_uses_verified_quote_without_rewriting_stored_summary(tmp_path, actor):
    life = DailyLifeStore(tmp_path / "life.sqlite3")
    quote = "你寄书的邮费我来报销。"
    summary = "对方必须先早睡，证据说话人才会让对方报销自己的邮费。"
    life.record_exchange("reply:postage:1", quote if actor == "user" else "谢谢。",
                         quote if actor == "linli" else "谢谢。", [{
        "id": "postage", "title": "寄书邮费", "detail": summary,
        "status": "planned", "kind": "shared", "actor": actor, "quote": quote,
    }], occurred_at=NOW)
    stored = life.snapshot(NOW)["shared"][0]
    following = life.exchange_state()["shared"][0]
    assert following == {**stored, "detail": quote}
    assert following["actor"] == actor
    assert summary not in json.dumps(life.exchange_state(), ensure_ascii=False)
    assert life.snapshot(NOW)["shared"][0] == stored
    disclosed = json.loads(life.reply_context("寄书邮费", now=NOW))["threads"][0]
    assert following == disclosed


def test_exchange_summary_cannot_add_a_motive_to_later_reply_context(tmp_path):
    life = DailyLifeStore(tmp_path / "life.sqlite3")
    quote = "你选一本书吧，有空我翻翻。"
    summary = "用户必须证明阅读水平，林离随后检查结果。"
    life.record_exchange("reply:book:1", "聊聊书？", quote, [{
        "id": "book", "title": "选一本书", "detail": summary,
        "status": "awaiting_user", "kind": "shared", "actor": "linli", "quote": quote,
    }], occurred_at=NOW)
    context = json.loads(life.reply_context("那本书选好了。", now=NOW))
    item = context["threads"][0]
    assert item["detail"] == quote
    assert item["status"] == "linli_waiting"
    assert item["actor"] == "linli" and item["source_id"] == "reply:book:1"
    assert summary not in json.dumps(context, ensure_ascii=False)
    assert json.loads(life.reply_context("阅读水平怎么样？", now=NOW))["threads"] == []
    # Projection must not rewrite the stored extraction or the visible archive.
    assert life.snapshot(NOW)["shared"][0]["detail"] == summary


@pytest.mark.parametrize("invitation", ["野餐地点你查过没有？", "野餐地点你查一下，再发给我。"])
def test_her_waiting_is_not_projected_as_the_users_commitment(tmp_path, invitation):
    life = DailyLifeStore(tmp_path / "life.sqlite3")
    life.record_exchange("reply:picnic:1", "给你一些野餐地点作参考。", invitation, [{
        "id": "picnic", "title": "野餐地点", "detail": "等用户查野餐地点",
        "status": "awaiting_user", "kind": "shared", "actor": "linli", "quote": invitation,
    }], occurred_at=NOW)
    context = json.loads(life.reply_context("野餐地点只是参考，不用一定去。", now=NOW))
    item = context["threads"][0]
    assert item["status"] == "linli_waiting"
    assert item["actor"] == "linli" and item["quote"] == invitation
    assert item["commitment_evidence"] == "requires_user_statement"
    assert life.snapshot(NOW)["shared"][0]["status"] == "awaiting_user"

    # An explicit user commitment remains a pending action, including when the
    # extractor expresses it as awaiting_user rather than planned.
    for index, status in enumerate(("planned", "awaiting_user", "completed", "cancelled")):
        quote = "我明天把野餐地图发给你。" if index < 2 else "野餐地图不发了。" if status == "cancelled" else "野餐地图发出了。"
        life.record_exchange(f"reply:picnic:{index + 2}", quote, "好。", [{
            "id": "picnic", "title": "野餐地点", "detail": quote,
            "status": status, "kind": "shared", "actor": "user", "quote": quote,
        }], occurred_at=NOW + timedelta(minutes=index + 1))
        item = json.loads(life.reply_context("野餐地点怎么安排？", now=NOW))["threads"][0]
        assert item["status"] == status
        assert item["actor"] == "user"
        assert "commitment_evidence" not in item


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
    assert snapshot["current"]["location"] is None
    assert snapshot["current"]["activity"] is None
    assert snapshot["current"]["source_id"] == "reply:new:1"
    assert snapshot["moments"][-1]["content"]["note"] == "读两页。"
    projected = json.loads(life.reply_context("你在哪？", now=NOW + timedelta(minutes=31)))
    assert projected["current"]["location"] is None
    assert projected["current"]["activity"] is None
    assert projected["current"]["note"] == "我现在在慢练左手。"


def test_legacy_quote_placeholders_are_unknown_in_views_without_rewriting_history(tmp_path):
    life = DailyLifeStore(tmp_path / "life.sqlite3")
    life.record_exchange("reply:legacy:1", "你好", "我刚把书放下。", [],
                         occurred_at=NOW, current_quote="我刚把书放下。")
    with life._db() as db:
        current = json.loads(db.execute("SELECT payload FROM life_current WHERE id=1").fetchone()[0])
        current.update(location="她刚在信里说", activity="新的近况")
        moment = json.loads(db.execute("SELECT payload FROM life_moments WHERE source_id='reply:legacy:1'").fetchone()[0])
        moment["current"] = current
        raw = json.dumps(moment, ensure_ascii=False)
        db.execute("UPDATE life_current SET payload=? WHERE id=1", (json.dumps(current, ensure_ascii=False),))
        db.execute("UPDATE life_moments SET payload=? WHERE source_id='reply:legacy:1'", (raw,))
    views = [life.snapshot(NOW), life.history()]
    assert views[0]["current"]["location"] is None
    for view in views:
        quoted = view["moments"][0]["content"]["current"]
        assert quoted["location"] is None and quoted["activity"] is None
        assert quoted["note"] == "我刚把书放下。"
        assert quoted["source_id"] == "reply:legacy:1"
    assert "她刚在信里说" not in life.reply_context("你在哪？", now=NOW)
    with life._db() as db:
        assert db.execute("SELECT payload FROM life_moments WHERE source_id='reply:legacy:1'").fetchone()[0] == raw


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
            return SimpleNamespace(text=json.dumps({"updates": [], "current_quote": "我在跑步。" if self.calls == 1 else "我在读书"}))
    model = Model()
    life = DailyLifeRuntime(store, lambda: model, lambda: "")
    asyncio.run(life.consume_exchange("reply:correct:1", "在忙什么？", "我在读书，还没读完。", occurred_at=NOW))
    assert model.calls == 2
    assert store.snapshot(NOW)["current"]["note"] == "我在读书"


@pytest.mark.parametrize("repair_quote", [True, False])
def test_extraction_reports_bad_fields_but_still_validates_corrected_evidence(tmp_path, repair_quote):
    from runtime.private_world.daily_life_runtime import DailyLifeRuntime
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    class Model:
        calls = 0
        async def complete(self, messages, **kwargs):
            self.calls += 1
            item = {"id": "book", "title": "读书", "detail": "读完一本书",
                    "status": "completed", "kind": "linli", "actor": "linli",
                    "quote": "我读完这本书了。"}
            if self.calls == 1:
                item.pop("actor")
                item["source_id"] = "day:untrusted"
                item["updated_at"] = "2026-01-01"
            else:
                assert not store.has_source("reply:fields:1")
                payload = json.loads(messages[-1]["content"])
                assert payload["validation_details"] == {
                    "update_fields": ["actor", "detail", "id", "kind", "quote", "status", "title"],
                    "errors": [{"path": "updates[0]", "extra_fields": ["source_id", "updated_at"],
                                "missing_fields": ["actor"]}],
                }
                if not repair_quote:
                    item["quote"] = "这句不在回信里。"
            return SimpleNamespace(text=json.dumps({"updates": [item], "current_quote": None,
                                                   "relationship": None, "routine": None}))
    model = Model()
    life = DailyLifeRuntime(store, lambda: model, lambda: "")
    call = life.consume_exchange("reply:fields:1", "好。", "我读完这本书了。", occurred_at=NOW)
    if repair_quote:
        assert asyncio.run(call)
        assert store.snapshot(NOW)["projects"][0]["source_id"] == "reply:fields:1"
    else:
        with pytest.raises(ValueError, match="DAILY_LIFE_EVIDENCE_INVALID"):
            asyncio.run(call)
        assert not store.has_source("reply:fields:1")
        assert store.snapshot(NOW)["projects"] == []
    assert model.calls == 2


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


def test_disclosed_old_correspondence_loads_latest_state_without_unrelated_threads(tmp_path):
    life = DailyLifeStore(tmp_path / "life.sqlite3")
    for identifier, text in (("parcel", "包裹已经寄出，尚未确认收到。"), ("trip", "出游约定取消了。")):
        life.record_exchange(f"reply:{identifier}:1", text, "知道了。", [{
            "id": identifier, "title": identifier, "detail": text, "status": "ongoing" if identifier == "parcel" else "cancelled",
            "kind": "shared", "actor": "user", "quote": text,
        }], occurred_at=NOW)
    result = json.loads(life.reply_context("晚安。", related_text="我还计划寄包裹。", now=NOW))
    assert [item["id"] for item in result["threads"]] == ["parcel"]
    assert result["threads"][0]["detail"] == "包裹已经寄出，尚未确认收到。"
    assert len(life.reply_context("晚安。", related_text="我还计划寄包裹。", now=NOW)) <= 1800


def test_unanswered_character_invitation_is_not_resurfaced_by_history_alone(tmp_path):
    life = DailyLifeStore(tmp_path / "life.sqlite3")
    invitation = "你选一本书吧，有空我翻翻。"
    life.record_exchange("reply:book:1", "聊聊书？", invitation, [{
        "id": "book", "title": "选一本书", "detail": invitation,
        "status": "awaiting_user", "kind": "shared", "actor": "linli", "quote": invitation,
    }], occurred_at=NOW)
    stored = life.snapshot(NOW)["shared"]
    context = json.loads(life.reply_context("晚安。", related_text=invitation, now=NOW))
    assert context["threads"] == []
    # A current response can still recall it; storage/extraction keep the invitation.
    current = json.loads(life.reply_context("那本书选好了。", related_text=invitation, now=NOW))
    assert current["threads"][0]["id"] == "book"
    assert current["threads"][0]["status"] == "linli_waiting"
    assert life.exchange_state()["shared"][0]["quote"] == invitation
    assert life.snapshot(NOW)["shared"] == stored


@pytest.mark.parametrize("actor,status", [
    ("user", "planned"), ("user", "awaiting_user"), ("user", "completed"),
    ("user", "cancelled"), ("linli", "planned"), ("linli", "completed"),
    ("linli", "cancelled"),
])
def test_history_still_recalls_commitments_and_terminal_state(tmp_path, actor, status):
    life = DailyLifeStore(tmp_path / "life.sqlite3")
    quote = "这本书的事就按刚才说的办。"
    life.record_exchange("reply:book:1", quote if actor == "user" else "嗯。",
                         quote if actor == "linli" else "嗯。", [{
        "id": "book", "title": "这本书", "detail": quote,
        "status": status, "kind": "shared", "actor": actor, "quote": quote,
    }], occurred_at=NOW)
    context = json.loads(life.reply_context("晚安。", related_text="这本书", now=NOW))
    assert context["threads"][0]["status"] == status
    assert context["threads"][0]["actor"] == actor


def test_exchange_extraction_cannot_copy_previous_evidence_as_current_quote(tmp_path):
    from runtime.private_world.daily_life_runtime import DailyLifeRuntime
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    store.record_exchange("reply:old:1", "周末打算寄包裹。", "我等着。", [{
        "id":"parcel", "title":"寄包裹", "detail":"用户计划寄包裹", "status":"planned",
        "kind":"shared", "actor":"user", "quote":"周末打算寄包裹。",
    }], occurred_at=NOW)
    class Model:
        calls = 0

        async def complete(self, messages, **kwargs):
            data = json.loads(messages[-1]["content"])
            assert data["user_letter"] == "包裹寄出了。"
            old = data["previous_state"]["shared"][0]
            assert old["quote"] == "周末打算寄包裹。"
            assert old["actor"] == "user" and old["source_id"] == "reply:old:1"
            self.calls += 1
            if self.calls == 2:
                assert data["validation_error"] == "DAILY_LIFE_EVIDENCE_INVALID"
                assert data["rejected_candidate"]["updates"][0]["quote"] == "周末打算寄包裹。"
                assert not store.has_source("reply:new:1")
                assert store.snapshot(NOW)["shared"][0]["status"] == "planned"
            return SimpleNamespace(text=json.dumps({"updates":[{
                "id":"parcel", "title":"寄包裹", "detail":"用户已寄出", "status":"completed",
                "kind":"shared", "actor":"user", "quote":old["quote"] if self.calls == 1 else "包裹寄出了。",
            }]}))
    model = Model()
    life = DailyLifeRuntime(store, lambda: model, lambda: "")
    asyncio.run(life.consume_exchange("reply:new:1", "包裹寄出了。", "知道了。", occurred_at=NOW + timedelta(hours=1)))
    assert store.snapshot(NOW + timedelta(hours=1))["shared"][0]["status"] == "completed"


def test_exchange_cancels_original_item_outside_the_six_item_ui_window(tmp_path):
    from runtime.private_world.daily_life_runtime import DailyLifeRuntime
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    for index in range(7):
        title = "周末画展" if index == 0 else f"推荐书籍{index}"
        quote = f"我答应参加{title}。"
        store.record_exchange(f"reply:old:{index}", quote, "好。", [{
            "id": f"project-{index}", "title": title, "detail": quote,
            "status": "planned", "kind": "shared", "actor": "user", "quote": quote,
        }], occurred_at=NOW + timedelta(minutes=index))
    assert "project-0" not in {p["id"] for p in store.snapshot(NOW)["shared"]}

    class Model:
        async def complete(self, messages, **kwargs):
            data = json.loads(messages[-1]["content"])
            original = next(p for p in data["previous_state"]["shared"] if p["title"] == "周末画展")
            assert original["actor"] == "user"
            assert original["quote"] == "我答应参加周末画展。"
            assert original["source_id"] == "reply:old:0"
            return SimpleNamespace(text=json.dumps({"updates": [{
                "id": original["id"], "title": original["title"], "detail": "取消画展",
                "status": "cancelled", "kind": "shared", "actor": "user",
                "quote": "周末画展的约定取消吧。",
            }]}))

    runtime = DailyLifeRuntime(store, lambda: Model(), lambda: "")
    asyncio.run(runtime.consume_exchange("reply:cancel:1", "周末画展的约定取消吧。", "好，取消。",
                                         occurred_at=NOW + timedelta(hours=1)))
    threads = json.loads(store.reply_context("周末画展还有安排吗？", now=NOW + timedelta(hours=1)))["threads"]
    assert len(threads) == 1
    assert threads[0]["id"] == "project-0" and threads[0]["status"] == "cancelled"


def test_completed_long_tail_keeps_every_identity_without_blocking_short_exchange(tmp_path):
    from runtime.private_world.daily_life_runtime import DailyLifeRuntime
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    for index in range(143):
        quote = f"第{index}册图书归还完毕。" + "核对借阅记录。" * 20
        store.record_exchange(f"reply:old:{index}", quote, "收到。", [{
            "id": f"book-{index}", "title": f"归还图书{index}", "detail": quote,
            "status": "completed", "kind": "shared", "actor": "user", "quote": quote,
        }], occurred_at=NOW)
    before = store.snapshot(NOW)

    class Model:
        calls = 0
        config = SimpleNamespace(max_input_chars=30000)

        async def complete(self, messages, **kwargs):
            self.calls += 1
            rows = json.loads(messages[-1]["content"])["previous_state"]["shared"]
            assert {row["id"] for row in rows} == {f"book-{i}" for i in range(143)}
            for row in rows:
                assert set(row) == {"id", "title", "kind", "actor", "status", "source_id", "updated_at"}
                assert row["actor"] == "user" and row["status"] == "completed"
            return SimpleNamespace(text='{"updates":[]}')

    model = Model()
    runtime = DailyLifeRuntime(store, lambda: model, lambda: "")
    assert asyncio.run(runtime.consume_exchange("reply:hello:1", "你好。", "晚上好。", occurred_at=NOW))
    assert model.calls == 1
    assert store.snapshot(NOW) == before
    with store._db() as db:
        assert db.execute("SELECT count(*) FROM life_projects").fetchone()[0] == 143
        assert all("quote" in json.loads(row[0]) for row in db.execute("SELECT payload FROM life_projects"))


@pytest.mark.parametrize("mention_in_reply", [False, True])
@pytest.mark.parametrize("mention", ["琥珀展览", "旧日安排"])
def test_terminal_same_title_items_restore_complete_evidence_for_either_speaker(tmp_path, mention_in_reply, mention):
    from runtime.private_world.daily_life_runtime import DailyLifeRuntime
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    quotes = {"user": "琥珀展览的约定取消。" + "核对旧行程。" * 30,
              "linli": "琥珀展览已经参观完毕。" + "保留旧票根。" * 30}
    for actor, quote in quotes.items():
        store.record_exchange(f"reply:{actor}:old", quote if actor == "user" else "嗯。",
                              quote if actor == "linli" else "嗯。", [{
            "id": f"exhibit-{actor}", "title": "旧日安排", "detail": "未经证实的概括",
            "status": "cancelled" if actor == "user" else "completed", "kind": "shared",
            "actor": actor, "quote": quote,
        }], occurred_at=NOW)
    # Either the title or the verified quotation retrieves both distinct ids.
    user = "你好。" if mention_in_reply else f"{mention}的约定仍然取消。"
    reply = f"{mention}我记得。" if mention_in_reply else "嗯。"

    class Model:
        async def complete(self, messages, **kwargs):
            rows = json.loads(messages[-1]["content"])["previous_state"]["shared"]
            assert len(rows) == 2
            for row in rows:
                actor = row["actor"]
                assert row["id"] == f"exhibit-{actor}"
                assert row["quote"] == row["detail"] == quotes[actor]
                assert row["source_id"] == f"reply:{actor}:old"
                assert row["updated_at"] == NOW.isoformat()
            return SimpleNamespace(text='{"updates":[]}')

    runtime = DailyLifeRuntime(store, lambda: Model(), lambda: "")
    asyncio.run(runtime.consume_exchange("reply:new:1", user, reply, occurred_at=NOW))
    compact = store.exchange_state()["shared"]
    assert len(compact) == 2 and all("quote" not in row and "detail" not in row for row in compact)


def test_compact_daily_identity_does_not_invent_a_quotation_speaker(tmp_path):
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    store.publish_day("day:old", {"location": "琴房", "activity": "练琴", "note": "休息。"}, [{
        "id": "piano", "title": "左手练习", "detail": "左手练习完毕。", "status": "completed",
    }], occurred_at=NOW)
    identity = store.exchange_state()["projects"][0]
    assert identity["actor"] is None and "quote" not in identity
    assert identity["source_id"] == "day:old" and identity["updated_at"] == NOW.isoformat()
    assert store.exchange_state("左手练习")["projects"][0] == store.snapshot(NOW)["projects"][0]


@pytest.mark.parametrize("stored_count,user_text,status", [(0, "长信正文" * 150, "planned"), (20, "你好。", "planned"), (20, "你好。", "completed")])
def test_exchange_over_budget_is_explicit_and_never_commits_partial_state(tmp_path, stored_count, user_text, status):
    from runtime.private_world.daily_life_runtime import DailyLifeRuntime, _EXCHANGE_PROMPT
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    for index in range(stored_count):
        quote = f"我会推荐第{index}本书。"
        store.record_exchange(f"reply:old:{index}", quote, "好。", [{
            "id": f"book-{index}", "title": "推荐书籍", "detail": quote, "status": status,
            "kind": "shared", "actor": "user", "quote": quote,
        }], occurred_at=NOW)
    before = store.exchange_state()

    class Model:
        config = SimpleNamespace(max_input_chars=len(_EXCHANGE_PROMPT) + 500)

        async def complete(self, *args, **kwargs):
            pytest.fail("Oversize context must not reach the provider")

    runtime = DailyLifeRuntime(store, lambda: Model(), lambda: "")
    with pytest.raises(ValueError, match="^DAILY_LIFE_CONTEXT_TOO_LARGE$"):
        asyncio.run(runtime.consume_exchange("reply:new:1", user_text, "好。", occurred_at=NOW))
    assert not store.has_source("reply:new:1")
    assert store.exchange_state() == before
