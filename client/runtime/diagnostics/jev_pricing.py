"""Approved JEV retail quote; this module never debits an account."""
from decimal import Decimal

PRICE_VERSION = 'jev-input-cny-20260928-v1'  # version of the signed usage receipt
# Settlement price versions this client verifies. It declares the newest one;
# the server charges that rate (2026-10-02: 10% lower) and reports the version.
DISCOUNT_PRICE_VERSION = 'jev-input-cny-20261002-v2'
# 2026-10-12: settlement version v3. Clients that do not declare it keep their version.
SETTLEMENT_V3 = 'jev-input-cny-20261012-v3'
# 2026-10-16: settlement version v5. Its rate also depends on the judgment's purpose.
CURRENT_PRICE_VERSION = 'jev-input-cny-20261016-v5'
INPUT_RATE_UNITS = {PRICE_VERSION: 150, DISCOUNT_PRICE_VERSION: 135,
                    SETTLEMENT_V3: 169, CURRENT_PRICE_VERSION: 169}  # 1 yuan = 100,000,000 units
# Purpose rate inside a settlement version: (rate, purposes that must settle at it).
# Any other purpose may settle at the version rate or at this rate, so the service can
# move more purposes to it without a client update; a listed purpose never settles higher.
PURPOSE_RATE_UNITS = {CURRENT_PRICE_VERSION: (101, frozenset({
    'quality-review', 'quality-confirm', 'history-selection', 'reply-world-selection',
    'character-emotion', 'exchange-facts', 'exchange-anchored-facts', 'experience-appraisal',
    'memory-extraction', 'reply_photo_plan', 'world-meal-lifecycle', 'letter-media-request',
    'web-search-decision'}))}
INPUT_CNY_PER_MILLION = Decimal('1.69')
OUTPUT_CNY_PER_MILLION = Decimal('0')


def settlement_rates(version, purpose):
    """Rates (units per input token) a judgment of `purpose` may settle at under `version`.

    Empty for a version this client does not know, so such a settlement is rejected.
    """
    rate = INPUT_RATE_UNITS.get(version) if isinstance(version, str) else None
    if rate is None:
        return frozenset()
    if version not in PURPOSE_RATE_UNITS:
        return frozenset({rate})
    purpose_rate, purposes = PURPOSE_RATE_UNITS[version]
    if isinstance(purpose, str) and purpose in purposes:
        return frozenset({purpose_rate})
    return frozenset({rate, purpose_rate})


def quote_input_cny(input_tokens: int, purpose: str | None = None) -> Decimal:
    """Quote an already deduplicated turn total, without per-attempt rounding.

    Unknown usage must remain unknown. Account attribution, eligible receipts,
    and idempotent wallet settlement are separate requirements, not inferred
    from the provider diagnostic log. A listed purpose is quoted at its own rate.
    """
    if type(input_tokens) is not int or input_tokens < 0:
        raise ValueError('JEV_INPUT_USAGE_INVALID')
    rate, purposes = PURPOSE_RATE_UNITS[CURRENT_PRICE_VERSION]
    if not (isinstance(purpose, str) and purpose in purposes):
        return Decimal(input_tokens) * INPUT_CNY_PER_MILLION / Decimal(1_000_000)
    return Decimal(input_tokens * rate) / Decimal(100_000_000)
