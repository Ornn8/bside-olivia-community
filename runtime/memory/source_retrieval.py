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
        db.execute("CREATE TABLE IF NOT EXISTS archive_targets (user TEXT, source TEXT, PRIMARY KEY(user,source))")
        db.execute("CREATE TABLE IF NOT EXISTS archive_scan (user TEXT PRIMARY KEY, stamp TEXT)")
        return db

    def register_archive(self, user, sources):
        from datetime import timezone
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM archive_targets WHERE user=?", (user,))
            db.executemany("INSERT OR IGNORE INTO archive_targets VALUES (?,?)", ((user, source) for source in sources))
            db.execute("INSERT OR REPLACE INTO archive_scan VALUES (?,?)", (user, datetime.now(timezone.utc).isoformat()))

    def browse(self, user, query, limit):
        with closing(self.connect()) as db:
            indexed = db.execute("SELECT COUNT(DISTINCT source) FROM originals WHERE user=?", (user,)).fetchone()[0]
            scanned = db.execute("SELECT stamp FROM archive_scan WHERE user=?", (user,)).fetchone()
            total, ready, removed = db.execute("""SELECT COUNT(*),
                COALESCE(SUM(EXISTS(SELECT 1 FROM originals o WHERE o.user=t.user AND o.source=t.source)),0),
                COALESCE(SUM(EXISTS(SELECT 1 FROM forgotten f WHERE f.user=t.user AND f.source=t.source)),0)
                FROM archive_targets t WHERE user=?""", (user,)).fetchone()
            rows = [] if query else db.execute("SELECT source,actor,stamp,text FROM originals WHERE user=? ORDER BY stamp DESC,source,actor LIMIT ?", (user, limit)).fetchall()
        if query:
            records = self.search(query, user, limit=limit)
            rows = [(r.source_id, r.metadata['speaker'], r.occurred_at.isoformat() if r.occurred_at else None, r.text) for r in records]
        return {'indexed_letters': indexed, 'archive_total': total if scanned else None,
                'archive_indexed': ready if scanned else None, 'archive_removed': removed if scanned else None,
                'scanned_at': scanned[0] if scanned else None,
                'originals': [{'source_id': s, 'speaker': a, 'created_at': stamp, 'text': text[:4000],
                               'excerpt': bool(query) or len(text)>4000} for s,a,stamp,text in rows]}

    def put(self, user, source, user_text, reply_text, stamp):
        stamp = stamp.isoformat() if stamp is not None else None
        with closing(self.connect()) as db, db:
            if db.execute("SELECT 1 FROM forgotten WHERE user=? AND source=?", (user, source)).fetchone():
                return
            for actor, text in (("user", user_text), ("linli", reply_text)):
                previous = db.execute("SELECT text,stamp FROM originals WHERE user=? AND source=? AND actor=?", (user, source, actor)).fetchone()
                if previous == (text, stamp):
                    continue
                db.execute("DELETE FROM chunks WHERE user=? AND source=? AND actor=?", (user, source, actor))
                db.execute("INSERT OR REPLACE INTO originals VALUES (?,?,?,?,?)", (user, source, actor, stamp, text))
                # Overlap preserves sentence context; offsets retain exact provenance.
                for start in range(0, len(text), 280):
                    chunk = text[start:start + 360]
                    db.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?)", (user, source, actor, stamp, start, chunk, " ".join(terms(chunk))))

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
            # Keep both sides of a matching exchange. A user's recollection
            # alone is not evidence of what the assistant previously replied.
            paired_sources = set()
            for rank, row in enumerate(sparse[:5]):
                source, actor, _, _, _ = row
                if source in excluded or source in paired_sources:
                    continue
                paired_sources.add(source)
                counterparts = db.execute(
                    "SELECT source,actor,stamp,start,text FROM chunks WHERE user=? AND source=? AND actor!=? ORDER BY rowid",
                    (user, source, actor),
                ).fetchall()
                counterparts.sort(key=lambda r: -len(set(query_terms).intersection(terms(r[4]))))
                if counterparts and set(query_terms).intersection(terms(counterparts[0][4])):
                    counterpart = counterparts[0]
                    key = (counterpart[0], counterpart[1], counterpart[3])
                    candidates[key] = counterpart
                    scores[key] = max(scores.get(key, 0), 1 / (60 + rank + 1.5))
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
                # Bound original excerpts before prompt rendering; otherwise a
                # long first side consumes the budget and drops its reply.
                if len(text) > 160:
                    pieces = list(re.finditer(r'[^。！？\n\r\u2028\u2029]+[。！？\n\r\u2028\u2029]*', text))
                    if pieces:
                        best = max(pieces, key=lambda p: len(set(query_terms).intersection(terms(p.group()))))
                        offset = best.start()
                        if len(best.group()) > 160:
                            hits = [best.group().find(term) for term in query_terms if term in best.group()]
                            offset += max(0, min(hits, default=0) - 40)
                        end = min(len(text), max(best.end(), offset + 80), offset + 160)
                        text = text[offset:end]
                        start += offset
                digest = hashlib.sha256(f"{user}:{source}:{actor}:{start}".encode()).hexdigest()
                selected.append(ConversationMemoryRecord(memory_id="original:" + digest, text=text, user_id=user,
                    source_id=source, score=min(1, scores[key]), occurred_at=datetime.fromisoformat(stamp) if stamp else None,
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
