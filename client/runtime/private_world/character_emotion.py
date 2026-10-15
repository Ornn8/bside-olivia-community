"""Evidence-bound character appraisals; no world or relationship authority."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from itertools import islice
from pathlib import Path
import sqlite3
import threading
import time


# A fictional continuity parameter, not a measured psychological recovery rate.
REACTION_WINDOW = timedelta(hours=6)
MAX_SOURCE_CHARS = 100_000
_BATCH_LIMIT = 32
_VIEW_LIMIT = 12
from .jev_emotion import REACTIONS as _REACTION_MEANINGS, active_concerns
_REACTIONS = set(_REACTION_MEANINGS)
_ACTIONS = {"none", "continue", "adjust", "rest", "share", "quiet"}
_FIELDS = {"source_id", "quote", "reported_affect", "reaction", "goal_or_need",
           "action_tendency", "concern", "revises"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _time(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("EMOTION_TIME_INVALID")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _string(value, maximum, *, empty=False):
    return isinstance(value, str) and len(value) <= maximum and "\x00" not in value and (empty or bool(value.strip()))


def _id(value):
    return _string(value, 240) and not any(char.isspace() or ord(char) < 32 for char in value)


def _source(source_id, kind, text, stamp):
    if not _id(source_id) or not _string(text, MAX_SOURCE_CHARS):
        raise ValueError("EMOTION_SOURCE_INVALID")
    value = dict(source_id=source_id, source_kind=kind, text=text, occurred_at=stamp)
    return {**value, "source_hash": _hash(value)}


def _checked_source(raw, key=None, order=None):
    try:
        value = json.loads(raw)
        if (value["source_kind"] not in {"received_input", "published_world"}
                or key is not None and value["source_id"] != key
                or value != _source(value["source_id"], value["source_kind"], value["text"],
                                    _time(datetime.fromisoformat(value["occurred_at"])) )):
            raise ValueError
        return {**value, "source_order": order} if order is not None else value
    except (ValueError, TypeError, KeyError):
        raise ValueError("EMOTION_STORED_EVIDENCE_INVALID") from None


def _world_source(db, source_id):
    try:
        row = db.execute("SELECT occurred_at,kind,payload FROM life_moments WHERE source_id=?", (source_id,)).fetchone()
    except sqlite3.OperationalError:
        raise ValueError("EMOTION_SOURCE_UNAVAILABLE") from None
    if row is None or row[1] != "daily":
        raise ValueError("EMOTION_SOURCE_UNAVAILABLE")
    try:
        value = json.loads(row[2])
        note = value["note"]
        if not isinstance(note, str):
            raise ValueError
        # Keep both the published wording and structured progress available.
        text = note + "\n" + _json(value)
        return _source(source_id, "published_world", text, _time(datetime.fromisoformat(row[0])))
    except (ValueError, TypeError, KeyError):
        raise ValueError("EMOTION_SOURCE_UNAVAILABLE") from None


def _concern_changes(value):
    """Old records keep their singular shape and hashes; new records may carry many."""
    return value if isinstance(value, list) else [value] if value is not None else []


def _validate_item(item, source):
    if not isinstance(item, dict) or set(item) != _FIELDS:
        raise ValueError("EMOTION_APPRAISAL_INVALID")
    if (item["source_id"] != source["source_id"]
            or not _string(item["reaction"], 40) or item["reaction"] not in _REACTIONS
            or not _string(item["action_tendency"], 40) or item["action_tendency"] not in _ACTIONS
            or not _string(item["quote"], 240, empty=True)
            or item["quote"] not in source["text"]
            or item["goal_or_need"] is not None and not _string(item["goal_or_need"], 160)):
        raise ValueError("EMOTION_APPRAISAL_INVALID")
    meaningful = (item["reaction"] != "none" or item["action_tendency"] != "none"
                  or item["goal_or_need"] is not None or item["concern"] is not None or item["revises"] is not None)
    if meaningful and not item["quote"].strip():
        raise ValueError("EMOTION_APPRAISAL_INVALID")
    reported = item["reported_affect"]
    if reported is not None and (
            not isinstance(reported, dict) or set(reported) != {"subject", "quote", "affect"}
            or not _string(reported.get("subject"), 40) or reported["subject"] not in {"user", "third_party", "unclear"}
            or not _string(reported.get("affect"), 40) or reported["affect"] not in _REACTIONS - {"none"}
            or not _string(reported.get("quote"), 240) or reported["quote"] not in source["text"]):
        raise ValueError("EMOTION_APPRAISAL_INVALID")
    changes = _concern_changes(item["concern"])
    if len(changes) > 33:
        raise ValueError("EMOTION_APPRAISAL_INVALID")
    identifiers = set()
    for concern in changes:
        if (
            not isinstance(concern, dict) or set(concern) != {"id", "action", "summary"}
            or not _id(concern.get("id")) or not _string(concern.get("action"), 16)
            or concern["action"] not in {"open", "resolve"} or not _string(concern.get("summary"), 200)):
            raise ValueError("EMOTION_APPRAISAL_INVALID")
        if concern['id'] in identifiers:
            raise ValueError("EMOTION_APPRAISAL_INVALID")
        identifiers.add(concern['id'])
    revision = item["revises"]
    if revision is not None and (
            not isinstance(revision, dict) or set(revision) != {"source_id", "action"}
            or not _id(revision.get("source_id")) or revision.get("action") != "withdraw"
            or revision["source_id"] == item["source_id"]):
        raise ValueError("EMOTION_APPRAISAL_INVALID")


def _parse_row(source_raw, order, raw, version, basis_raw):
    source, item, basis = _checked_source(source_raw, order=order), json.loads(raw), json.loads(basis_raw)
    _validate_item(item, source)
    dependencies = basis.get("dependencies", {})
    if _hash({"source": source, "appraisal": item, "dependencies": dependencies}) != version:
        raise ValueError("EMOTION_STORED_EVIDENCE_INVALID")
    return {**item, "occurred_at": source["occurred_at"], "source_order": order,
            "version": version, "_dependencies": dependencies}


def _chunks(values, size=500):
    for start in range(0, len(values), size):
        yield values[start:start + size]


_CONTEXT_ITEMS = 2 * _VIEW_LIMIT
# Stored rows are hashed again, from scratch, at least this often (seconds).
_LEDGER_MAX_AGE = 900.0
_LEDGER_LIMIT = 8


class _Projection:
    """Append-only form of the projection of accepted appraisals.

    Records arrive in durable order. Adding one touches only what it can change
    (the chain it withdraws, the concerns it names) instead of replaying the
    whole list; a record that flips an earlier one refolds the concerns once.
    """

    def __init__(self):
        self.items, self.stamps = [], []
        self.by_id, self.children, self.waiting, self.parent, self.active = {}, {}, {}, {}, {}
        self.live, self.live_stamps, self.position = [], [], {}
        self.concerns, self.rank, self.open_ids = OrderedDict(), {}, set()

    def add(self, item, stamp):
        identity = item["source_id"]
        self.items.append(item)
        self.stamps.append(stamp)
        self.by_id[identity] = item
        self.children[identity] = self.waiting.pop(identity, [])
        for child in self.children[identity]:
            self.parent[child] = identity
        revised = item["revises"]["source_id"] if item["revises"] else None
        if revised is not None:
            if revised in self.by_id:
                self.children[revised].append(identity)
                self.parent[identity] = revised
            else:
                self.waiting.setdefault(revised, []).append(identity)
        node = identity
        for _ in range(len(self.by_id)):
            if node not in self.parent:
                break
            node = self.parent[node]
            if node == identity:  # Any new cycle passes through the new record.
                raise ValueError("EMOTION_STORED_EVIDENCE_INVALID")
        self.active[identity] = not any(self.active[child] for child in self.children[identity])
        flipped, node = False, identity
        while node in self.parent:
            above = self.parent[node]
            alive = not any(self.active[child] for child in self.children[above])
            if alive == self.active[above]:
                break
            self.active[above], flipped, node = alive, True, above
        if flipped:
            self._refold()
        elif self.active[identity]:
            self._admit(item, stamp)

    def _admit(self, item, stamp):
        self.position[item["source_id"]] = len(self.live)
        self.live.append(item)
        self.live_stamps.append(stamp)
        touched = set()
        for change in _concern_changes(item["concern"]):
            old = self.concerns.get(change["id"])
            contributors = list(old["source_ids"]) if old and old["status"] == "open" else []
            if change["action"] == "open":
                contributors.append(item["source_id"])
            else:
                contributors = []
            self.rank.setdefault(change["id"], len(self.rank))
            self.concerns[change["id"]] = dict(
                id=change["id"], summary=change["summary"], status=change["action"],
                source_ids=contributors, occurred_at=item["occurred_at"], source_order=item["source_order"],
                version=_hash([old["version"] if old else None, item["version"]]))
            (self.open_ids.add if change["action"] == "open" else self.open_ids.discard)(change["id"])
            touched.add(change["id"])
        # The order is the last change's (occurred_at, source_order); one record's own
        # changes tie and keep the order in which the concerns first appeared.
        for key in sorted(touched, key=self.rank.__getitem__):
            self.concerns.move_to_end(key)

    def _refold(self):
        items, stamps = self.items, self.stamps
        self.live, self.live_stamps, self.position = [], [], {}
        self.concerns, self.rank, self.open_ids = OrderedDict(), {}, set()
        for item, stamp in zip(items, stamps):
            if self.active[item["source_id"]]:
                self._admit(item, stamp)

    def live_version(self, identity):
        item = self.by_id.get(identity)
        return item["version"] if item is not None and self.active[identity] else None

    def concern_version(self, identity):
        concern = self.concerns.get(identity)
        return concern["version"] if concern else None

    def context(self, stamp):
        """The bounded concerns and prior appraisals an appraisal at stamp reads."""
        count = len(self.live)
        recent = datetime.fromisoformat(stamp) - REACTION_WINDOW
        chosen = set(range(max(bisect_left(self.live_stamps, recent), count - _CONTEXT_ITEMS), count))
        chosen.update(sorted(self.position[key] for identity in self.open_ids
                             for key in self.concerns[identity]["source_ids"]
                             if key in self.position)[-_CONTEXT_ITEMS:])
        tail = list(islice(reversed(self.concerns.values()), _CONTEXT_ITEMS))
        tail.reverse()
        return dict(as_of=stamp, concerns=[CharacterEmotionStore._public_concern(c) for c in tail],
                    prior_appraisals=[CharacterEmotionStore._public(self.live[i])
                                      for i in sorted(chosen)[-_CONTEXT_ITEMS:]])


class _StateView:
    """Live records, concerns and stale sources up to a time, with an optional context."""
    __slots__ = ("live", "live_stamps", "concerns", "stale", "context", "_by_id")

    def __init__(self, live, live_stamps, concerns, stale, context=None):
        self.live, self.live_stamps, self.concerns = live, live_stamps, concerns
        self.stale, self.context, self._by_id = stale, context, None

    def record(self, identity):
        if self._by_id is None:
            self._by_id = {item["source_id"]: item for item in self.live}
        return self._by_id.get(identity)

    def public(self, identity):
        record = self.record(identity)
        return CharacterEmotionStore._public(record) if record is not None else None


class _Unusable(Exception):
    """The ledger cannot stand in for the reference replay on this data."""


_ROWS = ("SELECT s.rowid,s.source_id,s.occurred_at,s.payload,a.payload,a.version,a.basis "
         "FROM character_emotion_appraisals a JOIN character_emotion_sources s ON s.source_id=a.source_id")
_FINGERPRINT = ("SELECT COUNT(*),COALESCE(MAX(rowid),0),(SELECT version FROM character_emotion_appraisals "
                "WHERE rowid=(SELECT MAX(rowid) FROM character_emotion_appraisals)) FROM character_emotion_appraisals")


class _Ledger:
    """Verified appraisal records in durable order, replayed once.

    Whether a record is accepted depends only on the records before it, so any
    time or source cut is a prefix of one replay. The ledger is derived from the
    stored rows alone and syncs to them on every use: rows added after the last
    record are replayed on their own, anything else is replayed from the first
    row that differs. Data it cannot model falls back to the reference replay.
    A row is parsed and hashed only when its stored text is new to the ledger;
    unchanged rows are compared as text, which is as strong as hashing them again.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.fingerprint = self.refused = None
        self.built = 0.0
        self.known = {}
        self._clear()

    def _clear(self):
        self.idents, self.records, self.stamps, self.columns, self.keys = [], [], [], [], []
        self.verdicts, self.stale = [], []
        self.projection = _Projection()

    def sync(self, db):
        aged = time.monotonic() - self.built > _LEDGER_MAX_AGE
        for _ in range(3):
            seen = tuple(db.execute(_FINGERPRINT).fetchone())
            if seen == self.fingerprint and not aged:
                return
            if seen == self.refused and not aged:
                raise _Unusable
            try:
                rebuilt = self._advance(db, seen, aged)
            except sqlite3.Error:
                self._clear()
                self.fingerprint = None
                raise
            except Exception:
                self._clear()
                self.fingerprint, self.refused = None, seen
                raise _Unusable from None
            if tuple(db.execute(_FINGERPRINT).fetchone()) == seen:
                self.fingerprint, self.refused = seen, None
                if rebuilt:
                    self.built = time.monotonic()
                return
            self._clear()  # Rows moved while they were read.
            self.fingerprint = None
        raise _Unusable

    def _advance(self, db, seen, aged):
        """True when every stored row was read again."""
        old = self.fingerprint
        if old is not None and not aged and seen[1] > old[1] and self._append(db, old, seen):
            return False
        self._rebuild(db)
        return True

    def _append(self, db, old, seen):
        extra = db.execute("SELECT COUNT(*) FROM character_emotion_appraisals WHERE rowid>?", (old[1],)).fetchone()[0]
        if seen[0] - extra != old[0]:
            return False
        if old[0]:
            kept = db.execute("SELECT version FROM character_emotion_appraisals WHERE rowid=?", (old[1],)).fetchone()
            if kept is None or kept[0] != old[2]:
                return False
        rows = db.execute(_ROWS + " WHERE a.rowid>? ORDER BY s.occurred_at,s.rowid", (old[1],)).fetchall()
        if rows and self.keys and (rows[0][2], rows[0][0]) <= self.keys[-1]:
            return False
        self._extend(rows)
        return True

    def _rebuild(self, db):
        rows = [tuple(row) for row in db.execute(_ROWS + " ORDER BY s.occurred_at,s.rowid")]
        keep = 0
        for keep, (new, old) in enumerate(zip(rows, self.idents)):
            if new != old:
                break
        else:
            keep = min(len(rows), len(self.idents))
        if keep < len(self.records):
            del self.idents[keep:], self.records[keep:], self.stamps[keep:], self.columns[keep:], self.keys[keep:]
            del self.verdicts[keep:]
            self.stale = [index for index in self.stale if index < keep]
            self.projection = _Projection()
            for record, stamp, accepted in zip(self.records, self.stamps, self.verdicts):
                if accepted:
                    self.projection.add(record, stamp)
        self._extend(rows[keep:])
        self.known = dict(zip(self.idents, self.records))

    def _extend(self, rows):
        for row in rows:
            order, identity, column, source_raw, raw, version, basis_raw = row = tuple(row)
            record = self.known.get(row) or _parse_row(source_raw, order, raw, version, basis_raw)
            if record["occurred_at"] != column:
                raise ValueError("EMOTION_STORED_EVIDENCE_INVALID")
            self._replay(record, datetime.fromisoformat(column))
            self.idents.append(row)
            self.columns.append(column)
            self.keys.append((column, order))

    def _replay(self, record, stamp):
        projection, dependencies = self.projection, record["_dependencies"]
        accepted = True
        if dependencies:
            target, changes = record["revises"], _concern_changes(record["concern"])
            accepted = not (
                (target and projection.live_version(target["source_id"]) != dependencies.get("revised_version"))
                or any(change["action"] == "resolve" and projection.concern_version(change["id"]) != (
                        dependencies.get("concern_versions", {}).get(change["id"]) if isinstance(record["concern"], list)
                        else dependencies.get("concern_version")) for change in changes)
                or ("read_context" in dependencies
                    and _hash(projection.context(record["occurred_at"])) != dependencies["read_context"]))
        if not accepted:
            self.stale.append(len(self.records))
        self.records.append(record)
        self.stamps.append(stamp)
        self.verdicts.append(accepted)
        if accepted:
            projection.add(record, stamp)

    def view(self, db, stamp, before, context_at):
        with self.lock:
            self.sync(db)
            end = bisect_right(self.columns, stamp)
            if before is not None:
                end = min(end, bisect_left(self.keys, before))
            projection = self.projection
            if end < len(self.records):
                projection = _Projection()
                for record, moment, accepted in zip(self.records[:end], self.stamps[:end], self.verdicts[:end]):
                    if accepted:
                        projection.add(record, moment)
            stale = {self.records[index]["source_id"] for index in self.stale[:bisect_left(self.stale, end)]}
            return _StateView(list(projection.live), list(projection.live_stamps), OrderedDict(projection.concerns),
                              stale, projection.context(context_at) if context_at is not None else None)


