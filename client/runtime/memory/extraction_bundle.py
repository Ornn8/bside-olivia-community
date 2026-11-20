"""One memory-extraction judgment for several exchanges.

An exchange used to cost two judgments of purpose ``memory-extraction``: the fact sentences of the user's message
(runtime.memory.jev_memory_extraction) and, a moment later, the three relationship questions about the exchange
(runtime.memory.relationship_continuity). Both are keep/skip questions over the same words, so they travel in one
request. The request can also carry up to ``batch`` exchanges; the outbox then waits for a batch to fill or the
conversation to go quiet (``flush_count``). What was said a moment ago stays findable: the raw exchange index is
written at once and the last turns are always in the reply prompt, so only the derived indexes lag behind.

The cloud policy ``memory_extraction`` switches this on (``mode: bundle``); the default is the former behaviour.
Only a bundle request declares ``BUNDLE_PRICE_VERSION``; the relay prices it (qwen/jev_billing.py) and the JEV service
gives its fact questions the chat criteria (memory_chat_rules.py). Contract: the memory-extraction bundle spec.

A judgment made here is only used for exactly the words it was made for. Anything else (a failure, a message that
changed, a process restart) falls back to the former per-judgment calls.
"""
from __future__ import annotations

import hashlib
import threading
import time

from .jev_memory_extraction import CONTRACT as FACT_CONTRACT, CRITERIA, MAX_SPANS, candidate_spans
from .relationship_continuity import CONTRACT as RELATIONSHIP_CONTRACT, MEANINGS, relationship_questions

BUNDLE_PRICE_VERSION = 'jev-input-cny-20261108-v8'
PRICE_HEADER = 'X-Olivia-JEV-Price'
FACT_INSTRUCTION = '遵守state.fact_check.contract，判断state.fact_check.spans.{key}。'
CHAT_MAX_CHARS = 400            # the JEV service gives a message up to this long the chat criteria
MAX_EXCHANGE_CHARS = 12_000     # the same shared input budget claim_relationship_exchanges keeps
DEFAULT_POLICY = {'mode': 'separate', 'batch': 6, 'idle_seconds': 600}
MAX_BUNDLES_PER_SCAN = 8
_TTL_SECONDS = 3600


def policy() -> dict:
    """The cloud policy for extraction; any trouble reading it means the former behaviour."""
    try:
        from runtime import cloud_policy
        value = cloud_policy.get_policy('memory_extraction')
        if (isinstance(value, dict) and value.get('mode') in ('separate', 'bundle')
                and type(value.get('batch')) is int and type(value.get('idle_seconds')) is int):
            return {key: value[key] for key in DEFAULT_POLICY}
    except Exception:  # noqa: BLE001 - extraction must never fail because of policy
        pass
    return dict(DEFAULT_POLICY)


def is_bundle(packet) -> bool:
    """True for a request built by ``build`` (the sender names its price version)."""
    try:
        return (packet['purpose'] == 'memory-extraction'
                and packet['state']['source_metadata']['bundle'] is True)
    except (KeyError, TypeError):
        return False


def chat_line(source_id: str, user_message: str) -> bool:
    return source_id.startswith('reply:') and len(user_message) <= CHAT_MAX_CHARS


