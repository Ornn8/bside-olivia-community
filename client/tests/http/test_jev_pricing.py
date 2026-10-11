from decimal import Decimal

import pytest

from runtime.diagnostics.jev_pricing import quote_input_cny


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
