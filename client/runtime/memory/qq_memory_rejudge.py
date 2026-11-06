"""One-time second look at QQ lines the memory judge skipped by mistake (2026-10-21 .. 2026-11-04 05:00, China time).

Between 2026-10-21 and the day the service began reading one-line chat messages with chat criteria, almost every
short QQ message was judged with the rules written for long letters and skipped, and a skipped line is terminal in
the outbox journal.  This worker asks once more, for those lines only, in small paced batches, one request at a time,
in the background (it runs between outbox scans and never touches the reply path).  A kept sentence is written exactly
like any other keep, because the line goes through the same committer; a skipped one stays as it was.

Bookkeeping lives in the outbox journal (two small tables, content-free: source id, time, sentence count, state).
The queue is planned once and the plan is never extended; when nothing is queued any more a done marker ends the work
for good.  Each line is tried at most three times.  Requests carry the re-judge header (runtime.reply.jev_billing)
and a turn id of their own; the service accepts it only for this window and caps what one account may repeat.
"""

from __future__ import annotations

from runtime.personal_chat.channels import rich
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
import re
import sqlite3
import time

from runtime.memory.conversation_memory_delivery import CanonicalMemoryDeliveryStatus

WINDOW_START = datetime(2026, 10, 21, 0, 0, tzinfo=timezone(timedelta(hours=8)))
WINDOW_END = datetime(2026, 11, 4, 5, 0, tzinfo=timezone(timedelta(hours=8)))
CHAT_MAX_CHARS = 400          # a longer message is a letter, judged with the letter rules
MAX_SENTENCES = 5000          # per account, as the service allows
MAX_SPANS = 64                # add_originals refuses more spans in one message
MAX_FAILURES = 3              # tries per line
START_DELAY_SECONDS = 180.0   # after the memory worker starts
ITEM_GAP_SECONDS = 2.0        # between two requests
BATCH_SIZE = 20
BATCH_PAUSE_SECONDS = 60.0
FAILURE_PAUSE_SECONDS = 3600.0  # after five failures in a row (offline, refused, no balance...)
RETRY_PLAN_SECONDS = 300.0
DISABLE_ENV = "OLIVIA_QQ_MEMORY_REJUDGE"  # "0" switches the work off
_SENTENCE = re.compile(r"[^。！？!?\n]+[。！？!?\n]*")
_SETTLED = {
    CanonicalMemoryDeliveryStatus.SKIPPED: "skipped",
    CanonicalMemoryDeliveryStatus.DUPLICATE: "duplicate",
}


def sentence_count(text: str) -> int:
    """Questions the judge asks about a message (the same split as jev_memory_extraction.add_originals)."""
    return sum(1 for match in _SENTENCE.finditer(text) if match.group().strip())


