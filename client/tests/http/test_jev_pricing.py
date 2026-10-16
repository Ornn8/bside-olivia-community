from decimal import Decimal

import pytest

from runtime.diagnostics import jev_pricing
from runtime.diagnostics.jev_pricing import quote_input_cny, settlement_rates


@pytest.mark.parametrize('tokens,expected', [
    (0, '0'), (1, '0.00000169'), (10000, '0.0169'),
    (67075, '0.11335675'), (1000000, '1.69'),
])
def test_exact_turn_price_without_minimum_charge(tokens, expected):
    assert quote_input_cny(tokens) == Decimal(expected)


@pytest.mark.parametrize('unknown_or_invalid', [None, True, -1, 1.5, '10000'])
def test_unknown_usage_cannot_be_charged_as_zero(unknown_or_invalid):
    with pytest.raises(ValueError):
        quote_input_cny(unknown_or_invalid)


FIRST_BATCH = {'quality-review', 'quality-confirm', 'history-selection', 'reply-world-selection',
               'character-emotion', 'exchange-facts', 'exchange-anchored-facts', 'experience-appraisal',
               'memory-extraction', 'reply_photo_plan', 'world-meal-lifecycle', 'letter-media-request',
               'web-search-decision'}


def test_declared_version_lists_its_purpose_rate():
    rate, purposes = jev_pricing.PURPOSE_RATE_UNITS[jev_pricing.CURRENT_PRICE_VERSION]
    assert jev_pricing.CURRENT_PRICE_VERSION == 'jev-input-cny-20261016-v5'
    assert (rate, set(purposes)) == (101, FIRST_BATCH)
    assert rate <= jev_pricing.INPUT_RATE_UNITS[jev_pricing.SETTLEMENT_V3] * 0.6


@pytest.mark.parametrize('version,purpose,rates', [
    ('jev-input-cny-20261016-v5', 'quality-review', {101}),
    ('jev-input-cny-20261016-v5', 'world-decision', {101, 169}),
    ('jev-input-cny-20261016-v5', None, {101, 169}),
    ('jev-input-cny-20261016-v5', ['quality-review'], {101, 169}),
    ('jev-input-cny-20261012-v3', 'quality-review', {169}),
    ('jev-input-cny-20261002-v2', 'quality-review', {135}),
    ('jev-input-cny-20260928-v1', 'quality-review', {150}),
    ('jev-input-cny-20261013-v4', 'quality-review', set()),
    (None, 'quality-review', set()),
    (['jev-input-cny-20261016-v5'], 'quality-review', set()),
])
def test_settlement_rates_by_version_and_purpose(version, purpose, rates):
    assert settlement_rates(version, purpose) == rates


@pytest.mark.parametrize('tokens,purpose,expected', [
    (10000, 'quality-review', '0.0101'), (1000000, 'memory-extraction', '1.01'),
    (10000, 'world-decision', '0.0169'), (10000, None, '0.0169'),
])
def test_quote_follows_the_purpose_rate(tokens, purpose, expected):
    assert quote_input_cny(tokens, purpose) == Decimal(expected)
