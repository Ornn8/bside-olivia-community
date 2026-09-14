"""Local original-text retrieval. FTS candidates never require a chat model."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import re
import sqlite3
from pathlib import Path

from .conversation_memory_port import ConversationMemoryRecord


def terms(text):
    # Chinese bigrams cover short names that FTS5 trigram cannot match.
    words = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", text.lower())
    return list(dict.fromkeys(token for word in words for token in
        ([word] if word.isascii() or len(word) == 1 else
         [word[i:i + 2] for i in range(len(word) - 1)])))


class SourceRetrieval:
    def __init__(self, path: Path):
        self.path = path

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=2)
        db.execute("CREATE TABLE IF NOT EXISTS originals (user TEXT, source TEXT, actor TEXT, stamp TEXT, text TEXT, PRIMARY KEY(user,source,actor))")
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(user UNINDEXED, source UNINDEXED, actor UNINDEXED, stamp UNINDEXED, start UNINDEXED, text UNINDEXED, tokens)")
        db.execute("CREATE TABLE IF NOT EXISTS forgotten (user TEXT, source TEXT, PRIMARY KEY(user,source))")
        return db

    def put(self, user, source, user_text, reply_text, stamp):
        with closing(self.connect()) as db, db:
            if db.execute("SELECT 1 FROM forgotten WHERE user=? AND source=?", (user, source)).fetchone():
                return
            for actor, text in (("user", user_text), ("linli", reply_text)):
                previous = db.execute("SELECT text FROM originals WHERE user=? AND source=? AND actor=?", (user, source, actor)).fetchone()
                if previous and previous[0] == text:
                    continue
                db.execute("DELETE FROM chunks WHERE user=? AND source=? AND actor=?", (user, source, actor))
                db.execute("INSERT OR REPLACE INTO originals VALUES (?,?,?,?,?)", (user, source, actor, stamp.isoformat(), text))
                # Overlap preserves sentence context; offsets retain exact provenance.
                for start in range(0, len(text), 280):
                    chunk = text[start:start + 360]
                    db.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?)", (user, source, actor, stamp.isoformat(), start, chunk, " ".join(terms(chunk))))

    def forget(self, user, source=None):
        with closing(self.connect()) as db, db:
            sources = [source] if source is not None else [r[0] for r in db.execute("SELECT DISTINCT source FROM originals WHERE user=?", (user,))]
            for item in sources:
                db.execute("INSERT OR IGNORE INTO forgotten VALUES (?,?)", (user, item))
                db.execute("DELETE FROM originals WHERE user=? AND source=?", (user, item))
                db.execute("DELETE FROM chunks WHERE user=? AND source=?", (user, item))

    def search(self, query, user, semantic=(), limit=5, exclude_source_ids=()):
        excluded = set(exclude_source_ids)
        query_terms = terms(query[:2000])[:64]
        with closing(self.connect()) as db:
            sparse = []
            if query_terms:
                expression = " OR ".join('"' + word + '"' for word in query_terms)
                sparse = db.execute("SELECT source,actor,stamp,start,text FROM chunks WHERE tokens MATCH ? AND user=? ORDER BY bm25(chunks),rowid LIMIT 20", (expression, user)).fetchall()
            candidates, scores = {}, {}
            for rank, row in enumerate(sparse):
                if row[0] in excluded:
                    continue
                key = (row[0], row[1], row[3])
                candidates[key] = row
                scores[key] = 1 / (60 + rank + 1)
            fallback = []
            for rank, record in enumerate(semantic[:20]):
                if record.user_id != user or record.source_id in excluded:
                    continue
                if db.execute("SELECT 1 FROM forgotten WHERE user=? AND source=?", (user, record.source_id)).fetchone():
                    continue
                rows = db.execute("SELECT source,actor,stamp,start,text FROM chunks WHERE user=? AND source=? ORDER BY rowid", (user, record.source_id)).fetchall()
                if not rows:
                    fallback.append(record)
                    continue
                # Prefer the original paragraph related to the matching fact/query.
                evidence_terms = set(query_terms) | set(terms(record.text))
                rows.sort(key=lambda row: -len(evidence_terms.intersection(terms(row[4]))))
                for row in rows[:2]:
                    key = (row[0], row[1], row[3])
                    candidates[key] = row
                    scores[key] = scores.get(key, 0) + 1 / (60 + rank + 1)
            selected, seen_text, spans = [], set(), []
            proactive = {record.source_id for record in semantic if record.metadata.get("origin") == "proactive"}
            for key in sorted(scores, key=lambda key: -scores[key]):
                source, actor, stamp, start, text = candidates[key]
                start = int(start)
                if text in seen_text or any(s == source and a == actor and abs(start - offset) < 360 for s, a, offset in spans):
                    continue
                seen_text.add(text)
                spans.append((source, actor, start))
                digest = hashlib.sha256(f"{user}:{source}:{actor}:{start}".encode()).hexdigest()
                selected.append(ConversationMemoryRecord(memory_id="original:" + digest, text=text, user_id=user,
                    source_id=source, score=min(1, scores[key]), occurred_at=datetime.fromisoformat(stamp),
                    metadata={"verbatim": True, "speaker": actor, "start": start, "canonical": True,
                              **({"history_actor": actor} if source.startswith("history:") else {}),
                              **({"origin": "proactive"} if source in proactive else {})}))
                if len(selected) >= min(5, limit):
                    break
            # Facts without surviving originals remain explicitly summaries.
            for record in fallback:
                if len(selected) >= min(5, limit):
                    break
                if record.text not in seen_text and record.source_id not in {r.source_id for r in selected}:
                    selected.append(record)
                    seen_text.add(record.text)
            return tuple(selected)
