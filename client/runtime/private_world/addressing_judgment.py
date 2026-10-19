"""One small judgment a day picks the term of address out of the quotes seen since the last one, with the message each came from.

A longer quote ("早安呀饭饭", "外边下雨了小猫咪") may hold a greeting or a clause around the name. The shape check
(addressing.address_term) is only the fallback: it applies at once and whenever the judgment is not available.
The judgment is never on the reply path: it runs with the relationship batch (relationship_batch.tick), which
follows its own cadence. Each distinct quote is judged once and the result is kept.
"""
import asyncio
from datetime import timedelta, timezone
import re

from .addressing import QUOTE_MAX, address_term, normalize

PURPOSE = 'addressing-term'
INSTRUCTIONS = ('quote 是从说话的人的 message 里摘出、可能是称呼的片段。判断说话的人是不是在用 quote 里的某个词称呼对方：'
                '对方的昵称、爱称、名字或亲昵的叫法才算，只选称呼本身，不带问候语、天气、动作或句子的其他部分。'
                '说自己是什么人、说话人用来指自己的说法、说起第三方或别的事物、普通的应答和语气'
                '（例如“我是棒球手”里的棒球手、“哥们儿今天累死了”里的哥们儿、“好吧”）都不是称呼对方，选 none。')
SPEAKER = {'user_calls_linli': 'user', 'linli_calls_user': 'her'}
# One request a day carries at most this many questions (one per distinct quote); the rest wait for the next day.
MAX_ITEMS = 20
SPAN_MAX = 6      # a term is picked from the first and last characters of the quote, this many at most
WHOLE_MAX = 8     # a quote of up to this many characters may also be the term as a whole
SHORT_CLAUSE = 12  # a clause of up to this many characters offers every span, not only its edges
REQUEST_TIMEOUT_SECONDS = 25
SHANGHAI = timezone(timedelta(hours=8))
DAY_KEY = 'addressing_batch_day'
# The second key queues the short quotes once more: they were kept unjudged before 2.4.10.
LEGACY_KEY = 'addressing_legacy_queued_v2'
# The exchange questions rarely pick a quote (2026-10-19 synthetic check: one name in eleven), so the daily request
# also reads the newest short messages themselves; only a term the judgment finds is ever kept from them.
SCAN_KEY = 'addressing_scan_after'
SCAN_EXCHANGES = 12
_WORDS = re.compile(r'[\w一-鿿]+')
_CLAUSES = re.compile(r'[\s，,。.！!？?、；;：:~～…]+')


class AddressingJudgmentUnavailable(Exception):
    """The judgment could not be had; callers keep the shape check."""


def candidate_spans(quote):
    """Every prefix and suffix up to SPAN_MAX characters of each clause of the quote, and a short clause as a whole.

    A name sits at the edge of its clause more than of the whole quote ("好呀老姜，那我以后就这么叫你")."""
    spans = []
    for clause in _CLAUSES.split(quote):
        clause = clause.strip()
        if len(clause) <= SHORT_CLAUSE:
            # A short clause offers every span, so a name inside it ("那头号粉丝要一直支持我哦") stands on its own.
            spans += [clause[i:i + size] for size in range(1, min(SPAN_MAX, len(clause)) + 1)
                      for i in range(len(clause) - size + 1)]
        else:
            for size in range(1, min(SPAN_MAX, len(clause)) + 1):
                spans += [clause[:size], clause[-size:]]
        if clause and len(clause) <= WHOLE_MAX:
            spans.append(clause)
    # Only word characters can be a term, and 'none' is the answer for no term.
    return [span for span in dict.fromkeys(spans) if span != 'none' and _WORDS.fullmatch(span)]


