import pytest

from runtime.personal_chat.backfill import status


def test_default_off_and_requested_replay_is_explicitly_unavailable():
    assert status({}) == {'enabled': False, 'status': 'DISABLED', 'error_code': None}
    assert status({'backfill_enabled': True}) == {
        'enabled': True, 'status': 'UNAVAILABLE', 'error_code': 'QQ_BACKFILL_UNAVAILABLE'}


@pytest.mark.parametrize('value', [1, 0, 'true', None, []])
def test_backfill_switch_is_a_boolean(value):
    with pytest.raises(ValueError, match='QQ_BACKFILL_CONFIG_INVALID'):
        status({'backfill_enabled': value})
