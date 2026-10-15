"""The one-off repair of her stored words that took over the user's self-address 俺."""
from datetime import datetime, timedelta, timezone
import json

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.self_name_migration import MIGRATION, her_words, migrate, run_once


NOW = datetime(2026, 10, 15, 10, 48, tzinfo=timezone.utc)
USER = "俺今天下午去排练，奶油色那套俺也挺喜欢。"
REPLY = "奶油色那套俺记下啦，既然你一上来就喜欢，那就买它吧。俺下课就去琴房。你刚才那句「俺饿了」我也记着呢。"


def exchange(store):
    updates = [
        dict(id="buy-cream", title="奶油色那套俺记下啦", detail="奶油色那套俺记下啦，既然你一上来就喜欢，那就买它吧。",
             status="planned", kind="shared", actor="linli", quote="奶油色那套俺记下啦，既然你一上来就喜欢，那就买它吧。"),
        dict(id="rehearse", title="俺今天下午去排练", detail="俺今天下午去排练",
             status="planned", kind="shared", actor="user", quote="俺今天下午去排练"),
    ]
    assert store.record_exchange("reply:qq:1", USER, REPLY, updates, occurred_at=NOW,
                                 current_quote="俺下课就去琴房。")
    with store._db() as db:
        db.execute("INSERT INTO life_exchange_world_gate VALUES (?,?,?)", ("reply:qq:1", "reconsider", REPLY))
        db.execute("INSERT INTO life_exchange_user_text VALUES (?,?)", ("reply:qq:1", USER))


def payloads(store, table, key="source_id"):
    with store._db() as db:
        return {row[0]: row[1] for row in db.execute(f"SELECT {key},payload FROM {table}")}


def test_her_words_keep_the_users_words_and_quotes():
    assert her_words("俺记下啦，俺们周末去。") == "我记下啦，我们周末去。"
    assert her_words("你刚才那句「俺饿了」俺也记着呢。") == "你刚才那句「俺饿了」我也记着呢。"
    assert her_words("用户说：俺明天面试。") == "用户说：俺明天面试。"
    assert her_words("你说俺做饭好吃？") == "你说俺做饭好吃？"
    assert her_words("我记下啦。") == "我记下啦。" and her_words(None) is None


def test_only_her_own_words_change_and_originals_are_backed_up(tmp_path):
    store = DailyLifeStore(tmp_path / "daily_life.sqlite3")
    exchange(store)
    before_moment = payloads(store, "life_moments")["reply:qq:1"]
    before_project = payloads(store, "life_projects", "id")
    snapshot_before = store.snapshot(NOW + timedelta(minutes=1))
    assert run_once(store) == 3  # exchange moment (her item and current statement), her project row, world-gate reply

    moment = json.loads(payloads(store, "life_moments")["reply:qq:1"])
    hers, users = moment["updates"]
    assert hers["title"] == "奶油色那套我记下啦"
    assert hers["quote"] == hers["detail"] == "奶油色那套我记下啦，既然你一上来就喜欢，那就买它吧。"
    assert users == json.loads(before_moment)["updates"][1]  # the user's own words stay
    assert moment["current"]["note"] == "我下课就去琴房。"
    assert moment["digest"] == json.loads(before_moment)["digest"]
    projects = payloads(store, "life_projects", "id")
    assert json.loads(projects["buy-cream"])["title"] == "奶油色那套我记下啦"
    assert projects["rehearse"] == before_project["rehearse"]
    with store._db() as db:
        gate = db.execute("SELECT reply_text FROM life_exchange_world_gate").fetchone()[0]
        said = db.execute("SELECT user_text FROM life_exchange_user_text").fetchone()[0]
        backups = dict(((t, k), o) for t, k, o in db.execute(
            "SELECT table_name,row_key,original FROM life_migration_backup WHERE migration=?", (MIGRATION,)))
    assert gate == "奶油色那套我记下啦，既然你一上来就喜欢，那就买它吧。我下课就去琴房。你刚才那句「俺饿了」我也记着呢。"
    assert said == USER
    assert backups[("life_moments", "reply:qq:1")] == before_moment
    assert backups[("life_projects", "buy-cream")] == before_project["buy-cream"]
    assert backups[("life_exchange_world_gate", "reply:qq:1")] == REPLY

    shared = {item["id"]: item for item in store.snapshot(NOW + timedelta(minutes=1))["shared"]}
    assert shared["buy-cream"]["title"] == "奶油色那套我记下啦"
    assert shared["rehearse"]["title"] == "俺今天下午去排练"
    assert len(shared) == len(snapshot_before["shared"])
    # The exchange still matches the original reply it was recorded from.
    assert store.exchange_relationship("reply:qq:1", USER, REPLY) is None


