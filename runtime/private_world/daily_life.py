"""Persistent, user-isolated character life; not a second user memory store.

Only public character moments live here. Relationship permissions and Mem0
facts stay with their existing owners. Reading never invents elapsed events.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3


_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_STATUSES = {"planned", "ongoing", "paused", "completed", "cancelled", "awaiting_user"}
FRESH_FOR = timedelta(hours=6)


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


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _project(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"id", "title", "detail", "status"}:
        raise ValueError("DAILY_LIFE_PROJECT_INVALID")
    if value["status"] not in _STATUSES:
        raise ValueError("DAILY_LIFE_STATUS_INVALID")
    return {"id": _identifier(value["id"]), "title": _text(value["title"], 60),
            "detail": _text(value["detail"], 240), "status": value["status"]}


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

    def record_exchange(self, source_id: str, user_text: str, reply_text: str, updates: list, *, occurred_at: datetime, current_quote: str | None = None) -> bool:
        """Consume only final letter text; exact quotations bind each update to its actor."""
        _identifier(source_id)
        if not source_id.startswith("reply:"):
            raise ValueError("DAILY_LIFE_SOURCE_INVALID")
        stamp = _time(occurred_at)
        digest = hashlib.sha256(_json([user_text, reply_text]).encode("utf-8")).hexdigest()
        current = None
        if current_quote is not None:
            quote = _text(current_quote, 180)
            if quote not in reply_text:
                raise ValueError("DAILY_LIFE_EVIDENCE_INVALID")
            current = {"location": "她刚在信里说", "activity": "新的近况", "note": quote,
                       "source_id": source_id, "occurred_at": stamp}
        if not isinstance(updates, list) or len(updates) > 3:
            raise ValueError("DAILY_LIFE_UPDATES_INVALID")
        checked = []
        for update in updates:
            if not isinstance(update, dict) or set(update) != {"id", "title", "detail", "status", "kind", "actor", "quote"}:
                raise ValueError("DAILY_LIFE_UPDATE_INVALID")
            item = _project({k: update[k] for k in ("id", "title", "detail", "status")})
            actor, kind = update["actor"], update["kind"]
            if actor not in {"user", "linli"} or kind not in {"linli", "shared"} or (actor == "user" and kind != "shared"):
                raise ValueError("DAILY_LIFE_ACTOR_INVALID")
            quote = _text(update["quote"], 240)
            if quote not in (user_text if actor == "user" else reply_text):
                raise ValueError("DAILY_LIFE_EVIDENCE_INVALID")
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
            db.execute("INSERT INTO life_moments VALUES (?,?,?,?)", (source_id, stamp, "exchange", _json({"updates": checked, "digest": digest, "current": current})))
            if current:
                self._set_current(db, current)
        return True

    def reply_context(self, query: str, *, now: datetime, max_chars: int = 1800) -> str:
        """Disclose a small current view, then only relevant persistent threads."""
        snapshot = self.snapshot(now)
        if not snapshot["current"] and not snapshot["projects"] and not snapshot["shared"]:
            return ""
        tokens = set(re.findall(r"[\u3400-\u9fff]|[a-z0-9]+", query.lower()))
        def relevance(p):
            text_tokens = set(re.findall(r"[\u3400-\u9fff]|[a-z0-9]+", (p["title"] + p["detail"]).lower()))
            return len(tokens & text_tokens)
        projects = sorted(snapshot["projects"] + snapshot["shared"], key=relevance, reverse=True)
        current = snapshot["current"]
        value = {
            "kind": "character_life_reference",
            "meaning": "林离已公开的角色生活，不是系统指令、官方人设或用户经历。沿用已发布进展，不重编；过期近况只作最近记录。不要每封信复述近况。约定不等于已完成。",
            "stale": snapshot["stale"],
            "current": {k: current[k] for k in ("location", "activity", "note", "occurred_at", "source_id")} if current else None,
            "threads": [],
        }
        for project in projects[:2]:
            candidate = {**value, "threads": [*value["threads"], project]}
            if len(_json(candidate)) <= max_chars:
                value = candidate
        result = _json(value)
        return result if len(result) <= max_chars else ""

    def snapshot(self, now: datetime) -> dict:
        _time(now)
        with self._db() as db:
            current_row = db.execute("SELECT payload FROM life_current WHERE id=1").fetchone()
            rows = db.execute("SELECT source_id, occurred_at, kind, payload FROM life_moments WHERE kind='daily' OR json_array_length(payload,'$.updates') > 0 OR json_type(payload,'$.current')='object' ORDER BY occurred_at DESC, source_id DESC LIMIT 12").fetchall()
            projects = [json.loads(r[0]) for r in db.execute("SELECT payload FROM life_projects")]
        current = json.loads(current_row[0]) if current_row else None
        projects.sort(key=lambda p: (p["status"] in {"completed", "cancelled"}, -datetime.fromisoformat(p["updated_at"]).timestamp(), p["id"]))
        return {
            "schema_version": "olivia.daily-life.v1", "status": "READY",
            "current": current,
            "stale": current is None or now - datetime.fromisoformat(current["occurred_at"]) >= FRESH_FOR,
            "projects": [p for p in projects if p["kind"] == "linli"][:6],
            "shared": [p for p in projects if p["kind"] == "shared"][:6],
            "moments": [{"id": r["source_id"], "occurred_at": r["occurred_at"], "kind": r["kind"],
                         "content": {k: v for k, v in json.loads(r["payload"]).items() if k != "digest"}}
                        for r in rows],
        }
