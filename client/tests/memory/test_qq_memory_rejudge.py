from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from conversation_memory_port import ConversationMemoryStatus, MemoryWriteResult, MemoryWriteStatus
from runtime.memory import qq_memory_rejudge as module
from runtime.memory.conversation_memory_delivery import (
    CanonicalMemoryDelivery,
    CanonicalMemoryDeliveryError,
    CanonicalMemoryDeliveryResult,
    CanonicalMemoryDeliveryStatus,
    ConversationMemoryDeliveryCommitter,
)
from runtime.memory.conversation_memory_outbox import CanonicalMemoryOutbox
from runtime.reply import jev_billing

CST = timezone(timedelta(hours=8))
INSIDE = datetime(2026, 10, 25, 12, 0, tzinfo=CST)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Committer:
    """Answers per source id; remembers every delivery it was given."""

    def __init__(self, answers=None, default=CanonicalMemoryDeliveryStatus.SKIPPED):
        self.answers = dict(answers or {})
        self.default = default
        self.calls = []

    async def commit(self, delivery):
        self.calls.append(delivery)
        answer = self.answers.get(delivery.source_id, self.default)
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        error = "MEM0_WRITE_FAILED" if answer is CanonicalMemoryDeliveryStatus.UNAVAILABLE else None
        return CanonicalMemoryDeliveryResult(
            answer, delivery.source_id,
            memory_count=1 if answer is CanonicalMemoryDeliveryStatus.WRITTEN else 0, error_code=error,
        )


def qq_row(name, when=INSIDE, text="我下周三要去做检查。", channel="qq", status="DELIVERED", **extra):
    return {
        "letter_id": name, "letter_status": "COMPLETED", "reply_revision": 1, "content": text,
        "reply_text": "好的。", "created_at": when.isoformat(), "channel": channel, "delivery_status": status, **extra,
    }


def write_state(path: Path, chats, letters=()):
    path.write_text(json.dumps({"letters": list(letters), "personal_chats": list(chats)}, ensure_ascii=False),
                    encoding="utf-8")


@pytest.fixture(autouse=True)
def cloud_judge(monkeypatch):
    monkeypatch.setattr("runtime.reply.jev_questions.configured_questions", lambda: object())
    monkeypatch.delenv(module.DISABLE_ENV, raising=False)


def build(tmp_path, chats, letters=(), first=CanonicalMemoryDeliveryStatus.SKIPPED):
    """An outbox whose journal already holds `first` for every delivered row (what the old client left)."""
    write_state(tmp_path / "state.json", chats, letters)
    old = Committer(default=first)
    box = CanonicalMemoryOutbox(tmp_path / "state.json", tmp_path / "memory" / "delivery.sqlite3", old)
    asyncio.run(box.scan_once())
    return box


def worker(box, committer, clock):
    box.committer = committer
    return module.QqMemoryRejudge(box, clock=clock)


def run_steps(job, clock, count, advance=module.BATCH_PAUSE_SECONDS + 1):
    out = []
    for _ in range(count):
        clock.advance(advance)
        out.append(asyncio.run(job.step()))
    return out


def states(box):
    with sqlite3.connect(box.journal_path) as db:
        return dict(db.execute("SELECT source_id, state FROM qq_memory_rejudge"))


def test_plan_takes_only_skipped_qq_user_lines_inside_the_window(tmp_path):
    outside = [qq_row("before", INSIDE.replace(month=10, day=20, hour=23)), qq_row("after", INSIDE.replace(month=11, day=4, hour=6))]
    rows = [
        qq_row("keep-me"),
        qq_row("edge-start", datetime(2026, 10, 21, 0, 0, tzinfo=CST)),
        qq_row("edge-end", datetime(2026, 11, 4, 5, 0, tzinfo=CST)),
        *outside,
        qq_row("long", text="很长。" * 200),
        qq_row("wechat", channel="wechat"),
        qq_row("undelivered", status="FAILED"),
        qq_row("proactive", origin="proactive", content=""),
        qq_row("empty-text", text="   "),
    ]
    letter = {**qq_row("a-letter"), "channel": None, "delivery_status": None}
    box = build(tmp_path, rows, [letter])
    job = worker(box, Committer(), Clock())
    assert job.plan() == 3
    assert set(states(box)) == {"reply:keep-me:1", "reply:edge-start:1", "reply:edge-end:1"}


