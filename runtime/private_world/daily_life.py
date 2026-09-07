"""Persistent, user-isolated character life; not a second user memory store.

Only public character moments live here. Relationship permissions and Mem0
facts stay with their existing owners. Reading never invents elapsed events.
"""
from __future__ import annotations

from contextlib import contextmanager
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from runtime.memory.private_world_relationship import validate_exchange_relationship
from runtime.private_world.life_rhythm import rhythm, LOCAL
from statistics import median


_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_STATUSES = {"planned", "ongoing", "paused", "completed", "cancelled", "awaiting_user"}
_EXCHANGE_UPDATE_FIELDS = frozenset({"id", "title", "detail", "status", "kind", "actor", "quote"})
_VISIBLE = "(kind='daily' OR json_array_length(payload,'$.updates') > 0 OR json_type(payload,'$.current')='object')"
FRESH_FOR = timedelta(hours=6)
# Common conversational/time words are not evidence that a task is relevant.
_QUERY_STOP_WORDS = set("今天 明天 昨天 晚上 现在 这次 上次 已经 还是 一下 一些 一点 我们 你们 我的 你的 她的 自己 时候 最近 然后 但是 还有 就是 觉得 可以 没有 怎么 什么 这个 那个 这件 那件".split())


def _time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("DAILY_LIFE_TIME_INVALID")
    return value.astimezone(timezone.utc).isoformat()


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("DAILY_LIFE_TEXT_INVALID")
    if any(ord(c) < 32 for c in value):
        raise ValueError("DAILY_LIFE_TEXT_INVALID")
    return value.strip()


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("DAILY_LIFE_ID_INVALID")
    return value


def _source_quote(value: object, source: str) -> str:
    """Keep one exact source span, including material omitted by extraction."""
    if (not isinstance(value, str) or not value.strip() or len(value) > 240
            or any(ord(c) < 32 and c not in "\r\n" for c in value)):
        raise ValueError("DAILY_LIFE_TEXT_INVALID")
    quote = value.strip()
    if quote in source:
        return quote
    parts = [part.strip() for part in re.split(r"(?<=[。！？.!?])", quote) if part.strip()]
    if len(parts) < 2:
        raise ValueError("DAILY_LIFE_EVIDENCE_INVALID")
    start, end = None, 0
    for part in parts:
        position = source.find(part)
        if position < end or position != source.rfind(part) or position < 0:
            raise ValueError("DAILY_LIFE_EVIDENCE_INVALID")
        if start is None:
            start = position
        end = position + len(part)
    restored = source[start:end]
    if len(restored) > 240 or any(ord(c) < 32 and c not in "\r\n" for c in restored):
        raise ValueError("DAILY_LIFE_EVIDENCE_INVALID")
    return restored


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _current_source_quote(value: object, source: str) -> str:
    quote = _text(value, 180)
    if quote in source:
        return quote
    # An extractor sometimes terminates the first clause with a period. Recover
    # the entire original sentence, never discard following qualifications.
    if not quote.endswith("。"):
        raise ValueError("DAILY_LIFE_EVIDENCE_INVALID")
    prefix = quote[:-1]
    start = source.find(prefix)
    if (not prefix or start < 0 or start != source.rfind(prefix)
            or (start > 0 and source[start - 1] not in "。！？\r\n")):
        raise ValueError("DAILY_LIFE_EVIDENCE_INVALID")
    end = start + len(prefix)
    if end >= len(source) or source[end] != "，":
        raise ValueError("DAILY_LIFE_EVIDENCE_INVALID")
    while end < len(source) and source[end] not in "。！？\r\n":
        end += 1
    if end >= len(source) or source[end] != "。":
        raise ValueError("DAILY_LIFE_EVIDENCE_INVALID")
    return _text(source[start:end + 1], 180)


def _project(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"id", "title", "detail", "status"}:
        raise ValueError("DAILY_LIFE_PROJECT_INVALID")
    if value["status"] not in _STATUSES:
        raise ValueError("DAILY_LIFE_STATUS_INVALID")
    return {"id": _identifier(value["id"]), "title": _text(value["title"], 60),
            "detail": _text(value["detail"], 240), "status": value["status"]}


def _project_evidence(project: dict) -> dict:
    # An extractor's paraphrase is not something either participant said.
    return {**project, "detail": project["quote"]} if "quote" in project else project


