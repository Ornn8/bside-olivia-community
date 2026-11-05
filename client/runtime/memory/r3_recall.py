"""Recall by reranking (the "r3" recipe of the memory benchmark).

The exchanges retrieved for a message (cloud vectors, or the local model when the cloud index is not ready; plus the
word index) are up to ``CANDIDATES`` whole exchanges. The relay's /v1/rerank orders them against the message
alone and the best ``KEEP`` reach the writer, oldest first, as the dated lines of ``r3_block``. It replaces the
recall-selection judgment, one call for one call, so a turn makes the same number of model calls.

Any failure, or more than ``TIMEOUT`` seconds, raises ``Unavailable`` with a content-free reason code; the caller
then runs the former judgment, so a failed rerank never lowers the quality of that reply and never fails it.

Switch (default ON): ``OLIVIA_R3_RECALL=0`` or ``{"enabled": false}`` in
``<data root>/r3-recall.json``; the same file may set ``keep`` (1-10) and ``recall_chars`` (800-8000).
The compact lines of the recalled block have their own switch (default ON, ``OLIVIA_R3_COMPACT=0`` or
``{"compact": false}``): off, the block holds the former records again.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)
CANDIDATES = 40
KEEP = 10
TIMEOUT = 5.0
QUERY_CHARS = 2000
DOC_CHARS = 2500            # relay: each document <= 4000 characters, all together <= 120000
MIN_SCORE = 0.5            # of the benchmark's top-10 scores about one in ten is below this
RECALL_CHARS = 1250         # the recalled block never exceeds this, measured on the whole rendered element; on production 11-05 the judgment added 1243 chars on average (QQ 1161, letters 1843)
_COOLDOWN = 120.0           # after a refusal or an outage the former judgment runs without trying again first
_failed_at = 0.0


class Unavailable(Exception):
    """Rerank recall cannot answer this turn; ``code`` is a short content-free reason."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _setting(name):
    root = os.environ.get('OLIVIA_LOCAL_DATA_ROOT', '').strip()
    if not root:
        return None
    try:
        path = Path(root) / 'r3-recall.json'
        if path.exists() and path.stat().st_size <= 4096:
            value = json.loads(path.read_text(encoding='utf-8-sig'))   # a file saved by Windows tools may start with a BOM
            return value.get(name) if isinstance(value, dict) else None
    except (OSError, ValueError):
        pass                # an unreadable setting is the default
    return None


def enabled() -> bool:
    """On unless switched off: ``OLIVIA_R3_RECALL=0`` or ``{"enabled": false}`` in ``<data root>/r3-recall.json``.
    The environment wins over the file. This is the retrieval change only (rerank instead of the judgment)."""
    return _switch('OLIVIA_R3_RECALL', 'enabled', default=True)


def compact_enabled() -> bool:
    """The recalled block as dated lines (``r3_block``), on unless switched off: ``OLIVIA_R3_COMPACT=0`` or
    ``{"compact": false}`` in ``<data root>/r3-recall.json``. It only changes how the exchanges are written down;
    which exchanges are chosen and the size bound are the same."""
    return _switch('OLIVIA_R3_COMPACT', 'compact', default=True)


def format_enabled() -> bool:
    """The dated format only (``OLIVIA_R3_FORMAT=1`` or ``{"format": true}``), independent of the retrieval switch."""
    return _switch('OLIVIA_R3_FORMAT', 'format')


def _switch(variable, name, default=False) -> bool:
    env = os.environ.get(variable, '').strip().lower()
    if env in {'0', 'off', 'false', 'no'}:
        return False
    if env in {'1', 'on', 'true', 'yes'}:
        return True
    value = _setting(name)
    return value if isinstance(value, bool) else default


def min_score() -> float:
    """Guard against irrelevant recall: an exchange whose rerank score is below this is not shown (file key
    ``min_score``, 0-1). Nothing is shown at all when no exchange reaches it."""
    value = _setting('min_score')
    return float(value) if type(value) in (int, float) and 0 <= value <= 1 else MIN_SCORE


def _bounded(name, default, low, high):
    value = _setting(name)
    return value if type(value) is int and low <= value <= high else default