def test_written_and_unjournaled_lines_are_not_repeated(tmp_path):
    write_state(tmp_path / "state.json", [qq_row("had-keeps"), qq_row("new-one")])
    box = CanonicalMemoryOutbox(tmp_path / "state.json", tmp_path / "memory" / "delivery.sqlite3",
                                Committer(default=CanonicalMemoryDeliveryStatus.WRITTEN))
    asyncio.run(box.scan_once())
    with sqlite3.connect(box.journal_path) as db:
        db.execute("DELETE FROM canonical_memory_deliveries WHERE source_id='reply:new-one:1'")
    job = worker(box, Committer(), Clock())
    assert job.plan() == 0


def test_steps_are_paced_one_request_at_a_time_and_flag_the_request(tmp_path):
    rows = [qq_row(f"m{n}", INSIDE + timedelta(minutes=n)) for n in range(25)]
    box = build(tmp_path, rows)
    committer = Committer()
    clock = Clock()
    job = worker(box, committer, clock)
    assert asyncio.run(job.step()) == "waiting"                 # the startup delay
    clock.advance(module.START_DELAY_SECONDS + 1)
    assert asyncio.run(job.step()) == "skipped"
    assert asyncio.run(job.step()) == "waiting"                 # the gap between two requests
    assert len(committer.calls) == 1
    assert committer.calls[0].rejudge is True and committer.calls[0].source_id == "reply:m24:1"  # newest first
    for _ in range(19):
        clock.advance(module.ITEM_GAP_SECONDS + .1)
        assert asyncio.run(job.step()) == "skipped"
    assert len(committer.calls) == 20
    clock.advance(module.ITEM_GAP_SECONDS + .1)
    assert asyncio.run(job.step()) == "waiting"                 # the batch pause
    clock.advance(module.BATCH_PAUSE_SECONDS)
    assert asyncio.run(job.step()) == "skipped"


def test_a_kept_line_is_journaled_as_written_and_a_skip_stays_terminal(tmp_path):
    box = build(tmp_path, [qq_row("good"), qq_row("chat", INSIDE + timedelta(minutes=1))])
    committer = Committer({"reply:good:1": CanonicalMemoryDeliveryStatus.WRITTEN})
    clock = Clock()
    job = worker(box, committer, clock)
    clock.advance(module.START_DELAY_SECONDS + 1)
    assert run_steps(job, clock, 3) == ["skipped", "kept", "finished"]
    assert states(box) == {"reply:good:1": "kept", "reply:chat:1": "skipped"}
    with sqlite3.connect(box.journal_path) as db:
        journal = dict(db.execute("SELECT source_id, status FROM canonical_memory_deliveries"))
    assert journal == {"reply:good:1": "written", "reply:chat:1": "duplicate"}
    assert box.health()["status"] == "available"


def test_the_work_happens_once_and_a_restart_does_not_plan_again(tmp_path):
    box = build(tmp_path, [qq_row("a"), qq_row("b", INSIDE + timedelta(minutes=1))])
    first = Committer()
    clock = Clock()
    job = worker(box, first, clock)
    clock.advance(module.START_DELAY_SECONDS + 1)
    assert run_steps(job, clock, 3) == ["skipped", "skipped", "finished"]
    assert len(first.calls) == 2
    # A later skipped line inside the window, and a new client start: nothing is queued or sent.
    write_state(tmp_path / "state.json", [qq_row("a"), qq_row("b", INSIDE + timedelta(minutes=1)), qq_row("late")])
    asyncio.run(CanonicalMemoryOutbox(tmp_path / "state.json", box.journal_path, Committer()).scan_once())
    again = Committer()
    clock2 = Clock()
    job2 = worker(CanonicalMemoryOutbox(tmp_path / "state.json", box.journal_path, again), again, clock2)
    clock2.advance(module.START_DELAY_SECONDS + 1)
    assert asyncio.run(job2.step()) == "finished"
    assert again.calls == []
    assert job2.progress()["done"] is True


