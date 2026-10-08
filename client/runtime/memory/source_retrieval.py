"""Local original-text retrieval. FTS candidates never require a chat model."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from .conversation_memory_port import ConversationMemoryRecord


@dataclass(frozen=True)
class SourceDependencies:
    records: tuple[ConversationMemoryRecord, ...] = ()
    relations: tuple[dict, ...] = ()
    blocked_source_ids: tuple[str, ...] = ()


def _aware_time(value):
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
        raise ValueError('DEPENDENCY_TIME_UNKNOWN')
    return stamp


def _digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


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
        db.execute('CREATE TABLE IF NOT EXISTS source_dependencies '
                   '(user TEXT, dependency_id TEXT, payload TEXT NOT NULL, retracted INTEGER NOT NULL DEFAULT 0, '
                   'PRIMARY KEY(user,dependency_id))')
        db.execute('CREATE TABLE IF NOT EXISTS source_vectors '
                   '(user TEXT, source TEXT, model TEXT, digest TEXT, vector BLOB, PRIMARY KEY(user,source,model))')
        db.execute('CREATE TABLE IF NOT EXISTS source_aliases '
                   '(user TEXT, exchange_source TEXT, receipt_source TEXT, PRIMARY KEY(user,exchange_source,receipt_source))')
        db.execute('CREATE TABLE IF NOT EXISTS relationship_quotes '
                   '(user TEXT, source TEXT, digest TEXT, attempts INTEGER NOT NULL DEFAULT 0, '
                   'categories TEXT, PRIMARY KEY(user,source))')
        db.execute('CREATE INDEX IF NOT EXISTS relationship_pending ON relationship_quotes(user,attempts) '
                   'WHERE categories IS NULL')
        db.execute("CREATE INDEX IF NOT EXISTS relationship_kept ON relationship_quotes(user) WHERE categories!='[]'")
        return db

    @staticmethod
    def _aliases(db, user, sources):
        found = set(sources)
        if not found:
            return found
        pairs = db.execute('SELECT exchange_source,receipt_source FROM source_aliases WHERE user=?', (user,)).fetchall()
        while True:
            expanded = found | {node for a, b in pairs if a in found or b in found for node in (a, b)}
            if expanded == found:
                return found
            found = expanded

    @classmethod
    def _forget_sources(cls, db, user, sources):
        for source in cls._aliases(db, user, sources):
            db.execute('INSERT OR IGNORE INTO forgotten VALUES (?,?)', (user, source))
            db.execute('DELETE FROM originals WHERE user=? AND source=?', (user, source))
            db.execute('DELETE FROM chunks WHERE user=? AND source=?', (user, source))
            db.execute('DELETE FROM relationship_quotes WHERE user=? AND source=?', (user, source))

    @classmethod
    def _lineage(cls, db, user, source):
        aliases = cls._aliases(db, user, [source]) - {source}
        return {**({'source_aliases': json.dumps(sorted(aliases))} if aliases else {}),
                **({'evidence_kind': 'received_user_statement'} if source.startswith('received-user:') else {})}

    @classmethod
    def _alias_received(cls, db, user, exchange_source, receipt_sources):
        for receipt in receipt_sources:
            db.execute('INSERT OR IGNORE INTO source_aliases VALUES (?,?,?)', (user, exchange_source, receipt))
        family = cls._aliases(db, user, [exchange_source])
        if any(db.execute('SELECT 1 FROM forgotten WHERE user=? AND source=?', (user, s)).fetchone() for s in family):
            cls._forget_sources(db, user, family)

    def alias_received(self, user, exchange_source, receipt_sources):
        receipts = tuple(dict.fromkeys(receipt_sources))
        if (not isinstance(exchange_source, str) or not exchange_source.startswith('reply:') or not receipts
                or any(not isinstance(s, str) or not s.startswith('received-user:') for s in receipts)):
            return False
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if any(not db.execute('SELECT 1 FROM originals WHERE user=? AND source=? AND actor=?', (user, s, 'user')).fetchone()
                   and not db.execute('SELECT 1 FROM forgotten WHERE user=? AND source=?', (user, s)).fetchone() for s in receipts):
                return False
            self._alias_received(db, user, exchange_source, receipts)
            return True

    def put_received(self, user, source, text, stamp, *, exchange_source=None):
        """Index durable received user speech without implying assistant delivery."""
        if (not isinstance(source, str) or not source.startswith('received-user:')
                or not isinstance(text, str) or not text.strip()
                or exchange_source is not None and (not isinstance(exchange_source, str) or not exchange_source.startswith('reply:'))):
            return False
        try:
            stamp = _aware_time(stamp).isoformat()
        except (TypeError, ValueError):
            return False
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT text FROM originals WHERE user=? AND source=? AND actor=?', (user, source, 'user')).fetchone()
            if old and old[0] != text:
                return False
            if exchange_source is not None:
                self._alias_received(db, user, exchange_source, [source])
            if db.execute('SELECT 1 FROM forgotten WHERE user=? AND source=?', (user, source)).fetchone():
                return False
            if old:
                return True
            db.execute('INSERT INTO originals VALUES (?,?,?,?,?)', (user, source, 'user', stamp, text))
            for start in range(0, len(text), 280):
                chunk = text[start:start + 360]
                db.execute('INSERT INTO chunks VALUES (?,?,?,?,?,?,?)', (user, source, 'user', stamp, start, chunk, ' '.join(terms(chunk))))
            return True

    @staticmethod
    def _dependency_original(db, user, source, actor):
        if db.execute('SELECT 1 FROM forgotten WHERE user=? AND source=?', (user, source)).fetchone():
            return None
        return db.execute('SELECT text,stamp FROM originals WHERE user=? AND source=? AND actor=?',
                          (user, source, actor)).fetchone()

    def save_dependency(self, user, earlier_source, later_source, earlier_actor, later_actor,
                        earlier_quote, later_quote, kind):
        """Store a proposed recall dependency, never a verified fact or permission."""
        if (kind not in {'correction', 'state_change', 'challenge'} or earlier_source == later_source
                or any(actor not in {'user', 'linli'} for actor in (earlier_actor, later_actor))
                or any(not isinstance(q, str) or not q.strip() or not 2 <= len(q) <= 500
                       for q in (earlier_quote, later_quote))):
            return False
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            payload = {'kind': kind}
            times = []
            for prefix, source, actor, quote in (('earlier', earlier_source, earlier_actor, earlier_quote),
                                                ('later', later_source, later_actor, later_quote)):
                original = self._dependency_original(db, user, source, actor)
                if original is None or quote not in original[0]:
                    return False
                try:
                    times.append(_aware_time(original[1]))
                except (ValueError, TypeError):
                    return False
                start = original[0].index(quote)
                payload.update({prefix + '_source': source, prefix + '_actor': actor,
                                prefix + '_start': start, prefix + '_end': start + len(quote),
                                prefix + '_hash': _digest(original[0]), prefix + '_stamp': original[1]})
            if times[1] < times[0]:
                return False
            encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            identity = _digest(json.dumps([earlier_source, later_source, earlier_actor, later_actor,
                                          earlier_quote, later_quote, kind], ensure_ascii=False))
            existing = db.execute('SELECT retracted,payload FROM source_dependencies WHERE user=? AND dependency_id=?',
                                  (user, identity)).fetchone()
            if existing:
                return not existing[0] and existing[1] == encoded
            db.execute('INSERT INTO source_dependencies(user,dependency_id,payload) VALUES (?,?,?)',
                       (user, identity, encoded))
            return True

    def retract_dependency(self, user, dependency_id):
        with closing(self.connect()) as db, db:
            return bool(db.execute('UPDATE source_dependencies SET retracted=1 WHERE user=? AND dependency_id=? AND retracted=0',
                                   (user, dependency_id)).rowcount)

    def dependencies(self, user, source_ids, exclude_source_ids=(), before=None):
        """Read complete directed dependency groups, or block an incomplete seed.

        Errors propagate: a caller must not treat an unavailable lookup as no edges.
        Bounds count whole sources/edges per seed, including same-time cycles.
        """
        cutoff = _aware_time(before) if before is not None else None
        excluded = set(exclude_source_ids)
        records, relations, blocked = {}, {}, []
        with closing(self.connect()) as db:
            db.execute('BEGIN')
            edges = {}
            for identity, raw in db.execute('SELECT dependency_id,payload FROM source_dependencies WHERE user=? AND retracted=0', (user,)):
                value = json.loads(raw)
                edges.setdefault(value['earlier_source'], []).append((identity, value))
            for seed in dict.fromkeys(source_ids):
                pending, seen, group, links = [seed], set(), {}, {}
                invalid = False
                while pending and not invalid:
                    source = pending.pop()
                    if source in seen:
                        continue
                    seen.add(source)
                    unrelated_seed = source == seed and source not in edges and not links
                    original = self._source_records(db, user, source, {'retrieval_route': 'dependency'},
                                                    unknown_time=unrelated_seed) if source not in excluded else ()
                    if (source == seed and not original and source not in edges and source not in excluded
                            and not db.execute('SELECT 1 FROM forgotten WHERE user=? AND source=?', (user, source)).fetchone()):
                        continue  # An unindexed archive source has no dependency obligation yet.
                    if (len(seen) > 32 or not original
                            or cutoff is not None and any(
                                (r.occurred_at is None and not unrelated_seed)
                                or r.occurred_at is not None and _aware_time(r.occurred_at) > cutoff
                                for r in original)):
                        invalid = True
                        break
                    group.update((r.memory_id, r) for r in original)
                    # Existing aliases expose receipt dependencies even when the
                    # search hit is its delivered exchange. A future/unwritten
                    # exchange alias is not evidence and creates no obligation.
                    for alias in self._aliases(db, user, [source]) - seen:
                        stamps = db.execute('SELECT stamp FROM originals WHERE user=? AND source=?', (user, alias)).fetchall()
                        if not stamps:
                            continue
                        if cutoff is not None:
                            try:
                                if any(_aware_time(stamp) > cutoff for (stamp,) in stamps):
                                    continue
                            except (ValueError, TypeError):
                                if alias in edges:
                                    invalid = True
                                    break
                                continue
                        pending.append(alias)
                    for identity, edge in edges.get(source, ()):
                        for prefix in ('earlier', 'later'):
                            current = self._dependency_original(db, user, edge[prefix + '_source'], edge[prefix + '_actor'])
                            if (current is None or current[1] != edge[prefix + '_stamp']
                                    or _digest(current[0]) != edge[prefix + '_hash']):
                                invalid = True
                                break
                        if invalid:
                            break
                        links[identity] = {'dependency_id': identity, **{k: v for k, v in edge.items()
                                                                      if not k.endswith(('_hash', '_stamp'))}}
                        if len(links) > 64:
                            invalid = True
                            break
                        pending.append(edge['later_source'])
                if invalid:
                    blocked.append(seed)
                else:
                    records.update(group)
                    relations.update(links)
        return SourceDependencies(tuple(records.values()), tuple(relations.values()), tuple(blocked))

    def register_archive(self, user, sources):
        from datetime import timezone
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM archive_targets WHERE user=?", (user,))
            db.executemany("INSERT OR IGNORE INTO archive_targets VALUES (?,?)", ((user, source) for source in sources))
            db.execute("INSERT OR REPLACE INTO archive_scan VALUES (?,?)", (user, datetime.now(timezone.utc).isoformat()))

    # A QQ message is indexed when it arrives and again inside its reply exchange.
    # The browser lists the arrival record only, so one message appears once.
    _SHOWN = ("NOT (actor='user' AND source LIKE 'reply:%' AND EXISTS (SELECT 1 FROM source_aliases a "
              "JOIN originals r ON r.user=a.user AND r.source=a.receipt_source AND r.actor='user' "
              "WHERE a.user=originals.user AND a.exchange_source=originals.source))")

    def browse(self, user, query, limit):
        with closing(self.connect()) as db:
            indexed = db.execute("SELECT COUNT(DISTINCT source) FROM originals WHERE user=?", (user,)).fetchone()[0]
            scanned = db.execute("SELECT stamp FROM archive_scan WHERE user=?", (user,)).fetchone()
            total, ready, removed = db.execute("""SELECT COUNT(*),
                COALESCE(SUM(EXISTS(SELECT 1 FROM originals o WHERE o.user=t.user AND o.source=t.source)),0),
                COALESCE(SUM(EXISTS(SELECT 1 FROM forgotten f WHERE f.user=t.user AND f.source=t.source)),0)
                FROM archive_targets t WHERE user=?""", (user,)).fetchone()
            rows = [] if query else db.execute("SELECT source,actor,stamp,text FROM originals WHERE user=? AND " + self._SHOWN +
                                               " ORDER BY stamp DESC,source,actor LIMIT ?", (user, limit)).fetchall()
        if query:
            records = self.search(query, user, limit=limit)
            rows = [(r.source_id, r.metadata['speaker'], r.occurred_at.isoformat() if r.occurred_at else None, r.text) for r in records]
        return {'indexed_letters': indexed, 'archive_total': total if scanned else None,
                'archive_indexed': ready if scanned else None, 'archive_removed': removed if scanned else None,
                'scanned_at': scanned[0] if scanned else None,
                'originals': [{'source_id': s, 'speaker': a, 'created_at': stamp, 'text': text[:4000],
                               'excerpt': bool(query) or len(text)>4000} for s,a,stamp,text in rows]}

    def browse_page(self, user, *, query=None, page=1, limit=20, days=0, sort="new",
                    source_id=None, full=False, now=None):
        from .browse import page_bounds, validate_options
        from datetime import timezone
        validate_options(query=query, page=page, limit=limit, days=days, sort=sort)
        if (source_id is not None and (not isinstance(source_id, str) or not source_id or len(source_id) > 160)
                or type(full) is not bool or full and source_id is None):
            raise ValueError("MEMORY_BROWSE_OPTIONS_INVALID")
        where, args = ["user=?", self._SHOWN], [user]
        if query and query.strip():
            where.append("instr(lower(text),lower(?))>0")
            args.append(query.strip())
        if days:
            where.append("julianday(stamp)>=julianday(?)-? AND julianday(stamp)<=julianday(?)")
            stamp = (now or datetime.now(timezone.utc)).isoformat()
            args.extend((stamp, days, stamp))
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            if source_id is not None:
                sources = sorted(self._aliases(db, user, [source_id]))
                where.append("source IN (" + ",".join("?" for _ in sources) + ")")
                args.extend(sources)
            clause = " AND ".join(where)
            total = db.execute("SELECT COUNT(*) FROM originals WHERE " + clause, args).fetchone()[0]
            page, offset = page_bounds(total, page, limit)
            direction = "DESC" if sort == "new" else "ASC"
            rows = db.execute("SELECT source,actor,stamp,text FROM originals WHERE " + clause +
                              " ORDER BY julianday(stamp) " + direction + ",source,actor LIMIT ? OFFSET ?",
                              [*args, limit, offset]).fetchall()
        result = self.browse(user, "", 1)
        maximum = 50000 if full else 4000
        result.update(total=total, page=page, limit=limit,
                      originals=[{"source_id": s, "speaker": a, "created_at": stamp,
                                  "text": text[:maximum], "excerpt": len(text) > maximum}
                                 for s, a, stamp, text in rows])
        return result

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
            if reply_text.strip():
                digest = _digest(json.dumps([user_text, reply_text, stamp], ensure_ascii=False))
                # Re-indexing old delivered originals also queues their missing
                # character statements. A changed original invalidates old labels.
                db.execute('INSERT INTO relationship_quotes(user,source,digest) VALUES (?,?,?) '
                           'ON CONFLICT(user,source) DO UPDATE SET digest=excluded.digest, attempts=0, categories=NULL '
                           'WHERE relationship_quotes.digest != excluded.digest', (user, source, digest))

    def claim_relationship_exchanges(self, user):
        """Claim at most eight whole exchanges within one shared input budget."""
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT q.source,q.digest,u.text,l.text,l.stamp FROM relationship_quotes q '
                "CROSS JOIN originals u ON u.user=q.user AND u.source=q.source AND u.actor='user' "
                "CROSS JOIN originals l ON l.user=q.user AND l.source=q.source AND l.actor='linli' "
                'WHERE q.user=? AND q.categories IS NULL AND q.attempts<3 '
                'AND julianday(l.stamp) IS NOT NULL AND length(u.text)+length(l.text)<=12000 '
                'AND NOT EXISTS (SELECT 1 FROM forgotten f WHERE f.user=q.user AND f.source=q.source) '
                'ORDER BY julianday(l.stamp) DESC,q.source DESC LIMIT 8', (user,)).fetchall()
            originals, chars = [], 0
            for row in rows:
                size = len(row[2]) + len(row[3])
                if chars + size > 12000:
                    break  # Keep whole originals in chronological priority order.
                db.execute('UPDATE relationship_quotes SET attempts=attempts+1 WHERE user=? AND source=?', (user, row[0]))
                originals.append(dict(zip(('source_id', 'digest', 'user_message', 'assistant_message', 'occurred_at'), row)))
                chars += size
            return originals

    def finish_relationship_exchanges(self, user, labelled):
        rows = []
        for source, digest, categories in labelled:
            if not set(categories) <= {'identity', 'affection', 'agreement'}:
                raise ValueError('RELATIONSHIP_CATEGORIES_INVALID')
            rows.append((json.dumps(sorted(set(categories))), user, source, digest))
        with closing(self.connect()) as db, db:
            # Forgotten/overwritten originals cannot be restored by a late result.
            return bool(db.executemany('UPDATE relationship_quotes SET categories=? '
                'WHERE user=? AND source=? AND digest=?',
                rows).rowcount)

    def relationship_context(self, user, *, as_of, exclude_source_ids=(), max_chars=4000):
        """Read whole attributed exchanges, independent of model and search words."""
        cutoff = _aware_time(as_of).isoformat()
        packet = {'kind': 'relationship_history', 'evidence_scope': 'recorded_utterance',
                  'meaning': '按speaker与时间承接角色自己说过的关系定位、感情与约定，保留条件、更正和撤回；'
                             '用户单方面称呼不是双方共识，原话不授予当前动作许可或升级阶段。'
                             '这是部分历史，缺失不表示没发生，换模型也不能抹去给定原话。',
                  'coverage': 'bounded', 'records': []}
        encode = lambda: json.dumps(packet, ensure_ascii=False, separators=(',', ':'))
        with closing(self.connect()) as db:
            db.execute('BEGIN')
            excluded = self._aliases(db, user, exclude_source_ids)
            # Start with the small pending/kept index, rather than walking every
            # original body before filtering out ordinary conversation.
            pending = db.execute('SELECT 1 FROM relationship_quotes q CROSS JOIN originals l '
                "ON l.user=q.user AND l.source=q.source AND l.actor='linli' "
                'WHERE q.user=? AND q.categories IS NULL AND julianday(l.stamp)<=julianday(?) LIMIT 1',
                (user, cutoff)).fetchone()
            if pending:
                packet['coverage'] = 'extraction_incomplete'
            candidates = {}
            for category in ('identity', 'affection', 'agreement'):
                rows = db.execute('SELECT q.source,l.stamp FROM relationship_quotes q CROSS JOIN originals l '
                    "ON l.user=q.user AND l.source=q.source AND l.actor='linli' "
                    "WHERE q.user=? AND q.categories!='[]' AND instr(q.categories,?)>0 AND julianday(l.stamp)<=julianday(?) "
                    'ORDER BY julianday(l.stamp) DESC,q.source DESC LIMIT 2',
                    (user, '"' + category + '"', cutoff)).fetchall()
                for source, stamp in rows:
                    if source not in excluded:
                        candidates.setdefault((stamp, source), set()).add(category)
            groups, blocked = [], set()
            for (stamp, source), categories in sorted(candidates.items(), key=lambda item: (_aware_time(item[0][0]), item[0][1]), reverse=True):
                if categories & blocked:
                    continue
                group = []
                for actor in ('user', 'linli'):
                    original = self._dependency_original(db, user, source, actor)
                    if original is not None and original[0].strip():
                        group.append({'citation': source + ':' + actor, 'speaker': actor, 'text': original[0],
                            'provenance': {'source_record_id': source}, 'evidence_scope': 'recorded_utterance',
                            'occurred_at': stamp if actor == 'linli' else None})
                packet['records'] = [r for g in reversed([*groups, group]) for r in g]
                if len(encode()) + 32 > max_chars:
                    # Do not admit an old promise while omitting its later
                    # withdrawal, or cut off a condition at the end of a reply.
                    blocked.update(categories)
                    packet['coverage'] = 'omitted_due_to_capacity'
                else:
                    groups.append(group)
            packet['records'] = [r for g in reversed(groups) for r in g]
        return encode()

    def retract_received(self, user, sources):
        """Drop receipts that were never delivered (failed letters), without a user "forget".

        Only unaliased received-user originals are removed, so a delivered
        exchange that shares the receipt keeps its evidence.
        """
        removed = 0
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            for source in dict.fromkeys(sources):
                if not isinstance(source, str) or not source.startswith('received-user:'):
                    continue
                if db.execute('SELECT 1 FROM source_aliases WHERE user=? AND receipt_source=?', (user, source)).fetchone():
                    continue
                removed += db.execute('DELETE FROM originals WHERE user=? AND source=? AND actor=?',
                                      (user, source, 'user')).rowcount
                db.execute('DELETE FROM chunks WHERE user=? AND source=?', (user, source))
        return removed

    def forget(self, user, source=None):
        with closing(self.connect()) as db, db:
            sources = [source] if source is not None else [r[0] for r in db.execute("SELECT DISTINCT source FROM originals WHERE user=?", (user,))]
            self._forget_sources(db, user, sources)

    def search(self, query, user, semantic=(), limit=5, exclude_source_ids=(), expanded=False):
        if expanded:
            return self._expanded_search(query, user, semantic, limit, exclude_source_ids)
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
                              **self._lineage(db, user, source),
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

    def _expanded_search(self, query, user, semantic, limit, excluded):
        """Fuse per-topic and semantic ranks without allowing either to starve."""
        excluded = set(excluded)
        from .recall import query_topics
        topics = query_topics(query)
        ranked, topic_indexes, scores = [], {}, {}
        with closing(self.connect()) as db:
            db.execute('BEGIN')
            excluded.update(row[0] for row in db.execute('SELECT source FROM forgotten WHERE user=?', (user,)))
            semantic_by_source = {}
            for record in semantic:
                if record.user_id == user and record.source_id not in excluded:
                    semantic_by_source.setdefault(record.source_id, {})[record.memory_id] = record
            for topic_index, topic in enumerate(topics):
                words = terms(topic)
                hits = []
                # Cover every part of a long sentence without an enormous FTS expression.
                for start in range(0, len(words), 128):
                    expression = ' OR '.join('"' + word + '"' for word in words[start:start + 128])
                    batch = []
                    for (source,) in db.execute(
                        'SELECT source FROM chunks WHERE tokens MATCH ? AND user=? ORDER BY bm25(chunks),rowid',
                        (expression, user)):
                        if source not in excluded and source not in batch:
                            batch.append(source)
                            if len(batch) == 12:
                                break
                    hits.extend(batch)
                hits = list(dict.fromkeys(hits))[:12]
                ranked.append(hits)
                for rank, source in enumerate(hits):
                    topic_indexes.setdefault(source, []).append(topic_index)
                    scores[source] = scores.get(source, 0) + 1 / (60 + rank + 1)
            semantic_sources = list(semantic_by_source)
            for rank, source in enumerate(semantic_sources):
                scores[source] = scores.get(source, 0) + 1 / (60 + rank + 1)
            # Reserve a turn for semantic evidence at every rank, including sources
            # with no surviving original. The per-topic turns preserve broad recall.
            sources, seen = [], set()
            for rank in range(max(12, len(semantic_sources))):
                round_sources = sorted({hits[rank] for hits in ranked if rank < len(hits)},
                                       key=lambda source: (-scores[source], source))
                if rank < len(semantic_sources):
                    round_sources.insert(0, semantic_sources[rank])
                for source in round_sources:
                    if source not in seen:
                        sources.append(source)
                        seen.add(source)
            result = []
            for source in sources:
                route = 'hybrid' if source in topic_indexes and source in semantic_by_source else 'fts' if source in topic_indexes else 'semantic'
                metadata = {'retrieval_route': route, 'topic_indexes': ','.join(map(str, topic_indexes.get(source, ())))}
                semantic_group = tuple(semantic_by_source.get(source, {}).values())
                if any(r.metadata.get('origin') == 'proactive' for r in semantic_group):
                    metadata['origin'] = 'proactive'
                group = self._source_records(db, user, source, metadata)
                if not group:
                    group = tuple(replace(r, metadata={**r.metadata, **metadata}) for r in semantic_group)
                if len(result) + len(group) <= limit:
                    result.extend(group)  # Keep every part of both sides together.
            return tuple(result)

    @staticmethod
    def _source_records(db, user, source, metadata, *, unknown_time=False):
        if db.execute('SELECT 1 FROM forgotten WHERE user=? AND source=?', (user, source)).fetchone():
            return ()
        rows = db.execute('SELECT actor,stamp,text FROM originals WHERE user=? AND source=? ORDER BY actor DESC', (user, source)).fetchall()
        metadata = {**metadata, **SourceRetrieval._lineage(db, user, source)}
        result = []
        for actor, stamp, text in rows:
            occurred_at = datetime.fromisoformat(stamp) if stamp else None
            if unknown_time and occurred_at is not None and occurred_at.utcoffset() is None:
                occurred_at = None
            parts = []
            for offset in range(0, len(text), 2000):
                raw = text[offset:offset + 2000]
                part = raw.strip()
                if part:
                    start = offset + len(raw) - len(raw.lstrip())
                    parts.append((start, part))
            for start, part in parts:
                result.append(ConversationMemoryRecord(
                    memory_id='original:' + hashlib.sha256(f'{user}:{source}:{actor}:{start}:full'.encode()).hexdigest(),
                    text=part, user_id=user, source_id=source,
                    occurred_at=occurred_at,
                    metadata={'verbatim': True, 'speaker': actor, 'canonical': True, 'complete_original': True,
                              'start': start, 'end': start + len(part), 'part_count': len(parts),
                              **metadata,
                              **({'history_actor': actor} if source.startswith('history:') else {})}))
        return tuple(result)

    def sources_in_range(self, user, first, last, query='', *, exclude_source_ids=(), limit=6):
        """Complete exchanges received between two instants, best matching the question first."""
        excluded = set(exclude_source_ids)
        wanted = set(terms((query or '')[:2000]))
        with closing(self.connect()) as db:
            db.execute('BEGIN')
            excluded.update(row[0] for row in db.execute('SELECT source FROM forgotten WHERE user=?', (user,)))
            rows = db.execute('SELECT source, MIN(stamp), GROUP_CONCAT(text, char(10)) FROM originals WHERE user=? '
                              'AND stamp IS NOT NULL AND julianday(stamp) >= julianday(?) AND julianday(stamp) < julianday(?) '
                              'GROUP BY source', (user, first.isoformat(), last.isoformat())).fetchall()
            ranked = sorted((row for row in rows if row[0] not in excluded),
                            key=lambda row: (-len(wanted.intersection(terms(row[2] or ''))), row[1], row[0]))
            return tuple(record for source, _, _ in ranked[:limit]
                         for record in self._source_records(db, user, source, {'retrieval_route': 'date'}))

    # Semantic index over whole exchanges. Vectors are derived data: a changed or
    # forgotten original simply loses its vector and is embedded again later.
    @staticmethod
    def _exchange_text(rows):
        user = ' '.join(text for actor, text in rows if actor == 'user')
        linli = ' '.join(text for actor, text in rows if actor != 'user')
        return (user[:600] + '\n' + linli[:400]).strip()

    def vectors_missing(self, user, model, limit=32):
        """Exchanges whose current text has no vector for this model, newest first."""
        with closing(self.connect()) as db:
            sources = [row[0] for row in db.execute(
                'SELECT source FROM originals WHERE user=? GROUP BY source ORDER BY MAX(stamp) DESC', (user,))]
            result = []
            for source in sources:
                if db.execute('SELECT 1 FROM forgotten WHERE user=? AND source=?', (user, source)).fetchone():
                    continue
                rows = db.execute('SELECT actor,text FROM originals WHERE user=? AND source=? ORDER BY actor DESC',
                                  (user, source)).fetchall()
                text = self._exchange_text(rows)
                if not text:
                    continue
                digest = _digest(text)
                stored = db.execute('SELECT digest FROM source_vectors WHERE user=? AND source=? AND model=?',
                                    (user, source, model)).fetchone()
                if stored is None or stored[0] != digest:
                    result.append((source, digest, text))
                    if len(result) >= limit:
                        break
            return result

    def put_vectors(self, user, model, items):
        """items: (source, digest, vector) with a unit-length float vector."""
        from array import array
        with closing(self.connect()) as db, db:
            for source, digest, vector in items:
                db.execute('INSERT OR REPLACE INTO source_vectors VALUES (?,?,?,?,?)',
                           (user, source, model, digest, array('f', vector).tobytes()))

    def vector_coverage(self, user, model):
        """Share of exchanges holding a vector for this model (stale ones count)."""
        with closing(self.connect()) as db:
            total = db.execute('SELECT COUNT(DISTINCT source) FROM originals WHERE user=?', (user,)).fetchone()[0]
            have = db.execute('SELECT COUNT(*) FROM source_vectors WHERE user=? AND model=?', (user, model)).fetchone()[0]
        return have / total if total else 0.0

    def nearest_sources(self, user, model, vector, *, limit=12, exclude_source_ids=()):
        """Sources ranked by cosine similarity to a unit-length query vector."""
        from array import array
        excluded = set(exclude_source_ids)
        query = array('f', vector)
        scored = []
        with closing(self.connect()) as db:
            excluded.update(row[0] for row in db.execute('SELECT source FROM forgotten WHERE user=?', (user,)))
            rows = [(source, blob) for source, blob in db.execute(
                'SELECT source,vector FROM source_vectors WHERE user=? AND model=?', (user, model))
                if source not in excluded and len(blob) == len(query) * query.itemsize]
        try:
            import numpy
        except ImportError:
            numpy = None
        if numpy is not None and rows:
            matrix = numpy.frombuffer(b''.join(blob for _, blob in rows), dtype=numpy.float32).reshape(len(rows), len(query))
            scored = list(zip((matrix @ numpy.asarray(query, dtype=numpy.float32)).tolist(), (source for source, _ in rows)))
        else:
            for source, blob in rows:
                stored = array('f')
                stored.frombytes(blob)
                scored.append((sum(a * b for a, b in zip(query, stored)), source))
        scored.sort(reverse=True)
        result = []
        with closing(self.connect()) as db:
            for score, source in scored[:limit]:
                row = db.execute("SELECT text FROM originals WHERE user=? AND source=? AND actor='user'", (user, source)).fetchone()                     or db.execute('SELECT text FROM originals WHERE user=? AND source=?', (user, source)).fetchone()
                snippet = ' '.join((row[0] if row else '').split())[:200]
                if snippet:
                    result.append((source, score, snippet))
        return result

    def get_sources(self, user, source_ids, exclude_source_ids=()):
        """Read complete source groups atomically; never restore forgotten data."""
        excluded = set(exclude_source_ids)
        with closing(self.connect()) as db:
            db.execute('BEGIN')
            return tuple(record for source in dict.fromkeys(source_ids) if source not in excluded
                         for record in self._source_records(db, user, source, {'retrieval_route': 'source'}))

    def get_received_sources(self, user, source_ids, *, before=None):
        """Resolve trusted exchange IDs to received user originals, never replies."""
        cutoff = _aware_time(before) if before is not None else None
        with closing(self.connect()) as db:
            db.execute('BEGIN')
            sources = self._aliases(db, user, source_ids)
            return tuple(record for source in sorted(sources) if source.startswith('received-user:')
                         for record in self._source_records(db, user, source, {'retrieval_route': 'receipt'})
                         if record.metadata.get('speaker') == 'user'
                         and record.occurred_at is not None
                         and (cutoff is None or _aware_time(record.occurred_at) <= cutoff))

    def forgotten_sources(self, user):
        with closing(self.connect()) as db:
            return frozenset(row[0] for row in db.execute('SELECT source FROM forgotten WHERE user=?', (user,)))

    def expand_sources(self, user, source_ids, exclude_source_ids=(), limit=12):
        """Read at most one neighbour per direction, as context rather than proof.

        limit counts records; an exchange and all of its parts are atomic.
        """
        seeds = tuple(dict.fromkeys(source_ids))
        excluded = set(exclude_source_ids)
        result, seen = [], set(seeds)
        with closing(self.connect()) as db:
            db.execute('BEGIN')
            for seed in seeds:
                if seed in excluded or not self._source_records(db, user, seed, {}):
                    continue
                stamp = db.execute('SELECT stamp FROM originals WHERE user=? AND source=? AND stamp IS NOT NULL LIMIT 1', (user, seed)).fetchone()
                if stamp is None:
                    continue
                for comparison, order in (('<', 'DESC'), ('>', 'ASC')):
                    neighbour = db.execute(
                        f'SELECT DISTINCT source,stamp FROM originals WHERE user=? AND '
                        f'(julianday(stamp), source) {comparison} (julianday(?), ?) '
                        f'ORDER BY julianday(stamp) {order},source {order} LIMIT 1',
                        (user, stamp[0], seed)).fetchone()
                    if neighbour is None or neighbour[0] in seen or neighbour[0] in excluded:
                        continue
                    source = neighbour[0]
                    seen.add(source)
                    group = self._source_records(db, user, source, {'retrieval_route': 'context', 'expansion_seed': seed})
                    if len(result) + len(group) <= limit:
                        result.extend(group)
        return tuple(result)