class QqMemoryRejudge:
    def __init__(self, outbox, *, clock=time.monotonic) -> None:
        self.outbox = outbox
        self._clock = clock
        self._started = clock()
        self._not_before = self._started + START_DELAY_SECONDS
        self._in_batch = 0
        self._failures_in_a_row = 0
        self._deliveries: dict[str, object] = {}
        self._finished = False
        self._ensure_tables()

    # -- journal ---------------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        return self.outbox._connect()

    def _ensure_tables(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS qq_memory_rejudge (
                    source_id TEXT PRIMARY KEY,
                    occurred REAL NOT NULL,
                    sentences INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued',
                    failures INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS qq_memory_rejudge_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def _meta(self, key: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT value FROM qq_memory_rejudge_meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def _set_meta(self, key: str, value: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO qq_memory_rejudge_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def progress(self) -> dict[str, int | bool]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT state, COUNT(*) FROM qq_memory_rejudge GROUP BY state").fetchall()
        counts = {str(state): int(count) for state, count in rows}
        counts["planned"] = self._meta("planned") is not None
        counts["done"] = self._meta("done") is not None
        return counts

    # -- planning --------------------------------------------------------------------------------

    def _skipped_sources(self, source_ids: list[str]) -> set[str]:
        """Sources whose journal row is terminal 'duplicate': a skip, or a duplicate the committer answers locally."""
        found: set[str] = set()
        with closing(self._connect()) as connection:
            for start in range(0, len(source_ids), 500):
                chunk = source_ids[start:start + 500]
                marks = ",".join("?" for _ in chunk)
                found.update(
                    str(row[0])
                    for row in connection.execute(
                        f"SELECT source_id FROM canonical_memory_deliveries WHERE status='duplicate' "
                        f"AND source_id IN ({marks})",
                        chunk,
                    )
                )
        return found

    def _candidate_deliveries(self) -> list:
        """QQ user lines delivered in the window, as the outbox would deliver them (newest first)."""
        from runtime.memory.conversation_memory_outbox import _delivery_from_row

        payload = self.outbox._read_state()
        rows = payload.get("personal_chats", ()) if payload is not None else ()
        found = {}
        for row in rows:
            if not isinstance(row, dict) or not rich(row.get("channel")) or row.get("delivery_status") != "DELIVERED":
                continue
            delivery = _delivery_from_row(row, user_id=self.outbox.user_id)
            if delivery is None or delivery.origin != "user" or not delivery.user_message.strip():
                continue
            if not WINDOW_START <= delivery.occurred_at <= WINDOW_END or len(delivery.user_message) > CHAT_MAX_CHARS:
                continue
            if not 1 <= sentence_count(delivery.user_message) <= MAX_SPANS:
                continue
            found.setdefault(delivery.source_id, delivery)
        skipped = self._skipped_sources(list(found))
        return sorted((found[source] for source in found if source in skipped),
                      key=lambda delivery: delivery.occurred_at, reverse=True)

    def plan(self) -> int:
        """Queue the lines once. Returns how many were queued."""
        deliveries = self._candidate_deliveries()
        queued, total = [], 0
        for delivery in deliveries:
            sentences = sentence_count(delivery.user_message)
            if total + sentences > MAX_SENTENCES:
                continue
            total += sentences
            queued.append((delivery.source_id, delivery.occurred_at.timestamp(), sentences))
        stamp = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                "INSERT OR IGNORE INTO qq_memory_rejudge(source_id, occurred, sentences, updated_at) VALUES (?, ?, ?, ?)",
                [(source, occurred, sentences, stamp) for source, occurred, sentences in queued],
            )
            connection.execute(
                "INSERT OR REPLACE INTO qq_memory_rejudge_meta(key, value) VALUES ('planned', ?)", (stamp,)
            )
        self._deliveries = {delivery.source_id: delivery for delivery in deliveries}
        return len(queued)

    # -- work ------------------------------------------------------------------------------------

    def _next(self):
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT source_id, failures FROM qq_memory_rejudge WHERE state='queued' "
                "ORDER BY occurred DESC LIMIT 1"
            ).fetchone()

    def _mark(self, source_id: str, state: str, failures: int | None = None) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE qq_memory_rejudge SET state=?, failures=COALESCE(?, failures), updated_at=? WHERE source_id=?",
                (state, failures, datetime.now(timezone.utc).isoformat(), source_id),
            )

    def _enabled(self) -> bool:
        if os.environ.get(DISABLE_ENV) == "0":
            return False
        from runtime.reply.jev_questions import configured_questions

        return configured_questions() is not None  # no cloud judge, nothing to repeat

    async def step(self) -> str:
        """At most one request. Returns what happened (for tests and diagnostics)."""
        if self._finished:
            return "finished"
        now = self._clock()
        if now < self._not_before:
            return "waiting"
        if self._meta("done") is not None:
            self._finished = True
            return "finished"
        from runtime.reply import jev_billing

        if jev_billing.REJUDGE_CHARGED:  # the service billed a repeat: no more requests in this run
            self._finished = True
            return "stopped"
        if not self._enabled():
            self._not_before = now + FAILURE_PAUSE_SECONDS
            return "disabled"
        if self._meta("planned") is None:
            try:
                self.plan()
            except Exception:  # noqa: BLE001 - state unreadable right now; try again later
                self._not_before = now + RETRY_PLAN_SECONDS
                return "plan-failed"
        item = self._next()
        if item is None:
            self._set_meta("done", datetime.now(timezone.utc).isoformat())
            self._finished = True
            self._deliveries.clear()
            return "finished"
        source_id, failures = str(item[0]), int(item[1])
        delivery = self._deliveries.get(source_id)
        if delivery is None:
            try:
                self._deliveries = {d.source_id: d for d in self._candidate_deliveries()}
            except Exception:  # noqa: BLE001
                self._not_before = now + RETRY_PLAN_SECONDS
                return "plan-failed"
            delivery = self._deliveries.get(source_id)
        if delivery is None:  # the letter is gone, or no longer a skipped QQ line
            self._mark(source_id, "failed")
            return "gone"
        return await self._judge(source_id, failures, delivery, now)

    async def _judge(self, source_id: str, failures: int, delivery, now: float) -> str:
        committer = self.outbox.committer
        result = await committer.commit(replace(delivery, rejudge=True))
        drain = getattr(committer, "drain_completed", None)
        if callable(drain):
            for completed, completed_result in drain():  # earlier calls that finished after their timeout
                if not completed.rejudge:
                    self.outbox._record(completed, completed_result)
                elif completed_result.status is CanonicalMemoryDeliveryStatus.WRITTEN:
                    self.outbox._record(completed, completed_result)
                    self._mark(completed.source_id, "kept")
                elif completed_result.status in _SETTLED:
                    self._mark(completed.source_id, _SETTLED[completed_result.status])
        status = result.status
        self._not_before = now + ITEM_GAP_SECONDS
        self._in_batch += 1
        if self._in_batch >= BATCH_SIZE:
            self._in_batch = 0
            self._not_before = now + BATCH_PAUSE_SECONDS
        if status is CanonicalMemoryDeliveryStatus.WRITTEN:
            self.outbox._record(delivery, result)
            self._mark(source_id, "kept")
            self._failures_in_a_row = 0
            return "kept"
        if status is CanonicalMemoryDeliveryStatus.SKIPPED:
            self._mark(source_id, "skipped")
            self._failures_in_a_row = 0
            return "skipped"
        if status is CanonicalMemoryDeliveryStatus.DUPLICATE:
            self._mark(source_id, "duplicate")
            self._failures_in_a_row = 0
            return "duplicate"
        failures += 1
        self._mark(source_id, "failed" if failures >= MAX_FAILURES else "queued", failures)
        self._failures_in_a_row += 1
        if self._failures_in_a_row >= 5:
            self._failures_in_a_row = 0
            self._not_before = now + FAILURE_PAUSE_SECONDS
        return "failed" if failures >= MAX_FAILURES else "retry"


__all__ = ["QqMemoryRejudge", "sentence_count", "WINDOW_START", "WINDOW_END"]
