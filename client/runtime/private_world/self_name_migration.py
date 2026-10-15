"""One-off repair: her own words stored with the user's self-address 俺 say 我.

Until the 2.4.3 era some replies took over a user's dialect self-address (俺). The cloud now
returns her replies with 我, but items already extracted from those replies kept 俺, and the
world page showed them as her promises. This rewrites 俺 to 我 only in text that provably came
from her reply:

- exchange journal updates whose actor is her (their quote is checked against her reply when
  stored), their detail, and their title when the title was cut from that same reply;
- the materialised project rows of those items;
- her current-activity statements taken from a reply (source reply:...);
- her reply text kept for the world gate;
- the character_quote of development evidence from a reply.

Never touched: anything the user said (actor user, user quotes, user text tables), text that
marks the user's words (用户说/你说) or a 俺 inside quotation marks, relationship and boundary
quotes (they are re-checked against the original reply), published daily life (its payload is
hashed into emotion and development evidence), and the chat and memory originals, which stay
verbatim. Runs once per install (marker in life_settings) and keeps every original it changes
in life_migration_backup. Timing records are re-keyed so a changed item keeps its time scope.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

MIGRATION = "her-self-name-20261016"
_MARKER = "migration:" + MIGRATION
_USER_ADDRESS, _HER_ADDRESS = "俺", "我"   # 俺 -> 我
_USER_WORDS = ("用户说", "你说")  # 用户说, 你说
_QUOTES = {"“": "”", "「": "」", "『": "』", "‘": "’", '"': '"'}

log = logging.getLogger(__name__)


def her_words(text):
    """Her own wording with 我; text marking the user's words, and quoted spans, stay as they are."""
    if not isinstance(text, str) or _USER_ADDRESS not in text or any(mark in text for mark in _USER_WORDS):
        return text
    result, closing = [], []
    for char in text:
        if closing and char == closing[-1]:
            closing.pop()
        elif char in _QUOTES:
            closing.append(_QUOTES[char])
        elif char == _USER_ADDRESS and not closing:
            char = _HER_ADDRESS
        result.append(char)
    return "".join(result)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _from_reply(item):
    """An exchange update she made: the quote was checked against her reply when stored."""
    return (isinstance(item, dict) and item.get("actor") == "linli" and isinstance(item.get("quote"), str)
            and str(item.get("source_id", "")).startswith("reply:"))


def _title_from_reply(item):
    title = item.get("title")
    return isinstance(title, str) and bool(title) and any(
        isinstance(item.get(field), str) and title in item[field] for field in ("quote", "detail"))


def _her_item(item, her_titles):
    """Rewrite her fields of one stored item; returns the new item or None when unchanged."""
    changed = dict(item)
    if _from_reply(item):
        for field in ("quote", "detail"):
            changed[field] = her_words(item.get(field))
    if item.get("id") in her_titles and item.get("title") == her_titles[item["id"]]:
        changed["title"] = her_words(item["title"])
    return changed if changed != item else None


def _backup(db, table, key, original):
    db.execute("INSERT OR IGNORE INTO life_migration_backup VALUES (?,?,?,?)", (MIGRATION, table, key, original))


def _retime(db, old, new):
    """A changed item keeps the time scope recorded for its previous wording."""
    from .project_timing import source_version, version
    rows = db.execute("SELECT payload FROM life_project_timing WHERE version IN (?,?) "
                      "OR json_extract(payload,'$.source_version')=?",
                      (version(old), source_version(old), source_version(old))).fetchall()
    for (raw,) in rows:
        timing = json.loads(raw)
        timing.update(version=version(new), source_version=source_version(new))
        db.execute("INSERT OR IGNORE INTO life_project_timing VALUES (?,?)",
                   (timing["version"], json.dumps(timing, ensure_ascii=False)))