def test_an_interrupted_run_resumes_with_the_remaining_lines(tmp_path):
    box = build(tmp_path, [qq_row("a"), qq_row("b", INSIDE + timedelta(minutes=1))])
    clock = Clock()
    job = worker(box, Committer(), clock)
    clock.advance(module.START_DELAY_SECONDS + 1)
    assert asyncio.run(job.step()) == "skipped"
    resumed = Committer()
    box2 = CanonicalMemoryOutbox(tmp_path / "state.json", box.journal_path, resumed)
    clock2 = Clock()
    job2 = worker(box2, resumed, clock2)
    clock2.advance(module.START_DELAY_SECONDS + 1)
    assert run_steps(job2, clock2, 2) == ["skipped", "finished"]
    assert [call.source_id for call in resumed.calls] == ["reply:a:1"]


def test_a_failing_line_is_tried_three_times_then_dropped_and_failures_pause_the_work(tmp_path):
    rows = [qq_row(f"m{n}", INSIDE + timedelta(minutes=n)) for n in range(2)]
    box = build(tmp_path, rows)
    failing = Committer(default=CanonicalMemoryDeliveryStatus.UNAVAILABLE)
    clock = Clock()
    job = worker(box, failing, clock)
    clock.advance(module.START_DELAY_SECONDS + 1)
    # five failures in a row stop the work for an hour before the second line has used its tries
    assert run_steps(job, clock, 6) == ["retry", "retry", "failed", "retry", "retry", "waiting"]
    assert run_steps(job, clock, 1, advance=module.FAILURE_PAUSE_SECONDS + 1) == ["failed"]
    assert states(box) == {"reply:m0:1": "failed", "reply:m1:1": "failed"}
    assert run_steps(job, clock, 1) == ["finished"]
    with sqlite3.connect(box.journal_path) as db:
        assert dict(db.execute("SELECT source_id, status FROM canonical_memory_deliveries")) == {
            "reply:m0:1": "duplicate", "reply:m1:1": "duplicate"}


def test_five_failures_in_a_row_stop_the_work_for_an_hour(tmp_path):
    rows = [qq_row(f"m{n}", INSIDE + timedelta(minutes=n)) for n in range(5)]
    box = build(tmp_path, rows)
    failing = Committer(default=CanonicalMemoryDeliveryStatus.UNAVAILABLE)
    clock = Clock()
    job = worker(box, failing, clock)
    clock.advance(module.START_DELAY_SECONDS + 1)
    for _ in range(5):
        clock.advance(module.ITEM_GAP_SECONDS + .1)
        asyncio.run(job.step())
    clock.advance(module.BATCH_PAUSE_SECONDS + 1)
    assert asyncio.run(job.step()) == "waiting"
    clock.advance(module.FAILURE_PAUSE_SECONDS)
    assert asyncio.run(job.step()) in {"retry", "failed"}


def test_account_cap_matches_the_service(tmp_path):
    rows = [qq_row(f"m{n}", INSIDE + timedelta(minutes=n), text="我在备考。" * 1) for n in range(5003)]
    box = build(tmp_path, rows)
    job = worker(box, Committer(), Clock())
    assert job.plan() == module.MAX_SENTENCES
    # the newest lines were queued first
    assert "reply:m5002:1" in states(box) and "reply:m0:1" not in states(box)


def test_sentences_are_counted_like_the_extraction_split():
    from runtime.memory.jev_memory_extraction import add_originals

    class Port:
        seen = None

        def ask_sync(self, state, questions, *, purpose):
            Port.seen = questions
            return {key: "skip" for key in questions}

    for text in ("我明天去医院。顺便买药！好吗？", "没有标点", "一。\n二\n\n三。"):
        add_originals(Port(), None, [{"role": "user", "content": text}], prompt="", metadata={})
        assert module.sentence_count(text) == len(Port.seen)