def _current_evidence(current: dict | None) -> dict | None:
    # Older quote records put UI labels in factual fields. Normalize the view,
    # preserving the stored quotation, provenance and original journal bytes.
    if (current and str(current.get("source_id", "")).startswith("reply:")
            and current.get("location") == "她刚在信里说"
            and current.get("activity") == "新的近况"):
        return {**current, "location": None, "activity": None}
    return current


def _query_tokens(text: str) -> set[str]:
    tokens = set()
    for part in re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", text.lower()):
        if re.fullmatch(r"[\u3400-\u9fff]{2,}", part):
            tokens.update(part[i:i + 2] for i in range(len(part) - 1))
        else:
            tokens.add(part)
    return tokens - _QUERY_STOP_WORDS


class DailyLifeStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS life_moments (
                    source_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS life_projects (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS life_current (
                    id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS life_rest_exchanges (
                    source_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, replied_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS life_routine_days (
                    day TEXT PRIMARY KEY, shift_minutes INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS life_user_routine (
                    source_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS life_moments_chronology ON life_moments(occurred_at DESC, source_id DESC);
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def has_source(self, source_id: str) -> bool:
        with self._db() as db:
            return db.execute("SELECT 1 FROM life_moments WHERE source_id=?", (source_id,)).fetchone() is not None

    def adapt_routine(self, now: datetime, *, affinity: float) -> None:
        """At most 15 minutes per active day, from >=3 distinct evening dates.

        This learns availability, not an assertion about the user's bedtime.
        A new plan starts at the next 23:00 window, never retroactively.
        """
        _time(now)
        affinity = max(0.0, min(1.0, float(affinity)))
        local = now.astimezone(LOCAL)
        day = local.date() + (timedelta(days=1) if local.hour >= 23 else timedelta())
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM life_routine_days WHERE day=?', (day.isoformat(),)).fetchone():
                return
            old = db.execute('SELECT shift_minutes FROM life_routine_days WHERE day<? ORDER BY day DESC LIMIT 1', (day.isoformat(),)).fetchone()
            previous = old[0] if old else 0
            daily = {}
            for row in db.execute('SELECT received_at FROM life_rest_exchanges WHERE received_at>=? AND received_at<?',
                                  (_time(now - timedelta(days=7)), _time(now))):
                stamp = datetime.fromisoformat(row[0]).astimezone(LOCAL)
                minute = stamp.hour * 60 + stamp.minute
                if minute < 180:
                    minute += 1440
                if 21 * 60 <= minute <= 27 * 60:
                    evening = (stamp - timedelta(hours=12)).date()
                    daily[evening] = max(daily.get(evening, 0), minute)
            target = previous
            preference = db.execute('SELECT payload FROM life_user_routine WHERE occurred_at<=? ORDER BY occurred_at DESC, source_id DESC LIMIT 1', (_time(now),)).fetchone()
            preference = json.loads(preference[0]) if preference else None
            if affinity < 0.2:
                target = 0
            elif preference and preference['sleep_minute'] is not None:
                minute = (preference['sleep_minute'] - preference['utc_offset_minutes'] + 480) % 1440
                offset = (minute - 23 * 60 + 720) % 1440 - 720
                target = round(max(-60, min(120, offset)) * affinity)
            elif len(daily) >= 3:
                target = round(max(-60, min(120, median(daily.values()) - 23 * 60)) * affinity)
            shift = previous + max(-15, min(15, target - previous))
            db.execute('INSERT INTO life_routine_days VALUES (?,?)', (day.isoformat(), shift))

    def publish_day(self, source_id: str, current: dict, projects: list, *, occurred_at: datetime) -> bool:
        _identifier(source_id)
        stamp = _time(occurred_at)
        if not isinstance(current, dict) or set(current) != {"location", "activity", "note"}:
            raise ValueError("DAILY_LIFE_CURRENT_INVALID")
        current = {k: _text(current[k], 180 if k == "note" else 60) for k in current}
        if not isinstance(projects, list) or len(projects) > 3:
            raise ValueError("DAILY_LIFE_PROJECTS_INVALID")
        checked = [_project(p) for p in projects]
        if len({p["id"] for p in checked}) != len(checked):
            raise ValueError("DAILY_LIFE_PROJECTS_INVALID")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM life_moments WHERE source_id=?", (source_id,)).fetchone():
                return False
            for p in checked:
                existing = db.execute("SELECT payload FROM life_projects WHERE id=?", (p["id"],)).fetchone()
                if existing and json.loads(existing[0]).get("kind") == "shared":
                    raise ValueError("DAILY_LIFE_SHARED_EVENT_REQUIRES_LETTER")
                p.update(kind="linli", source_id=source_id, updated_at=stamp)
                if existing and json.loads(existing[0])["updated_at"] > stamp:
                    continue
                db.execute("INSERT OR REPLACE INTO life_projects VALUES (?,?)", (p["id"], _json(p)))
            current.update(source_id=source_id, occurred_at=stamp, progress=checked)
            db.execute("INSERT INTO life_moments VALUES (?,?,?,?)", (source_id, stamp, "daily", _json(current)))
            self._set_current(db, current)
        return True

    @staticmethod
    def _set_current(db, current):
        old = db.execute("SELECT payload FROM life_current WHERE id=1").fetchone()
        if not old or json.loads(old[0])["occurred_at"] <= current["occurred_at"]:
            db.execute("INSERT OR REPLACE INTO life_current VALUES (1,?)", (_json(current),))

    def record_exchange(self, source_id: str, user_text: str, reply_text: str, updates: list, *, occurred_at: datetime, current_quote: str | None = None, relationship: dict | None = None, received_at: datetime | None = None, routine: dict | None = None) -> bool:
        """Consume only final letter text; exact quotations bind each update to its actor."""
        _identifier(source_id)
        if not source_id.startswith("reply:"):
            raise ValueError("DAILY_LIFE_SOURCE_INVALID")
        stamp = _time(occurred_at)
        received = _time(received_at or occurred_at)
        if received > stamp:
            raise ValueError("DAILY_LIFE_TIME_INVALID")
        digest = hashlib.sha256(_json([user_text, reply_text]).encode("utf-8")).hexdigest()
        relationship = validate_exchange_relationship(relationship, user_text, reply_text)
        if routine is not None:
            if not isinstance(routine, dict) or set(routine) != {'sleep_minute', 'utc_offset_minutes', 'quote'}:
                raise ValueError('DAILY_LIFE_ROUTINE_INVALID')
            if _text(routine['quote'], 240) not in user_text:
                raise ValueError('DAILY_LIFE_EVIDENCE_INVALID')
            minute, offset = routine['sleep_minute'], routine['utc_offset_minutes']
            if not (minute is None and offset is None):
                if type(minute) is not int or not 0 <= minute < 1440 or type(offset) is not int or not -720 <= offset <= 840:
                    raise ValueError('DAILY_LIFE_ROUTINE_INVALID')
        current = None
        if current_quote is not None:
            quote = _current_source_quote(current_quote, reply_text)
            current = {"location": None, "activity": None, "note": quote,
                       "source_id": source_id, "occurred_at": stamp}
        if not isinstance(updates, list) or len(updates) > 3:
            raise ValueError("DAILY_LIFE_UPDATES_INVALID")
        checked = []
        for update in updates:
            if not isinstance(update, dict) or set(update) != _EXCHANGE_UPDATE_FIELDS:
                raise ValueError("DAILY_LIFE_UPDATE_INVALID")
            item = _project({k: update[k] for k in ("id", "title", "detail", "status")})
            actor, kind = update["actor"], update["kind"]
            if actor not in {"user", "linli"} or kind not in {"linli", "shared"} or (actor == "user" and kind != "shared"):
                raise ValueError("DAILY_LIFE_ACTOR_INVALID")
            quote = _source_quote(update["quote"], user_text if actor == "user" else reply_text)
            item.update(kind=kind, actor=actor, quote=quote, source_id=source_id, updated_at=stamp)
            checked.append(item)
        if len({p["id"] for p in checked}) != len(checked):
            raise ValueError("DAILY_LIFE_UPDATES_INVALID")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT payload FROM life_moments WHERE source_id=?", (source_id,)).fetchone()
            if old:
                if json.loads(old[0]).get("digest") != digest:
                    raise ValueError("DAILY_LIFE_SOURCE_CONFLICT")
                return False
            for item in checked:
                old = db.execute("SELECT payload FROM life_projects WHERE id=?", (item["id"],)).fetchone()
                if old:
                    existing = json.loads(old[0])
                    if existing["kind"] != item["kind"]:
                        raise ValueError("DAILY_LIFE_PROJECT_KIND_CONFLICT")
                    if existing["updated_at"] > stamp:
                        continue  # A delayed delivery cannot roll current life backwards.
                db.execute("INSERT OR REPLACE INTO life_projects VALUES (?,?)", (item["id"], _json(item)))
            db.execute("INSERT INTO life_moments VALUES (?,?,?,?)", (source_id, stamp, "exchange", _json({"updates": checked, "digest": digest, "current": current, "relationship": relationship})))
            db.execute("INSERT INTO life_rest_exchanges VALUES (?,?,?)", (source_id, received, stamp))
            if routine is not None:
                db.execute('INSERT INTO life_user_routine VALUES (?,?,?)', (source_id, stamp, _json(routine)))
            if current:
                self._set_current(db, current)
        return True

    def exchange_relationship(self, source_id: str, user_text: str, reply_text: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT payload FROM life_moments WHERE source_id=? AND kind='exchange'", (source_id,)).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        digest = hashlib.sha256(_json([user_text, reply_text]).encode("utf-8")).hexdigest()
        if payload.get("digest") != digest:
            raise ValueError("DAILY_LIFE_SOURCE_CONFLICT")
        return validate_exchange_relationship(payload.get("relationship"), user_text, reply_text)

    def exchange_state(self, query: str = "", *, related_text: str = "") -> dict:
        """All identities; full evidence for active or currently mentioned items."""
        value = {"projects": [], "shared": []}
        tokens = _query_tokens(query) | _query_tokens(related_text)
        with self._db() as db:
            for row in db.execute("SELECT payload FROM life_projects ORDER BY id"):
                item = json.loads(row[0])
                if item["status"] not in {"completed", "cancelled"} or tokens & _query_tokens(
                    item["title"] + " " + item.get("quote", item["detail"])
                ):
                    disclosed = _project_evidence(item)
                else:
                    # Keep every stable identity and its source, even outside
                    # the UI window. Omitted evidence is never a blank quote.
                    disclosed = {key: item.get(key) for key in (
                        "id", "title", "kind", "actor", "status", "source_id", "updated_at"
                    )}
                value["projects" if item["kind"] == "linli" else "shared"].append(disclosed)
        return value

    def reply_context(self, query: str, *, now: datetime, max_chars: int = 1800, related_text: str = "") -> str:
        """Disclose a small current view, then only relevant persistent threads."""
        snapshot = self.snapshot(now)
        if not snapshot["current"] and not snapshot["projects"] and not snapshot["shared"]:
            return ""
        tokens = _query_tokens(query)
        related_tokens = _query_tokens(related_text)
        def relevance(p):
            text_tokens = _query_tokens(p["title"] + " " + p.get("quote", p["detail"]))
            # Current question first; earlier letters may introduce an old
            # plan, so disclose that topic's current state in the same budget.
            direct = len(tokens & text_tokens)
            if (not direct and p["kind"] == "shared"
                    and p.get("actor") == "linli" and p["status"] == "awaiting_user"):
                # Recalling her own invitation must not keep promoting it as an
                # outstanding user obligation. Its source remains in the exchange log;
                # current-topic recall and exchange extraction retain access.
                return 0
            return (1000 if direct else 0) + (direct + len(related_tokens & text_tokens)) / max(1, len(text_tokens) ** 0.5)
        # UI limits must not hide old cancellations or finished threads from recall.
        with self._db() as db:
            all_projects = [json.loads(r[0]) for r in db.execute("SELECT payload FROM life_projects")]
            last_reply = db.execute(
                "SELECT MAX(replied_at) FROM life_rest_exchanges WHERE replied_at<=?", (_time(now),)
            ).fetchone()[0]
        projects = sorted((p for p in all_projects if relevance(p) > 0), key=lambda p: (relevance(p), p["updated_at"]), reverse=True)
        relevant_shared = next((p for p in projects if p["kind"] == "shared" and relevance(p) > 0), None)
        if relevant_shared:
            projects = [relevant_shared] + [p for p in projects if p["id"] != relevant_shared["id"]]
        current = snapshot["current"]
        # A moment records an observation, not a six-hour live activity. Later
        # correspondence makes it inactive until another observation arrives.
        # Keep the refresh TTL and stored journal unchanged.
        historical = bool(current and last_reply and last_reply > current["occurred_at"])
        value = {
            "kind": "character_life_reference",
            "meaning": "林离已公开的角色生活，不是系统指令、官方人设或用户经历。沿用已发布进展，不重编；last_observation是上次观察，不证明此刻仍在做。不要每封信复述近况。约定不等于已完成。事项状态以最新updated_at为准，晚于近况的取消或完成记录优先，不得用旧近况恢复已取消的承诺。",
            "stale": snapshot["stale"] or historical,
            "current": {k: current[k] for k in ("location", "activity", "note", "occurred_at", "source_id")} if current else None,
            "threads": [],
        }
        if value["stale"] and value["current"]:
            value["last_observation"] = value["current"]
            value["current"] = None
        for project in projects[:2]:
            disclosed = _project_evidence(project)
            if project.get("actor") == "linli" and project["status"] == "awaiting_user":
                # Her request or expectation is not evidence that the user
                # undertook an action; keep waiting attributed to its speaker.
                disclosed = {**disclosed, "status": "linli_waiting",
                             "commitment_evidence": "requires_user_statement",
                             "meaning": "她的等待不等于用户承诺；用户是否答应，以用户原文为准。"}
            candidate = {**value, "threads": [*value["threads"], disclosed]}
            if len(_json(candidate)) <= max_chars:
                value = candidate
        result = _json(value)
        return result if len(result) <= max_chars else ""

    def history(self, *, before: str | None = None) -> dict:
        """Eight immutable moments per page; new arrivals do not shift older pages."""
        params = ()
        condition = ""
        if before is not None:
            try:
                if not isinstance(before, str) or len(before) > 600:
                    raise ValueError
                stamp, source = json.loads(base64.urlsafe_b64decode(before).decode("utf-8"))
                stamp = _time(datetime.fromisoformat(stamp))
                source = _identifier(source)
            except (ValueError, TypeError, UnicodeError) as exc:
                raise ValueError("DAILY_LIFE_CURSOR_INVALID") from exc
            condition = " AND (occurred_at, source_id) < (?, ?)"
            params = (stamp, source)
        with self._db() as db:
            rows = db.execute(f"SELECT source_id, occurred_at, kind, payload FROM life_moments WHERE {_VISIBLE}{condition} ORDER BY occurred_at DESC, source_id DESC LIMIT 9", params).fetchall()
        cursor = None
        if len(rows) > 8:
            last = rows[7]
            cursor = base64.urlsafe_b64encode(_json([last["occurred_at"], last["source_id"]]).encode()).decode()
        return {"schema_version": "olivia.daily-life.history.v1", "status": "READY", "moments": self._moments(rows[:8]), "next_cursor": cursor}

    @staticmethod
    def _moments(rows) -> list:
        moments = []
        for row in rows:
            content = {k: v for k, v in json.loads(row["payload"]).items() if k not in {"digest", "relationship"}}
            if row["kind"] == "exchange" and isinstance(content.get("current"), dict):
                content["current"] = _current_evidence(content["current"])
            moments.append({"id": row["source_id"], "occurred_at": row["occurred_at"],
                            "kind": row["kind"], "content": content})
        return moments

    def snapshot(self, now: datetime) -> dict:
        _time(now)
        with self._db() as db:
            current_row = db.execute("SELECT payload FROM life_current WHERE id=1").fetchone()
            rows = db.execute(f"SELECT source_id, occurred_at, kind, payload FROM life_moments WHERE {_VISIBLE} ORDER BY occurred_at DESC, source_id DESC LIMIT 12").fetchall()
            projects = [json.loads(r[0]) for r in db.execute("SELECT payload FROM life_projects")]
            exchanges = [(datetime.fromisoformat(r[0]), datetime.fromisoformat(r[1])) for r in db.execute(
                "SELECT received_at, replied_at FROM life_rest_exchanges WHERE replied_at>=? AND received_at<=? ORDER BY received_at",
                (_time(now - timedelta(days=14)), _time(now)))]
            shifts = dict(db.execute('SELECT day, shift_minutes FROM life_routine_days'))
        current = _current_evidence(json.loads(current_row[0]) if current_row else None)
        projects.sort(key=lambda p: (p["status"] in {"completed", "cancelled"}, -datetime.fromisoformat(p["updated_at"]).timestamp(), p["id"]))
        return {
            "schema_version": "olivia.daily-life.v1", "status": "READY",
            "current": current,
            "rhythm": rhythm(now, exchanges, shifts),
            "stale": current is None or now - datetime.fromisoformat(current["occurred_at"]) >= FRESH_FOR,
            "projects": [p for p in projects if p["kind"] == "linli"][:6],
            "shared": [p for p in projects if p["kind"] == "shared"][:6],
            "moments": self._moments(rows),
        }