def test_runs_once_per_install_and_is_idempotent(tmp_path):
    store = DailyLifeStore(tmp_path / "daily_life.sqlite3")
    exchange(store)
    assert run_once(store) == 3
    once = payloads(store, "life_moments")
    assert run_once(store) == 0
    with store._db() as db:
        db.execute("BEGIN")
        assert migrate(db) == 0
        marker = json.loads(db.execute("SELECT value FROM life_settings WHERE key=?",
                                       ("migration:" + MIGRATION,)).fetchone()[0])
    assert marker["changed"] == 3
    assert payloads(store, "life_moments") == once


def test_title_not_cut_from_her_reply_is_left_alone(tmp_path):
    store = DailyLifeStore(tmp_path / "daily_life.sqlite3")
    reply = "俺下周把那首曲子练熟。"
    assert store.record_exchange("reply:qq:2", "加油。", reply, [dict(
        id="practice", title="练熟俺那首曲子的计划", detail=reply, status="planned", kind="linli",
        actor="linli", quote=reply)], occurred_at=NOW)
    run_once(store)
    item = json.loads(payloads(store, "life_moments")["reply:qq:2"])["updates"][0]
    assert item["quote"] == item["detail"] == "我下周把那首曲子练熟。"
    assert item["title"] == "练熟俺那首曲子的计划"  # not provably her reply's wording


def test_changed_item_keeps_its_time_scope(tmp_path):
    from runtime.private_world.project_timing import source_version, version
    store = DailyLifeStore(tmp_path / "daily_life.sqlite3")
    exchange(store)
    old = json.loads(payloads(store, "life_moments")["reply:qq:1"])["updates"][0]
    timing = {"id": old["id"], "version": version(old), "source_version": source_version(old), "scope": "bounded",
              "deadline_at": (NOW + timedelta(days=3)).isoformat(), "source_at": old["updated_at"], "contract_revision": 4}
    with store._db() as db:
        db.execute("INSERT INTO life_project_timing VALUES (?,?)", (timing["version"], json.dumps(timing)))
    before = {p["id"]: p for p in store.snapshot(NOW + timedelta(minutes=1))["shared"]}["buy-cream"]
    run_once(store)
    after = {p["id"]: p for p in store.snapshot(NOW + timedelta(minutes=1))["shared"]}["buy-cream"]
    assert after["title"] == "奶油色那套我记下啦"
    assert after.get("time_scope") == before.get("time_scope") == "bounded"
    assert after.get("deadline_at") == before.get("deadline_at") and "time_scope_pending" not in after


def test_failure_never_raises_and_retries_next_start(tmp_path, monkeypatch):
    from runtime.private_world import self_name_migration
    store = DailyLifeStore(tmp_path / "daily_life.sqlite3")
    exchange(store)
    def broken(db, now=None):
        raise RuntimeError("disk")
    monkeypatch.setattr(self_name_migration, "migrate", broken)
    assert run_once(store) is None
    monkeypatch.undo()
    assert run_once(store) == 3


def test_reply_statement_kept_as_current_activity_is_hers(tmp_path):
    store = DailyLifeStore(tmp_path / "daily_life.sqlite3")
    with store._db() as db:
        db.execute("INSERT INTO life_current VALUES (1,?)", (json.dumps(dict(
            location=None, activity=None, note="俺在琴房练琴。", source_id="reply:qq:3",
            occurred_at=NOW.isoformat()), ensure_ascii=False),))
    assert run_once(store) == 1
    with store._db() as db:
        assert json.loads(db.execute("SELECT payload FROM life_current").fetchone()[0])["note"] == "我在琴房练琴。"


def test_development_evidence_changes_only_her_quote(tmp_path):
    store = DailyLifeStore(tmp_path / "daily_life.sqlite3")
    event = dict(key="photography", stance="positive", user_quote="俺觉得你拍得真好。", character_quote="俺也觉得这次拍得顺手。",
                 experience_quote="你拍得真好", episode_id=None, withdraws=None, source_id="reply:qq:4", source_hash="x",
                 occurred_at=NOW.isoformat(), available_at=NOW.isoformat(), evidence_kind="shared_experience",
                 experience_id=None, policy="p")
    with store._db() as db:
        db.execute("INSERT INTO character_development_events VALUES (?,?,?,?)",
                   ("reply:qq:4", "photography", NOW.isoformat(), json.dumps(event, ensure_ascii=False)))
    assert run_once(store) == 1
    with store._db() as db:
        stored = json.loads(db.execute("SELECT payload FROM character_development_events").fetchone()[0])
    assert stored["character_quote"] == "我也觉得这次拍得顺手。"
    assert stored["user_quote"] == "俺觉得你拍得真好。" and stored["source_hash"] == "x"
