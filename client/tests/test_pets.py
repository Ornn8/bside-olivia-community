"""Cats: request bounds, cloud response shape, and what she knows in a reply."""
import json
from datetime import datetime, timezone

import pytest

from runtime.pets import context, validate_pets, validate_request

PET = {'breed': 'orange-tabby', 'name': '团子', 'breed_name': '橘猫', 'personality': 'greedy', 'personality_name': '贪吃',
       'stage': 2, 'stage_name': '半大的猫', 'days': 9, 'next_stage_in': 19, 'build': 'chubby', 'build_name': '圆滚滚',
       'feeds_left_today': 1, 'adopted': 1791400000.0, 'room': '厨房'}
STATE = {'breeds': [{'id': 'orange-tabby', 'name': '橘猫', 'summary': '能吃能睡。', 'stages': ['幼猫', '半大的猫', '成年猫']}],
         'items': [{'id': 'cat-tree', 'kind': 'home', 'name': '猫爬架', 'summary': '三层。', 'price_cents': 990, 'owned': True},
                   {'id': 'bell-collar', 'kind': 'wear', 'name': '铃铛项圈', 'summary': '红色。', 'price_cents': 490, 'owned': False}],
         'pets': [PET], 'adopt_cents': 990, 'max_pets': 3, 'feeds_per_day': 2,
         'balance': {'balance_cents': 1234}, 'fed': True, 'unknown': 'dropped'}


def test_four_stages_three_builds_and_preset_pictures_are_kept():
    adult = {**PET, 'stage': 4, 'stage_name': '成年猫', 'next_stage_in': None, 'build': 'slim', 'build_name': '偏瘦',
             'image': 'orange-tabby-4-slim'}
    breed = {**STATE['breeds'][0], 'stages': ['奶猫', '幼猫', '半大的猫', '成年猫'], 'image': 'orange-tabby-4-normal'}
    value = validate_pets({**STATE, 'breeds': [breed], 'pets': [adult]})
    assert value['pets'][0]['image'] == 'orange-tabby-4-slim' and value['breeds'][0]['image'] == 'orange-tabby-4-normal'
    for bad in ({**adult, 'image': '../x'}, {**adult, 'stage': 7}):
        with pytest.raises(ValueError):
            validate_pets({**STATE, 'pets': [bad]})


def test_only_named_adoptions_free_feeds_and_single_items_are_sent():
    assert validate_request({'action': 'adopt', 'breed': 'orange-tabby', 'name': '团子'})['name'] == '团子'
    assert validate_request({'action': 'feed', 'breed': 'orange-tabby'})['action'] == 'feed'
    assert validate_request({'action': 'item', 'item': 'cat-tree'})['item'] == 'cat-tree'
    for bad in [{'action': 'adopt', 'breed': 'orange-tabby', 'name': ''}, {'action': 'adopt', 'breed': 'orange-tabby', 'name': ' 团子'},
                {'action': 'adopt', 'breed': 'orange-tabby', 'name': '<b>x</b>'}, {'action': 'food', 'species': 'cat', 'request_id': 'food-abc12345'},
                {'action': 'feed', 'breed': '../x'}, {'action': 'item', 'item': 'cat tree'},
                {'action': 'adopt', 'breed': 'orange-tabby', 'name': '团子', 'extra': 1}]:
        with pytest.raises(ValueError):
            validate_request(bad)


def test_cloud_state_is_bounded_and_unknown_fields_are_dropped():
    value = validate_pets(STATE)
    assert value['pets'] == [PET] and value['balance_cents'] == 1234 and value['fed'] is True
    assert value['items'][0]['owned'] and 'unknown' not in value
    with pytest.raises(ValueError):
        validate_pets({**STATE, 'pets': [{**PET, 'build': 'huge'}]})
    with pytest.raises(ValueError):
        validate_pets({**STATE, 'items': [{**STATE['items'][0], 'kind': 'toy'}]})


def test_she_knows_her_cats_their_character_build_and_things(tmp_path):
    from runtime.diary.diary import DiaryStore
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    now = datetime(2026, 10, 8, 4, tzinfo=timezone.utc)
    assert context([]) is None
    store.remember_pets([PET], now, ['猫爬架'])
    store.remember_pets([{**PET, 'build': 'normal', 'build_name': '匀称'}], datetime(2026, 10, 12, tzinfo=timezone.utc), ['猫爬架'])
    [kept] = store.pets()
    assert kept['adopted_on'] == '2026-10-08' and kept['build'] == 'normal'
    packet = json.loads(store.context(now=datetime(2026, 10, 12, 4, tzinfo=timezone.utc)))
    assert packet['pets_from_user'] == [{'name': '团子', 'kind': '橘猫', 'stage': '半大的猫', 'character': '贪吃，一听到动静就往厨房跑',
                                         'build': '匀称', 'adopted_on': '2026-10-08', 'usually_in_room_now': '厨房'}]
    assert packet['pet_things_from_user'] == ['猫爬架']