def test_nothing_runs_without_a_cloud_judge_or_when_switched_off(tmp_path, monkeypatch):
    box = build(tmp_path, [qq_row("a")])
    committer = Committer()
    clock = Clock()
    job = worker(box, committer, clock)
    clock.advance(module.START_DELAY_SECONDS + 1)
    monkeypatch.setattr("runtime.reply.jev_questions.configured_questions", lambda: None)
    assert asyncio.run(job.step()) == "disabled"
    monkeypatch.setattr("runtime.reply.jev_questions.configured_questions", lambda: object())
    clock.advance(module.FAILURE_PAUSE_SECONDS + 1)
    monkeypatch.setenv(module.DISABLE_ENV, "0")
    assert asyncio.run(job.step()) == "disabled"
    assert committer.calls == [] and job.progress()["planned"] is False


def test_a_deleted_or_vanished_line_is_dropped_without_a_request(tmp_path):
    box = build(tmp_path, [qq_row("a"), qq_row("b", INSIDE + timedelta(minutes=1))])
    clock = Clock()
    job = worker(box, Committer(), clock)
    job.plan()
    write_state(tmp_path / "state.json", [qq_row("a")])      # b was deleted in the meantime
    job._deliveries.clear()
    committer = job.outbox.committer
    clock.advance(module.START_DELAY_SECONDS + 1)
    assert run_steps(job, clock, 3) == ["gone", "skipped", "finished"]
    assert [call.source_id for call in committer.calls] == ["reply:a:1"]


def test_the_outbox_runs_the_worker_between_scans(tmp_path):
    box = build(tmp_path, [qq_row("a")])
    assert asyncio.run(box.rejudge_step()) == "waiting"
    assert box._rejudge is not None
    # health and scans are unaffected by the extra tables
    assert box.health()["status"] == "available"
    asyncio.run(box.scan_once())


# -- the request itself -----------------------------------------------------------------------------

def test_request_headers_name_the_rejudge_only_inside_the_scope(monkeypatch):
    monkeypatch.setenv("OLIVIA_JEV_BILLING_ENABLED", "1")
    jev_billing.configure_account(lambda: "olivia-test-key")
    plain = jev_billing.cloud_request_headers("a" * 64)
    assert jev_billing.REJUDGE_HEADER not in plain and plain["X-Olivia-Turn-Id"] == "jev:" + "a" * 64
    with jev_billing.rejudge_scope("reply:x:1"):
        inside = jev_billing.cloud_request_headers("a" * 64)
        local = jev_billing.billing_headers()
    assert inside[jev_billing.REJUDGE_HEADER] == jev_billing.REJUDGE_VALUE
    assert inside["X-Olivia-Turn-Id"].startswith("rejudge:") and len(inside["X-Olivia-Turn-Id"]) <= 128
    assert local[jev_billing.REJUDGE_HEADER] == jev_billing.REJUDGE_VALUE and local["X-Olivia-Turn-Id"] == inside["X-Olivia-Turn-Id"]
    assert jev_billing.REJUDGE.get() is None and jev_billing.CURRENT.get() is None
    again = jev_billing.cloud_request_headers("a" * 64)
    assert jev_billing.REJUDGE_HEADER not in again


def test_the_turn_id_belongs_to_the_source_not_to_the_text(monkeypatch):
    monkeypatch.setenv("OLIVIA_JEV_BILLING_ENABLED", "1")
    jev_billing.configure_account(lambda: "olivia-test-key")
    seen = []
    for source in ("reply:x:1", "reply:y:1", "reply:x:1"):
        with jev_billing.rejudge_scope(source):
            seen.append(jev_billing.cloud_request_headers("b" * 64)["X-Olivia-Turn-Id"])
    assert seen[0] == seen[2] != seen[1]