_LEDGERS = OrderedDict()
_LEDGERS_LOCK = threading.Lock()


def _ledger_for(db):
    try:
        path = db.execute("PRAGMA database_list").fetchone()[2]
    except (sqlite3.Error, IndexError, TypeError):
        return None
    if not path:
        return None  # In-memory data has no identity to share.
    with _LEDGERS_LOCK:
        ledger = _LEDGERS.pop(path, None) or _Ledger()
        _LEDGERS[path] = ledger
        while len(_LEDGERS) > _LEDGER_LIMIT:
            _LEDGERS.popitem(last=False)
        return ledger


class CharacterEmotionStore:
    """Source records and reversible appraisals in the per-user life database."""

    def __init__(self, path: Path, *, initialize: bool = True):
        self.path = Path(path)
        if not initialize:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS character_emotion_sources (
                    source_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS character_emotion_appraisals (
                    source_id TEXT PRIMARY KEY, payload TEXT NOT NULL, version TEXT NOT NULL, basis TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS character_emotion_appraisal_history (
                    source_id TEXT NOT NULL, version TEXT NOT NULL, payload TEXT NOT NULL, basis TEXT NOT NULL,
                    PRIMARY KEY (source_id,version));
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _record_source(db, source):
        old = db.execute("SELECT payload FROM character_emotion_sources WHERE source_id=?", (source["source_id"],)).fetchone()
        encoded = _json(source)
        if old:
            if old[0] != encoded:
                raise ValueError("EMOTION_SOURCE_CONFLICT")
            return False
        db.execute("INSERT INTO character_emotion_sources VALUES (?,?,?)", (source["source_id"], source["occurred_at"], encoded))
        return True

    def receive(self, source_id: str, text: str, *, occurred_at: datetime) -> bool:
        """Internal receipt adapter: the caller must first persist user input."""
        source = _source(source_id, "received_input", text, _time(occurred_at))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._record_source(db, source)

    def publish(self, source_id: str) -> bool:
        if not _id(source_id):
            raise ValueError("EMOTION_SOURCE_INVALID")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._record_source(db, _world_source(db, source_id))

    def pending_source_ids(self, *, before: datetime | None = None, limit: int = 8,
                           after: datetime | None = None, until: datetime | None = None) -> list[str]:
        """Oldest first; `after` (exclusive) and `until` (inclusive) bound the sources' times."""
        # The extra item is an overflow probe, never an expanded model batch.
        if type(limit) is not int or not 1 <= limit <= _BATCH_LIMIT + 1:
            raise ValueError("EMOTION_INPUT_INVALID")
        with self._db() as db:
            db.execute("BEGIN")
            stamp = _time(before) if before is not None else self._latest_time(db)
            stale = self._state_view(db, stamp).stale
            low = _time(after) if after is not None else ""
            high = min(stamp, _time(until)) if until is not None else stamp
            found = [(row[1], row[2], row[0]) for row in db.execute(
                "SELECT s.source_id,s.occurred_at,s.rowid FROM character_emotion_sources s "
                "WHERE s.occurred_at<=? AND s.occurred_at>? AND NOT EXISTS "
                "(SELECT 1 FROM character_emotion_appraisals a WHERE a.source_id=s.source_id) "
                "ORDER BY s.occurred_at,s.rowid LIMIT ?", (high, low, limit))]
            # Re-evaluation is durable: a stale completed row is pending again.
            for chunk in _chunks(sorted(stale)):
                found += [(row[1], row[2], row[0]) for row in db.execute(
                    "SELECT source_id,occurred_at,rowid FROM character_emotion_sources WHERE occurred_at<=? "
                    "AND occurred_at>? AND source_id IN (%s)" % ",".join("?" * len(chunk)), (high, low, *chunk))]
            return [item[2] for item in sorted(found)][:limit]

    @staticmethod
    def _latest_time(db):
        return db.execute("SELECT MAX(occurred_at) FROM character_emotion_sources").fetchone()[0] or _time(datetime.now(timezone.utc))

    @staticmethod
    def _records(db, stamp):
        return [_parse_row(source_raw, order, raw, version, basis_raw) for order, source_raw, raw, version, basis_raw in db.execute(
                "SELECT s.rowid,s.payload,a.payload,a.version,a.basis FROM character_emotion_appraisals a "
                "JOIN character_emotion_sources s ON s.source_id=a.source_id WHERE s.occurred_at<=? "
                "ORDER BY s.occurred_at,s.rowid", (stamp,))]

    @staticmethod
    def _key(item):
        return item["occurred_at"], item["source_order"]

    @staticmethod
    def _public(item):
        return {key: value for key, value in item.items() if key[0] != "_"}

    @staticmethod
    def _public_concern(concern):
        return {**concern, "source_ids": concern["source_ids"][-2 * _VIEW_LIMIT:],
                "source_count": len(concern["source_ids"])}

    @classmethod
    def _state_reference(cls, db, stamp, *, before=None):
        """The whole-list replay: the definition the ledger must agree with."""
        # ponytail: dependent transitions replay prefixes. Reads go through the
        # ledger; this stays the definition, and the answer for data it cannot model.
        accepted, stale = [], set()
        for item in cls._records(db, stamp):
            if before is not None and cls._key(item) >= before:
                continue
            dependencies = item["_dependencies"]
            if dependencies:
                live, concerns = cls._project(accepted)
                prior = {record["source_id"]: record for record in live}
                target = item["revises"]
                changes = _concern_changes(item["concern"])
                if ((target and (prior.get(target["source_id"], {}).get("version") != dependencies.get("revised_version")))
                        or any(concern["action"] == "resolve"
                            and concerns.get(concern["id"], {}).get("version") != (
                                dependencies.get("concern_versions", {}).get(concern['id'])
                                if isinstance(item['concern'], list) else dependencies.get("concern_version"))
                            for concern in changes)
                        or ("read_context" in dependencies
                            and _hash(cls._context(live, concerns, item["occurred_at"])) != dependencies["read_context"])):
                    stale.add(item["source_id"])
                    continue
            accepted.append(item)
        live, concerns = cls._project(accepted)
        return live, concerns, stale

    @classmethod
    def _state_view(cls, db, stamp, *, before=None, context_at=None):
        """Live records, concerns and stale sources up to stamp and before a key.

        The context an appraisal at `context_at` reads is computed with the state,
        because the shared ledger moves on as soon as the database does.
        """
        ledger = _ledger_for(db)
        if ledger is not None:
            try:
                return ledger.view(db, stamp, before, context_at)
            except _Unusable:
                pass
        live, concerns, stale = cls._state_reference(db, stamp, before=before)
        ordered = OrderedDict(sorted(concerns.items(), key=lambda pair: (pair[1]["occurred_at"], pair[1]["source_order"])))
        return _StateView(live, [datetime.fromisoformat(item["occurred_at"]) for item in live], ordered, stale,
                          cls._context(live, concerns, context_at) if context_at is not None else None)

    @classmethod
    def _state(cls, db, stamp, *, before=None):
        view = cls._state_view(db, stamp, before=before)
        return view.live, dict(view.concerns), view.stale

    @staticmethod
    def _project(records):
        # A withdrawal may itself be withdrawn. Commit-time references only point
        # to already-stored records, so walking reverse dependency order is acyclic.
        by_id = {item["source_id"]: item for item in records}
        incoming = {key: [] for key in by_id}
        for item in records:
            if item["revises"] and item["revises"]["source_id"] in incoming:
                incoming[item["revises"]["source_id"]].append(item["source_id"])
        active, visiting = {}, set()
        for key in by_id:
            stack = [(key, False)]
            while stack:
                current, expanded = stack.pop()
                if current in active:
                    continue
                if expanded:
                    active[current] = not any(active[other] for other in incoming[current])
                    visiting.remove(current)
                else:
                    if current in visiting:
                        raise ValueError("EMOTION_STORED_EVIDENCE_INVALID")
                    visiting.add(current)
                    stack.append((current, True))
                    stack.extend((other, False) for other in incoming[current])
        live = [item for item in records if active[item["source_id"]]]
        concerns = {}
        for item in live:
            for change in _concern_changes(item["concern"]):
                old = concerns.get(change["id"])
                contributors = list(old["source_ids"]) if old and old["status"] == "open" else []
                if change["action"] == "open":
                    contributors.append(item["source_id"])
                else:
                    contributors = []
                concerns[change["id"]] = dict(
                    id=change["id"], summary=change["summary"], status=change["action"],
                    source_ids=contributors, occurred_at=item["occurred_at"], source_order=item["source_order"],
                    version=_hash([old["version"] if old else None, item["version"]]))
        return live, concerns

    @staticmethod
    def _bounded_prior(live, concerns, stamp):
        recent = datetime.fromisoformat(stamp) - REACTION_WINDOW
        contributors = {key for concern in concerns.values() if concern["status"] == "open" for key in concern["source_ids"]}
        selected = [item for item in live if item["source_id"] in contributors or datetime.fromisoformat(item["occurred_at"]) >= recent]
        return [CharacterEmotionStore._public(item) for item in selected[-2 * _VIEW_LIMIT:]]

    @staticmethod
    def _context(live, concerns, stamp):
        return dict(as_of=stamp,
                    concerns=[CharacterEmotionStore._public_concern(c) for c in
                              sorted(concerns.values(), key=lambda c: (c["occurred_at"], c["source_order"]))[-2 * _VIEW_LIMIT:]],
                    prior_appraisals=CharacterEmotionStore._bounded_prior(live, concerns, stamp))

    def assessment(self, source_ids, *, now: datetime | None = None) -> dict:
        if (not isinstance(source_ids, (tuple, list)) or len(source_ids) > _BATCH_LIMIT
                or any(not _id(key) for key in source_ids)):
            raise ValueError("EMOTION_INPUT_INVALID")
        with self._db() as db:
            db.execute("BEGIN")
            stamp = _time(now) if now is not None else self._latest_time(db)
            state = self._state_view(db, stamp, context_at=stamp)
            sources = []
            for key in dict.fromkeys(source_ids):
                row = db.execute("SELECT rowid,payload FROM character_emotion_sources WHERE source_id=?", (key,)).fetchone()
                if row is None:
                    raise ValueError("EMOTION_SOURCE_UNAVAILABLE")
                source = _checked_source(row[1], key, row[0])
                if source["occurred_at"] > stamp:
                    raise ValueError("EMOTION_SOURCE_UNAVAILABLE")
                if key in state.stale or not db.execute("SELECT 1 FROM character_emotion_appraisals WHERE source_id=?", (key,)).fetchone():
                    sources.append(source)
            sources.sort(key=self._key)
            if sum(len(source["text"]) for source in sources) > 200_000:
                raise ValueError("EMOTION_INPUT_INVALID")
            contexts = {}
            for source in sources:
                contexts[source["source_id"]] = self._state_view(
                    db, source["occurred_at"], before=self._key(source), context_at=source["occurred_at"]).context
            value = dict(schema="olivia.character-emotion.input.v1", sources=sources,
                         source_contexts=contexts, **state.context)
            return {**value, "context_version": _hash(value)}

    def commit(self, assessment: dict, result: dict) -> bool:
        try:
            fields = {"schema", "as_of", "sources", "concerns", "prior_appraisals", "context_version", "source_contexts"}
            if (not isinstance(assessment, dict) or set(assessment) != fields
                    or assessment["schema"] != "olivia.character-emotion.input.v1"
                    or _hash({k: v for k, v in assessment.items() if k != "context_version"}) != assessment["context_version"]
                    or not isinstance(result, dict) or set(result) != {"appraisals"}
                    or not isinstance(result["appraisals"], list) or len(result["appraisals"]) > _BATCH_LIMIT
                    or not isinstance(assessment["sources"], list) or len(assessment["sources"]) > _BATCH_LIMIT):
                raise ValueError("EMOTION_APPRAISAL_INVALID")
            stamp = _time(datetime.fromisoformat(assessment["as_of"]))
            sources = {source["source_id"]: source for source in assessment["sources"]}
            items = {item["source_id"]: item for item in result["appraisals"]}
            if (len(sources) != len(assessment["sources"]) or len(items) != len(result["appraisals"])
                    or items.keys() != sources.keys() or set(assessment["source_contexts"]) != set(sources)):
                raise ValueError("EMOTION_APPRAISAL_INVALID")
            for key, item in items.items():
                _validate_item(item, sources[key])
                context = assessment["source_contexts"][key]
                if not isinstance(context, dict) or set(context) != {"as_of", "concerns", "prior_appraisals"}:
                    raise ValueError("EMOTION_APPRAISAL_INVALID")
        except (ValueError, KeyError, TypeError, OverflowError):
            raise ValueError("EMOTION_APPRAISAL_INVALID") from None
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            for key, source in sources.items():
                row = db.execute("SELECT rowid,payload FROM character_emotion_sources WHERE source_id=?", (key,)).fetchone()
                if row is None or _checked_source(row[1], key, row[0]) != source or source["occurred_at"] > stamp:
                    raise ValueError("EMOTION_CONTEXT_STALE")
                if source["source_kind"] == "published_world" and {**_world_source(db, key), "source_order": row[0]} != source:
                    raise ValueError("EMOTION_CONTEXT_STALE")
            # Check the frozen model input before inserting provisional batch
            # results. A concurrent earlier appraisal must not silently become
            # context the evaluator supposedly read.
            latest = max(stamp, self._latest_time(db))
            initially_stale = self._state_view(db, latest).stale
            for key, source in sources.items():
                old = db.execute("SELECT payload FROM character_emotion_appraisals WHERE source_id=?", (key,)).fetchone()
                if old and key not in initially_stale:
                    continue
                prior = self._state_view(db, source["occurred_at"], before=self._key(source), context_at=source["occurred_at"])
                if assessment["source_contexts"][key] != prior.context:
                    raise ValueError("EMOTION_CONTEXT_STALE")
            changed, batch_appraisals, batch_concerns = False, set(), set()
            for source in sorted(sources.values(), key=self._key):
                key, item = source["source_id"], items[source["source_id"]]
                latest = max(stamp, self._latest_time(db))
                newest = self._state_view(db, latest)
                old = db.execute("SELECT payload FROM character_emotion_appraisals WHERE source_id=?", (key,)).fetchone()
                if old and key not in newest.stale:
                    if old[0] != _json(item):
                        raise ValueError("EMOTION_APPRAISAL_INVALID")
                    batch_appraisals.add(key)
                    batch_concerns.update(change['id'] for change in _concern_changes(item['concern']))
                    continue
                historical = self._state_view(db, source["occurred_at"], before=self._key(source),
                                              context_at=source["occurred_at"])
                context = assessment["source_contexts"][key]
                before_concerns = {record["id"]: record for record in context["concerns"]}
                before_appraisals = {record["source_id"]: record for record in context["prior_appraisals"]}
                # Bind every interpretation to its source-time context, including
                # concern continuations. A later withdrawal cannot rewrite this
                # earlier prefix; an unseen late earlier appraisal can.
                dependencies = {"read_context": _hash(historical.context)}
                changes, revision = _concern_changes(item["concern"]), item["revises"]
                for change in changes:
                    prior, current = before_concerns.get(change["id"]), historical.concerns.get(change["id"])
                    if change["id"] in batch_concerns:
                        prior = self._public_concern(current) if current else None
                    if change["action"] == "resolve":
                        if prior != (self._public_concern(current) if current else None) or current != newest.concerns.get(change["id"]):
                            raise ValueError("EMOTION_CONTEXT_STALE")
                        if not prior or prior["status"] != "open":
                            raise ValueError("EMOTION_APPRAISAL_INVALID")
                        if isinstance(item['concern'], list):
                            dependencies.setdefault('concern_versions', {})[change['id']] = current['version']
                        else:
                            dependencies["concern_version"] = current["version"]
                    elif (bool(prior) != bool(current)
                          or not prior and change["id"] in newest.concerns):
                        # A newly received earlier item may extend a concern that already
                        # existed then, but cannot borrow one created in its future.
                        raise ValueError("EMOTION_CONTEXT_STALE")
                if revision:
                    target = revision["source_id"]
                    prior, current = before_appraisals.get(target), historical.public(target)
                    if target in batch_appraisals:
                        prior = current
                    if target in sources and self._key(sources[target]) >= self._key(source):
                        raise ValueError("EMOTION_CONTEXT_STALE")
                    if not prior:
                        code = "EMOTION_CONTEXT_STALE" if newest.record(target) else "EMOTION_APPRAISAL_INVALID"
                        raise ValueError(code)
                    if not current or prior != current or current != newest.public(target):
                        raise ValueError("EMOTION_CONTEXT_STALE")
                    dependencies["revised_version"] = current["version"]
                record = (key, _json(item), _hash({"source": source, "appraisal": item, "dependencies": dependencies}),
                          _json(dict(context_version=assessment["context_version"], as_of=stamp,
                                     source_hash=source["source_hash"], dependencies=dependencies)))
                db.execute("INSERT INTO character_emotion_appraisal_history SELECT source_id,version,payload,basis "
                           "FROM character_emotion_appraisals WHERE source_id=? ON CONFLICT DO NOTHING", (key,))
                db.execute("INSERT OR REPLACE INTO character_emotion_appraisals VALUES (?,?,?,?)", record)
                db.execute("INSERT OR IGNORE INTO character_emotion_appraisal_history VALUES (?,?,?,?)",
                           (record[0], record[2], record[1], record[3]))
                changed = True
                batch_appraisals.add(key)
                batch_concerns.update(change['id'] for change in changes)
            return changed

    def view(self, *, now: datetime) -> dict:
        stamp = _time(now)
        with self._db() as db:
            db.execute("BEGIN")
            state = self._state_view(db, stamp)
            recent = state.live[bisect_left(state.live_stamps, now - REACTION_WINDOW):]
            reactions = [item for item in recent if item["reaction"] != "none"][-_VIEW_LIMIT:]
            # Expiry removes attention only; the ledger does not claim resolution.
            open_concerns = active_concerns(state.concerns.values(), stamp, limit=_VIEW_LIMIT)
            open_concerns.reverse()
            # Only what the view shows needs its original wording.
            wanted = sorted({item["source_id"] for item in reactions}
                            | {key for concern in open_concerns for key in concern["source_ids"]})
            sources = {}
            for chunk in _chunks(wanted):
                sources.update((row[0], json.loads(row[1])) for row in db.execute(
                    "SELECT source_id,payload FROM character_emotion_sources WHERE source_id IN (%s)"
                    % ",".join("?" * len(chunk)), chunk))
        from .emotion_wording import presentation_text
        # Retain original evidence and hashes. Repair only the public wording of
        # old appraisals which accidentally selected the internal JSON envelope.
        shown = []
        for item in reactions:
            source = sources.get(item["source_id"])
            shown.append({**item, "quote": presentation_text(source, item["quote"]) if source else item["quote"]})
        concerns = []
        for concern in open_concerns:
            summary = concern["summary"]
            for source_id in concern["source_ids"]:
                source = sources.get(source_id)
                if source:
                    summary = presentation_text(source, summary, with_subject=True)
            concerns.append(self._public_concern({**concern, "summary": summary}))
        return dict(reaction_subject="character", interpretation_only=True,
                    reactions=[{k: item[k] for k in ("source_id", "occurred_at", "quote", "reaction", "goal_or_need", "action_tendency")}
                               for item in shown],
                    concerns=[{k: concern[k] for k in ("id", "summary", "source_ids", "source_count", "occurred_at")}
                              for concern in concerns],
                    reported_affects=[dict(source_id=item["source_id"], occurred_at=item["occurred_at"], **item["reported_affect"])
                                      for item in recent if item["reported_affect"] is not None][-_VIEW_LIMIT:])