def keep() -> int:
    """How many exchanges at most reach the writer (file key ``keep``, 1-10)."""
    return _bounded('keep', KEEP, 1, KEEP)


def recall_chars() -> int:
    """The recalled block's size bound (file key ``recall_chars``, 800-8000)."""
    return _bounded('recall_chars', RECALL_CHARS, 800, 8000)


def document(group) -> str:
    """One exchange as the reranker reads it: who said it, then the words."""
    lines = [('对方：' if r.get('speaker') == 'user' else '你：') + r['text']
             for r in group if isinstance(r, dict) and isinstance(r.get('text'), str) and r['text'].strip()]
    return '\n'.join(lines)[:DOC_CHARS]


def candidates(groups, recent_citations=()):
    """The retrieved groups without those already in the recent window, once each, in retrieval order."""
    recent, seen, found = set(recent_citations), set(), []
    for group in groups:
        cites = {r.get('citation') for r in group}
        text = document(group)
        if not text or (cites and cites <= recent) or text in seen:
            continue
        seen.add(text)
        found.append((group, text))
        if len(found) == CANDIDATES:
            break
    return found


def _post(key, query, documents):
    from runtime import ui_language
    from runtime.official_endpoints import API_BASE
    from runtime.reply.companion_decision import _NoRedirect
    from runtime.tls import client_tls_context
    body = json.dumps({'query': query[:QUERY_CHARS], 'documents': documents, 'top_n': keep()},
                      ensure_ascii=False).encode()
    request = urllib.request.Request(API_BASE + '/rerank', data=body, method='POST', headers={
        'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', **ui_language.headers()})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                         urllib.request.HTTPSHandler(context=client_tls_context()))
    with opener.open(request, timeout=TIMEOUT) as response:
        return json.loads(response.read(1024 * 1024))


def _order(answer, count):
    order, seen = [], False
    for item in (answer.get('results') if isinstance(answer, dict) else None) or []:
        index = item.get('index') if isinstance(item, dict) else None
        if type(index) is int and 0 <= index < count and index not in order:
            seen = True
            score = item.get('score')
            if type(score) in (int, float) and score < min_score():
                continue            # not relevant enough: shown to nobody
            order.append(index)
    if not seen:
        raise Unavailable('r3_rerank_empty')
    return order[:keep()]


async def _ask(query, documents):
    global _failed_at
    from . import remote_embedding
    key = remote_embedding._key()
    if key is None:
        raise Unavailable('r3_rerank_no_key')
    if time.monotonic() - _failed_at < _COOLDOWN:
        raise Unavailable('r3_rerank_cooldown')
    try:
        answer = await asyncio.wait_for(asyncio.to_thread(_post, key, query, documents), timeout=TIMEOUT + 1)
    except (asyncio.TimeoutError, TimeoutError):
        raise Unavailable('r3_rerank_timeout') from None
    except urllib.error.HTTPError as exc:
        exc.close()
        _failed_at = time.monotonic()
        raise Unavailable('r3_rerank_http_%d' % exc.code if 400 <= exc.code < 600 else 'r3_rerank_http') from None
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, 'reason', None), TimeoutError):
            raise Unavailable('r3_rerank_timeout') from None
        _failed_at = time.monotonic()
        raise Unavailable('r3_rerank_network') from None
    except (OSError, ValueError):
        _failed_at = time.monotonic()
        raise Unavailable('r3_rerank_response') from None
    return _order(answer, len(documents))


async def choose(groups, query, *, recent_citations=()):
    """(offered, ids): ``offered`` maps 'r0'... to the candidate groups, ``ids`` are the best ones, best first.

    One rerank call whenever there is a candidate: even a few are scored, so that nothing irrelevant is shown."""
    found = candidates(groups, recent_citations)
    offered = {'r%d' % i: group for i, (group, _) in enumerate(found)}
    if not found:
        return offered, []
    if not isinstance(query, str) or not query.strip():
        raise Unavailable('r3_rerank_no_query')
    order = await _ask(query, [text for _, text in found])
    return offered, ['r%d' % i for i in dict.fromkeys(order) if 0 <= i < len(found)][:keep()]