def test_the_provider_call_runs_inside_the_scope_and_normal_deliveries_do_not(monkeypatch):
    monkeypatch.setenv("OLIVIA_JEV_BILLING_ENABLED", "1")
    jev_billing.configure_account(lambda: "olivia-test-key")
    seen = []

    class Memory:
        def status(self):
            return ConversationMemoryStatus("available", True, "mem0", "qdrant-local")

        def remember_exchange(self, **kwargs):
            seen.append((jev_billing.REJUDGE.get(), kwargs["source_id"]))
            return MemoryWriteResult(MemoryWriteStatus.SKIPPED, kwargs["source_id"])

    committer = ConversationMemoryDeliveryCommitter(Memory())
    base = CanonicalMemoryDelivery(letter_id="q1", revision=1, user_message="我不吃香菜。", assistant_message="好。",
                                   occurred_at=INSIDE)
    result = asyncio.run(committer.commit(base))
    assert result.status is CanonicalMemoryDeliveryStatus.SKIPPED and seen[-1][0] is None
    from dataclasses import replace
    asyncio.run(committer.commit(replace(base, rejudge=True)))
    assert seen[-1][0].startswith("rejudge:") and seen[-1][1] == "reply:q1:1"
    with pytest.raises(CanonicalMemoryDeliveryError):
        replace(base, rejudge="yes")


def test_a_repeated_judgment_skips_the_reply_shadow_measurement(monkeypatch):
    from runtime.memory import mem0_memory

    calls = []
    monkeypatch.setattr(mem0_memory, "_after_delivery", lambda *args: calls.append(args))
    adapter = object.__new__(mem0_memory.Mem0ConversationMemoryAdapter)
    adapter._evidence_cache = {}
    adapter._fact_cache = None
    adapter._normalized_user_id = lambda value: value

    class Originals:
        def letter_deleted(self, *_):
            return True            # stops right after the shadow line, without any store

    adapter._originals = Originals()
    adapter._shadow_reply = lambda *a: None
    run = lambda: adapter._remember_resolved_exchange_transaction(
        user_message="我不吃香菜。", assistant_message="好。", occurred_at=INSIDE, source_id="reply:q:1", user_id="u")
    run()
    assert len(calls) == 1
    monkeypatch.setenv("OLIVIA_JEV_BILLING_ENABLED", "1")
    jev_billing.configure_account(lambda: "olivia-test-key")
    with jev_billing.rejudge_scope("reply:q:1"):
        run()
    assert len(calls) == 1


def _settlement(charged):
    return {"status": "settled", "turn_id": None, "operation_id": "op", "price_version": "v", "input_tokens": 10,
            "charged_units": charged, "replayed": True, "debited_units": 0}


def test_a_service_that_bills_the_repeat_stops_the_work(tmp_path, monkeypatch):
    monkeypatch.setenv("OLIVIA_JEV_BILLING_ENABLED", "1")
    monkeypatch.setattr(jev_billing, "REJUDGE_CHARGED", False)
    jev_billing.configure_account(lambda: "olivia-test-key")
    receipt = {"operation_id": "op", "input_tokens": 10}

    def settle(charged):
        result = _settlement(charged)
        monkeypatch.setattr(jev_billing, "_post_settlement", lambda key, signed: {**result, "turn_id": turn.turn_id})
        return jev_billing._settle(turn, {}, receipt)

    with jev_billing.billing_scope("letter:ordinary"):
        turn = jev_billing.CURRENT.get()
        assert settle(5)["charged_units"] == 5                      # an ordinary judgment is settled as before
    with jev_billing.rejudge_scope("reply:x:1"):
        turn = jev_billing.CURRENT.get()
        assert settle(0)["charged_units"] == 0                      # waived: fine
        assert jev_billing.REJUDGE_CHARGED is False
        with pytest.raises(ValueError, match="JEV_REJUDGE_CHARGED"):
            settle(5)
    assert jev_billing.REJUDGE_CHARGED is True
    box = build(tmp_path, [qq_row("a")])
    committer = Committer()
    clock = Clock()
    job = worker(box, committer, clock)
    clock.advance(module.START_DELAY_SECONDS + 1)
    assert asyncio.run(job.step()) == "stopped" and committer.calls == []
    monkeypatch.setattr(jev_billing, "REJUDGE_CHARGED", False)
