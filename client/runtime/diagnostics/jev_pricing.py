"""Approved JEV retail quote; this module never debits an account."""
from decimal import Decimal

PRICE_VERSION = 'jev-input-cny-20260928-v1'  # version of the signed usage receipt
# Settlement price versions this client verifies. It declares the newest one;
# the server charges that rate (2026-10-02: 10% lower) and reports the version.
DISCOUNT_PRICE_VERSION = 'jev-input-cny-20261002-v2'
# 2026-10-12: settlement version v3. Clients that do not declare it keep their version.
SETTLEMENT_V3 = 'jev-input-cny-20261012-v3'
# 2026-10-13: settlement version v4, the v3 rate with its own per-judgment floor.
CURRENT_PRICE_VERSION = 'jev-input-cny-20261013-v4'
INPUT_RATE_UNITS = {PRICE_VERSION: 150, DISCOUNT_PRICE_VERSION: 135,
                    SETTLEMENT_V3: 169, CURRENT_PRICE_VERSION: 169}  # 1 yuan = 100,000,000 units
# Smallest charge for a judgment whose usage is above zero; versions not listed use 1_000_000.
MINIMUM_UNITS = {CURRENT_PRICE_VERSION: 2_000_000}
INPUT_CNY_PER_MILLION = Decimal('1.69')
OUTPUT_CNY_PER_MILLION = Decimal('0')


def quote_input_cny(input_tokens: int) -> Decimal:
    """Quote an already deduplicated turn total, without per-attempt rounding.

    Unknown usage must remain unknown. Account attribution, eligible receipts,
    and idempotent wallet settlement are separate requirements, not inferred
    from the provider diagnostic log.
    """
    if type(input_tokens) is not int or input_tokens < 0:
        raise ValueError('JEV_INPUT_USAGE_INVALID')
    return Decimal(input_tokens) * INPUT_CNY_PER_MILLION / Decimal(1_000_000)