def build_request(items):
    """items: [(kind, quote) or (kind, quote, message)] -> (state, questions), one question per item, in order.

    The message is the whole thing the speaker said; without it (older exchanges) the quote stands for it."""
    state = {'items': []}
    questions = {}
    for index, item in enumerate(items):
        kind, quote = item[0], item[1]
        message = item[2] if len(item) > 2 and item[2] else quote
        key = f'q{index}'
        state['items'].append({'id': key, 'speaker': SPEAKER[kind], 'message': message, 'quote': quote})
        questions[key] = {
            'instructions': f'针对 items 里 id 为 {key} 的那一条：' + INSTRUCTIONS,
            'criteria': {**{span: f'称呼「{span}」' for span in candidate_spans(quote)}, 'none': '没有称呼对方'}}
    return state, questions


def _bounded(port, timeout):
    # The transport's own timeout ends the worker thread too; the caller never waits longer than `timeout`.
    transport = getattr(port, 'transport', None)
    if transport is not None and hasattr(transport, 'timeout_seconds'):
        transport.timeout_seconds = max(1, min(transport.timeout_seconds, timeout - 1))


async def ask(port, items, *, timeout=REQUEST_TIMEOUT_SECONDS):
    """One semantic-decisions request for all items. Returns one term (or None for no term) per item."""
    state, questions = build_request(items)
    _bounded(port, timeout)
    try:
        answers = await asyncio.wait_for(port.ask(state, questions, purpose=PURPOSE), timeout)
    except Exception as exc:
        raise AddressingJudgmentUnavailable(type(exc).__name__) from None
    if not isinstance(answers, dict) or set(answers) != set(questions) or any(
            not isinstance(answer, str) or answer not in questions[key]['criteria'] for key, answer in answers.items()):
        raise AddressingJudgmentUnavailable('JEV_RESPONSE_INVALID')
    # The chosen span still passes the shape check; one that does not is no term.
    return [None if answers[f'q{i}'] == 'none' else address_term(answers[f'q{i}']) for i in range(len(items))]


def _configured_port():
    from runtime.reply.jev_questions import configured_questions
    return configured_questions()


async def run_daily_batch(store, now, *, port_factory=None, timeout=REQUEST_TIMEOUT_SECONDS):
    """At most one request per profile and local day, whatever its outcome.

    Returns 'done_today' (already requested), 'idle' (nothing waits), 'unconfigured', 'unavailable' or 'judged'.
    """
    today = now.astimezone(SHANGHAI).date().isoformat()
    if store.setting(DAY_KEY) == today:
        return 'done_today'
    if store.setting(LEGACY_KEY) != '1':
        # Stored quotes from before the judgment take their turn in the same daily request, newest first.
        store.queue_legacy_addressing()
        store.set_setting(LEGACY_KEY, '1')
    pending = store.pending_addressing(MAX_ITEMS)
    recent = store.recent_addressing_messages(store.setting(SCAN_KEY), SCAN_EXCHANGES)
    scanned = []
    for source_id, stamp, user_text, reply_text in recent:
        for kind, text in (('user_calls_linli', user_text), ('linli_calls_user', reply_text)):
            text = normalize(text or '')
            if text and len(text) <= QUOTE_MAX and candidate_spans(text):
                scanned.append((source_id, stamp, kind, text))
    if not pending and not scanned:
        if recent:
            store.set_setting(SCAN_KEY, recent[0][1])
        return 'idle'
    port = (port_factory or _configured_port)()
    if port is None:
        return 'unconfigured'
    store.set_setting(DAY_KEY, today)
    items = ([(kind, quote, store.addressing_context(kind, quote)) for kind, _key, quote in pending]
             + [(kind, text, text) for _source, _stamp, kind, text in scanned])
    try:
        terms = await ask(port, items, timeout=timeout)
    except AddressingJudgmentUnavailable:
        return 'unavailable'
    store.settle_addressing_judgments([(kind, key, term) for (kind, key, _quote), term in zip(pending, terms)])
    store.record_judged_addressing([(source, kind, term, stamp) for (source, stamp, kind, _text), term
                                    in zip(scanned, terms[len(pending):]) if term])
    if recent:
        store.set_setting(SCAN_KEY, recent[0][1])
    return 'judged'