def flush_count(ages, batch: int, idle_seconds: int) -> int:
    """How many of the pending exchanges (``ages`` in seconds since each was said, oldest first) to judge now.

    All pending ones (up to MAX_BUNDLES_PER_SCAN bundles) once ``batch`` of them wait, once the newest has been
    quiet for ``idle_seconds`` or once the oldest has waited three times that; otherwise none."""
    if not ages:
        return 0
    if len(ages) >= batch or min(ages) >= idle_seconds or max(ages) >= 3 * idle_seconds:
        return min(len(ages), batch * MAX_BUNDLES_PER_SCAN)
    return 0


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def build(items, *, fact_prompt):
    """The request for ``items`` (dicts: source_id, user_message, rel) and how to read its answers.

    ``rel`` is the pending relationship row of the exchange (runtime.memory.source_retrieval) or None. Returns
    (state, questions, layout); layout holds what ``split`` needs: the source of every message, the sentence and
    the place behind every ``s<N>`` key and the relationship rows in the order of ``state.exchanges``."""
    messages = [{'role': 'user', 'content': item['user_message']} for item in items]
    candidates, references = candidate_spans(messages)
    exchanges = [item['rel'] for item in items if item.get('rel') is not None]
    sources = [item['source_id'] for item in items]
    # The two judgments keep their own sub-states: with both contracts side by side the relationship answers
    # drifted (replay 2026-11-08: 12 of 30 former yes answers lost, against 18 lost with the flat layout).
    state = {'fact_check': {'contract': FACT_CONTRACT, 'memory_rules': fact_prompt, 'original_messages': messages,
                            'spans': references},
             'source_metadata': {'domain': 'conversation_memory', 'canonical': True, 'actor': 'local_user',
                                 'bundle': True, 'source_ids': sources}}
    questions = {key: {'instructions': FACT_INSTRUCTION.format(key=key), 'criteria': dict(CRITERIA)}
                 for key in candidates}
    if exchanges:
        state['relationship_check'] = {
            'contract': RELATIONSHIP_CONTRACT,
            'exchanges': [{key: row[key] for key in ('source_id', 'user_message', 'assistant_message', 'occurred_at')}
                          for row in exchanges]}
        questions.update(relationship_questions(len(exchanges), 'state.relationship_check.contract',
                                                'state.relationship_check.exchanges'))
    return state, questions, dict(sources=sources, candidates=candidates, references=references, exchanges=exchanges)


def split(layout, answers):
    """({source_id: [kept sentences]}, [(relationship row, categories)]) from the answers of a ``build`` request."""
    kept = {source: [] for source in layout['sources']}
    for key, quote in layout['candidates'].items():
        if answers[key] == 'keep':
            kept[layout['sources'][layout['references'][key]['message_index']]].append(quote)
    related = [(row, tuple(key for key in MEANINGS if answers[f'r{i}_{key}'] == 'keep'))
               for i, row in enumerate(layout['exchanges'])]
    return kept, related


def plan(items, *, batch: int, fact_prompt: str, fits_bytes=None):
    """Group ``items`` (oldest first) into requests: chat lines apart from long letters, at most ``batch`` exchanges,
    MAX_SPANS sentences and MAX_EXCHANGE_CHARS characters each, and ``fits_bytes(state, questions)`` if given.
    Returns (bundles, excluded): an item that cannot be judged within the limits even alone is excluded and keeps
    the former per-judgment path."""
    bundles, excluded, current = [], [], []

    def fits(group):
        if len(group) > batch:
            return False
        state, questions, layout = build(group, fact_prompt=fact_prompt)
        if len(layout['candidates']) > MAX_SPANS:
            return False
        if sum(len(row['user_message']) + len(row['assistant_message']) for row in layout['exchanges']) > MAX_EXCHANGE_CHARS:
            return False
        return fits_bytes is None or fits_bytes(state, questions)

    for item in items:
        kind = chat_line(item['source_id'], item['user_message'])
        if current and chat_line(current[0]['source_id'], current[0]['user_message']) != kind:
            bundles.append(current)
            current = []
        if fits(current + [item]):
            current.append(item)
            continue
        if current:
            bundles.append(current)
            current = []
        if fits([item]):
            current.append(item)
        else:
            excluded.append(item)
    if current:
        bundles.append(current)
    return bundles, excluded


# What a bundle judged is handed to the per-message path of the same source (mem0_memory), once, for exactly
# those words.
_LOCK = threading.Lock()
_KEPT: dict[str, tuple[str, tuple[str, ...], float]] = {}


def put(source_id: str, message: str, quotes) -> None:
    now = time.monotonic()
    with _LOCK:
        for key in [key for key, (_, _, at) in _KEPT.items() if now - at > _TTL_SECONDS]:
            del _KEPT[key]
        _KEPT[source_id] = (_digest(message), tuple(quotes), now)


def take(source_id, message):
    """The kept sentences a bundle judged for this source and message, once; None when there are none."""
    if not isinstance(source_id, str):
        return None
    with _LOCK:
        entry = _KEPT.pop(source_id, None)
    if entry is None or entry[0] != _digest(message) or time.monotonic() - entry[2] > _TTL_SECONDS:
        return None
    return list(entry[1])


def clear() -> None:
    with _LOCK:
        _KEPT.clear()
