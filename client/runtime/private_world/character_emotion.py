"""Evidence-bound character appraisals; no world or relationship authority."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3


# A fictional continuity parameter, not a measured psychological recovery rate.
REACTION_WINDOW = timedelta(hours=6)
MAX_SOURCE_CHARS = 100_000
_BATCH_LIMIT = 32
_VIEW_LIMIT = 12
from .jev_emotion import REACTIONS as _REACTION_MEANINGS
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

    def pending_source_ids(self, *, before: datetime | None = None, limit: int = 8) -> list[str]:
        # The extra item is an overflow probe, never an expanded model batch.
        if type(limit) is not int or not 1 <= limit <= _BATCH_LIMIT + 1:
            raise ValueError("EMOTION_INPUT_INVALID")
        with self._db() as db:
            db.execute("BEGIN")
            stamp = _time(before) if before is not None else self._latest_time(db)
            _, _, stale = self._state(db, stamp)
            rows = db.execute("SELECT s.source_id,a.source_id FROM character_emotion_sources s LEFT JOIN character_emotion_appraisals a "
                              "ON a.source_id=s.source_id WHERE s.occurred_at<=? ORDER BY s.occurred_at,s.rowid", (stamp,))
            # Re-evaluation is durable: a stale completed row is pending again.
            return [row[0] for row in rows if row[1] is None or row[0] in stale][:limit]

    @staticmethod
    def _latest_time(db):
        return db.execute("SELECT MAX(occurred_at) FROM character_emotion_sources").fetchone()[0] or _time(datetime.now(timezone.utc))

    @staticmethod
    def _records(db, stamp):
        records = []
        for order, source_raw, raw, version, basis_raw in db.execute(
                "SELECT s.rowid,s.payload,a.payload,a.version,a.basis FROM character_emotion_appraisals a "
                "JOIN character_emotion_sources s ON s.source_id=a.source_id WHERE s.occurred_at<=? "
                "ORDER BY s.occurred_at,s.rowid", (stamp,)):
            source, item, basis = _checked_source(source_raw, order=order), json.loads(raw), json.loads(basis_raw)
            _validate_item(item, source)
            dependencies = basis.get("dependencies", {})
            if _hash({"source": source, "appraisal": item, "dependencies": dependencies}) != version:
                raise ValueError("EMOTION_STORED_EVIDENCE_INVALID")
            records.append({**item, "occurred_at": source["occurred_at"], "source_order": order,
                            "version": version, "_dependencies": dependencies})
        return records

    @staticmethod
    def _key(item):
        return item["occurred_at"], item["source_order"]

    @staticmethod
    def _public(item):
        return {key: value for key, value in item.items() if not key.startswith("_")}

    @staticmethod
    def _public_concern(concern):
        return {**concern, "source_ids": concern["source_ids"][-2 * _VIEW_LIMIT:],
                "source_count": len(concern["source_ids"])}

    @classmethod
    def _state(cls, db, stamp, *, before=None):
        # ponytail: dependent transitions replay prefixes; checkpoint if measured
        # histories make this quadratic worst case material. No model is called.
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
            live, concerns, stale = self._state(db, stamp)
            sources = []
            for key in dict.fromkeys(source_ids):
                row = db.execute("SELECT rowid,payload FROM character_emotion_sources WHERE source_id=?", (key,)).fetchone()
                if row is None:
                    raise ValueError("EMOTION_SOURCE_UNAVAILABLE")
                source = _checked_source(row[1], key, row[0])
                if source["occurred_at"] > stamp:
                    raise ValueError("EMOTION_SOURCE_UNAVAILABLE")
                if key in stale or not db.execute("SELECT 1 FROM character_emotion_appraisals WHERE source_id=?", (key,)).fetchone():
                    sources.append(source)
            sources.sort(key=self._key)
            if sum(len(source["text"]) for source in sources) > 200_000:
                raise ValueError("EMOTION_INPUT_INVALID")
            contexts = {}
            for source in sources:
                prior, previous, _ = self._state(db, source["occurred_at"], before=self._key(source))
                contexts[source["source_id"]] = self._context(prior, previous, source["occurred_at"])
            value = dict(schema="olivia.character-emotion.input.v1", sources=sources,
                         source_contexts=contexts, **self._context(live, concerns, stamp))
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
            _, _, initially_stale = self._state(db, latest)
            for key, source in sources.items():
                old = db.execute("SELECT payload FROM character_emotion_appraisals WHERE source_id=?", (key,)).fetchone()
                if old and key not in initially_stale:
                    continue
                prior, previous, _ = self._state(db, source["occurred_at"], before=self._key(source))
                if assessment["source_contexts"][key] != self._context(prior, previous, source["occurred_at"]):
                    raise ValueError("EMOTION_CONTEXT_STALE")
            changed, batch_appraisals, batch_concerns = False, set(), set()
            for source in sorted(sources.values(), key=self._key):
                key, item = source["source_id"], items[source["source_id"]]
                latest = max(stamp, self._latest_time(db))
                live, latest_concerns, stale = self._state(db, latest)
                old = db.execute("SELECT payload FROM character_emotion_appraisals WHERE source_id=?", (key,)).fetchone()
                if old and key not in stale:
                    if old[0] != _json(item):
                        raise ValueError("EMOTION_APPRAISAL_INVALID")
                    batch_appraisals.add(key)
                    batch_concerns.update(change['id'] for change in _concern_changes(item['concern']))
                    continue
                historical, concerns, _ = self._state(db, source["occurred_at"], before=self._key(source))
                active = {record["source_id"]: self._public(record) for record in historical}
                latest_active = {record["source_id"]: self._public(record) for record in live}
                context = assessment["source_contexts"][key]
                before_concerns = {record["id"]: record for record in context["concerns"]}
                before_appraisals = {record["source_id"]: record for record in context["prior_appraisals"]}
                # Bind every interpretation to its source-time context, including
                # concern continuations. A later withdrawal cannot rewrite this
                # earlier prefix; an unseen late earlier appraisal can.
                dependencies = {"read_context": _hash(self._context(historical, concerns, source["occurred_at"]))}
                changes, revision = _concern_changes(item["concern"]), item["revises"]
                for change in changes:
                    prior, current = before_concerns.get(change["id"]), concerns.get(change["id"])
                    if change["id"] in batch_concerns:
                        prior = self._public_concern(current) if current else None
                    if change["action"] == "resolve":
                        if prior != (self._public_concern(current) if current else None) or current != latest_concerns.get(change["id"]):
                            raise ValueError("EMOTION_CONTEXT_STALE")
                        if not prior or prior["status"] != "open":
                            raise ValueError("EMOTION_APPRAISAL_INVALID")
                        if isinstance(item['concern'], list):
                            dependencies.setdefault('concern_versions', {})[change['id']] = current['version']
                        else:
                            dependencies["concern_version"] = current["version"]
                    elif (bool(prior) != bool(current)
                          or not prior and change["id"] in latest_concerns):
                        # A newly received earlier item may extend a concern that already
                        # existed then, but cannot borrow one created in its future.
                        raise ValueError("EMOTION_CONTEXT_STALE")
                if revision:
                    target = revision["source_id"]
                    prior, current = before_appraisals.get(target), active.get(target)
                    if target in batch_appraisals:
                        prior = current
                    if target in sources and self._key(sources[target]) >= self._key(source):
                        raise ValueError("EMOTION_CONTEXT_STALE")
                    if not prior:
                        code = "EMOTION_CONTEXT_STALE" if target in latest_active else "EMOTION_APPRAISAL_INVALID"
                        raise ValueError(code)
                    if not current or prior != current or current != latest_active.get(target):
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
            live, concerns, _ = self._state(db, stamp)
            sources = {row[0]: json.loads(row[1]) for row in db.execute(
                'SELECT source_id,payload FROM character_emotion_sources WHERE source_id IN (' +
                ','.join('?' for _ in live) + ')', [item['source_id'] for item in live])} if live else {}
        from .emotion_wording import presentation_text
        # Retain original evidence and hashes. Repair only the public wording of
        # old appraisals which accidentally selected the internal JSON envelope.
        for item in live:
            source = sources.get(item['source_id'])
            if source:
                item['quote'] = presentation_text(source, item['quote'])
        for concern in concerns.values():
            for source_id in concern['source_ids']:
                source = sources.get(source_id)
                if source:
                    concern['summary'] = presentation_text(source, concern['summary'], with_subject=True)
        recent = [item for item in live if now - datetime.fromisoformat(item["occurred_at"]) <= REACTION_WINDOW]
        return dict(reaction_subject="character", interpretation_only=True,
                    reactions=[{k: item[k] for k in ("source_id", "occurred_at", "quote", "reaction", "goal_or_need", "action_tendency")}
                               for item in recent if item["reaction"] != "none"][-_VIEW_LIMIT:],
                    concerns=[{k: self._public_concern(concern)[k] for k in ("id", "summary", "source_ids", "source_count", "occurred_at")}
                              for concern in sorted(concerns.values(), key=lambda c: (c["occurred_at"], c["source_order"]))
                              if concern["status"] == "open"][-_VIEW_LIMIT:],
                    reported_affects=[dict(source_id=item["source_id"], occurred_at=item["occurred_at"], **item["reported_affect"])
                                      for item in recent if item["reported_affect"] is not None][-_VIEW_LIMIT:])