def migrate(db, *, now=None):
    """Run the repair inside an open transaction once; returns the number of rows changed."""
    db.execute("CREATE TABLE IF NOT EXISTS life_migration_backup (migration TEXT NOT NULL, table_name TEXT NOT NULL, "
               "row_key TEXT NOT NULL, original TEXT NOT NULL, PRIMARY KEY(migration, table_name, row_key))")
    if db.execute("SELECT 1 FROM life_settings WHERE key=?", (_MARKER,)).fetchone():
        return 0
    pattern = "%" + _USER_ADDRESS + "%"
    exchanges = [(source_id, json.loads(raw)) for source_id, raw in db.execute(
        "SELECT source_id,payload FROM life_moments WHERE kind='exchange' AND source_id LIKE 'reply:%' "
        "AND payload LIKE ? ORDER BY occurred_at, rowid", (pattern,))]
    her_titles = {}
    for _, payload in exchanges:
        for item in payload.get("updates") or []:
            if (_from_reply(item) and _USER_ADDRESS in str(item.get("title", ""))
                    and _title_from_reply(item) and item.get("id") not in her_titles):
                her_titles[item["id"]] = item["title"]
    changed = 0
    for source_id, payload in exchanges:
        updated, items = dict(payload), []
        for item in payload.get("updates") or []:
            new = _her_item(item, her_titles) if isinstance(item, dict) else None
            if new is not None:
                _retime(db, item, new)
            items.append(new or item)
        updated["updates"] = items
        current = payload.get("current")
        if isinstance(current, dict) and str(current.get("source_id", "")).startswith("reply:"):
            updated["current"] = {**current, "note": her_words(current.get("note"))}
        if updated != payload:
            raw = db.execute("SELECT payload FROM life_moments WHERE source_id=?", (source_id,)).fetchone()[0]
            _backup(db, "life_moments", source_id, raw)
            db.execute("UPDATE life_moments SET payload=? WHERE source_id=?", (_json(updated), source_id))
            changed += 1
    for project_id, raw in db.execute("SELECT id,payload FROM life_projects WHERE payload LIKE ?", (pattern,)).fetchall():
        item = json.loads(raw)
        new = _her_item(item, her_titles)
        if new is not None:
            _retime(db, item, new)
            _backup(db, "life_projects", project_id, raw)
            db.execute("UPDATE life_projects SET payload=? WHERE id=?", (_json(new), project_id))
            changed += 1
    row = db.execute("SELECT payload FROM life_current WHERE id=1 AND payload LIKE ?", (pattern,)).fetchone()
    if row:
        current = json.loads(row[0])
        if str(current.get("source_id", "")).startswith("reply:") and her_words(current.get("note")) != current.get("note"):
            _backup(db, "life_current", "1", row[0])
            db.execute("UPDATE life_current SET payload=? WHERE id=1", (_json({**current, "note": her_words(current["note"])}),))
            changed += 1
    for source_id, text in db.execute("SELECT source_id,reply_text FROM life_exchange_world_gate "
                                      "WHERE source_id LIKE 'reply:%' AND reply_text LIKE ?", (pattern,)).fetchall():
        if her_words(text) != text:
            _backup(db, "life_exchange_world_gate", source_id, text)
            db.execute("UPDATE life_exchange_world_gate SET reply_text=? WHERE source_id=?", (her_words(text), source_id))
            changed += 1
    for source_id, topic, raw in db.execute("SELECT source_id,topic,payload FROM character_development_events "
                                            "WHERE source_id LIKE 'reply:%' AND payload LIKE ?", (pattern,)).fetchall():
        event = json.loads(raw)
        quote = her_words(event.get("character_quote"))
        if quote != event.get("character_quote"):
            _backup(db, "character_development_events", source_id + "\x00" + topic, raw)
            db.execute("UPDATE character_development_events SET payload=? WHERE source_id=? AND topic=?",
                       (json.dumps({**event, "character_quote": quote}, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False), source_id, topic))
            changed += 1
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    db.execute("INSERT INTO life_settings VALUES (?,?)", (_MARKER, _json({"changed": changed, "at": stamp})))
    return changed


def run_once(store):
    """Never blocks startup: a failure leaves the marker unset, so the next start retries."""
    try:
        with store._db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = migrate(db)
        if changed:
            log.info("her self-name repair changed %d stored rows", changed)
        return changed
    except Exception as exc:  # noqa: BLE001 - optional repair, never fatal
        log.warning("her self-name repair skipped: %s", type(exc).__name__)
        return None
