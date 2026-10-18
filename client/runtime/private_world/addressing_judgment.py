"""One small judgment a day picks the term of address out of the longer quotes seen since the last one.

A longer quote ("早安呀饭饭", "外边下雨了小猫咪") may hold a greeting or a clause around the name. The shape check
(addressing.address_term) is only the fallback: it applies at once and whenever the judgment is not available.
The judgment is never on the reply path: it runs with the relationship batch (relationship_batch.tick), which
follows its own cadence. Each distinct quote is judged once and the result is kept.
"""
import asyncio
from datetime import timedelta, timezone
import re

from .addressing import address_term

PURPOSE = 'addressing-term'
INSTRUCTIONS = ('从 message 里找出说话的人用来称呼对方的那个称呼：昵称、爱称、名字或亲昵的叫法。'
                '只选称呼本身，不带问候语、天气、动作或句子的其他部分；如果这句话里没有称呼对方，选 none。')
SPEAKER = {'user_calls_linli': 'user', 'linli_calls_user': 'her'}
# One request a day carries at most this many questions (one per distinct quote); the rest wait for the next day.
MAX_ITEMS = 20
SPAN_MAX = 6      # a term is picked from the first and last characters of the quote, this many at most
WHOLE_MAX = 8     # a quote of up to this many characters may also be the term as a whole
REQUEST_TIMEOUT_SECONDS = 25
SHANGHAI = timezone(timedelta(hours=8))
DAY_KEY = 'addressing_batch_day'
LEGACY_KEY = 'addressing_legacy_queued'
_WORDS = re.compile(r'[\w一-鿿]+')


class AddressingJudgmentUnavailable(Exception):
    """The judgment could not be had; callers keep the shape check."""


def candidate_spans(quote):
    """Every prefix and suffix of the quote up to SPAN_MAX characters, and the whole quote when it is short."""
    spans = []
    for size in range(1, min(SPAN_MAX, len(quote)) + 1):
        spans += [quote[:size], quote[-size:]]
    if len(quote) <= WHOLE_MAX:
        spans.append(quote)
    # Only word characters can be a term, and 'none' is the answer for no term.
    return [span for span in dict.fromkeys(spans) if span != 'none' and _WORDS.fullmatch(span)]


def build_request(items):
    """items: [(kind, quote)] -> (state, questions), one question per item, in order."""
    state = {'items': []}
    questions = {}
    for index, (kind, quote) in enumerate(items):
        key = f'q{index}'
        state['items'].append({'id': key, 'speaker': SPEAKER[kind], 'message': quote})
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
    if not pending:
        return 'idle'
    port = (port_factory or _configured_port)()
    if port is None:
        return 'unconfigured'
    store.set_setting(DAY_KEY, today)
    try:
        terms = await ask(port, [(kind, quote) for kind, _key, quote in pending], timeout=timeout)
    except AddressingJudgmentUnavailable:
        return 'unavailable'
    store.settle_addressing_judgments([(kind, key, term) for (kind, key, _quote), term in zip(pending, terms)])
    return 'judged'
