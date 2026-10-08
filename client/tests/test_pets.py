"""Pets: request bounds, cloud response shape, and what she knows in a reply."""
import json
from datetime import datetime, timezone

import pytest

from runtime.pets import context, validate_pets, validate_request

PET = {'breed': 'corgi', 'name': '团子', 'species': 'dog', 'breed_name': '柯基', 'stage': 2, 'stage_name': '半大的狗',
       'fed_days': 9, 'next_stage_at': 28, 'food_days': 1, 'adopted': 1791400000.0, 'room': '客厅'}
STATE = {'breeds': [{'id': 'corgi', 'species': 'dog', 'name': '柯基', 'summary': '短腿。', 'stages': ['幼犬', '半大的狗', '成年狗']}],
         'pets': [PET], 'foods': {'cat': '猫粮', 'dog': '狗粮'}, 'adopt_cents': 990, 'food_cents': 200, 'bag_days': 7,
         'stage_at': [0, 7, 28], 'balance': {'balance_cents': 1234}, 'charged_cents': 200, 'unknown': 'dropped'}


def test_only_named_adoptions_and_single_food_bags_are_sent():
    assert validate_request({'action': 'adopt', 'breed': 'corgi', 'name': '团子'})['name'] == '团子'
    assert validate_request({'action': 'food', 'species': 'cat', 'request_id': 'food-abc12345'})['species'] == 'cat'
    for bad in [{'action': 'adopt', 'breed': 'corgi', 'name': ''}, {'action': 'adopt', 'breed': 'corgi', 'name': ' 团子'},
                {'action': 'adopt', 'breed': 'corgi', 'name': '<b>x</b>'}, {'action': 'food', 'species': 'fish', 'request_id': 'food-abc12345'},
                {'action': 'food', 'species': 'cat', 'request_id': 'x'}, {'action': 'adopt', 'breed': 'corgi', 'name': '团子', 'extra': 1}]:
        with pytest.raises(ValueError):
            validate_request(bad)


def test_cloud_state_is_bounded_and_unknown_fields_are_dropped():
    value = validate_pets(STATE)
    assert value['pets'] == [PET] and value['balance_cents'] == 1234 and value['charged_cents'] == 200
    assert 'unknown' not in value and 'stage_at' not in value
    with pytest.raises(ValueError):
        validate_pets({**STATE, 'pets': [{**PET, 'stage': 4}]})
    with pytest.raises(ValueError):
        validate_pets({**STATE, 'foods': {'cat': '猫粮'}})


def test_she_knows_her_pets_and_food_without_being_told_to_nag(tmp_path):
    from runtime.diary.diary import DiaryStore
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    now = datetime(2026, 10, 8, 4, tzinfo=timezone.utc)
    assert context([]) is None
    store.remember_pets([PET], now)
    store.remember_pets([{**PET, 'food_days': 0}], datetime(2026, 10, 12, tzinfo=timezone.utc))
    [kept] = store.pets()
    assert kept['adopted_on'] == '2026-10-08' and kept['food_days'] == 0
    packet = json.loads(store.context(now=datetime(2026, 10, 12, 4, tzinfo=timezone.utc)))
    assert packet['pets_from_user'] == [{'name': '团子', 'kind': '柯基', 'stage': '半大的狗', 'food_days_left': 0,
                                         'adopted_on': '2026-10-08', 'usually_in_room_now': '客厅'}]
    assert '不催' in packet['pets_meaning']
