"""Pets the user adopts for her: the cloud state, requests, and how she knows about them."""
import re

_ID = re.compile(r'^[a-z0-9-]{3,40}$')
_REQUEST = re.compile(r'^[A-Za-z0-9_-]{8,64}$')
_NAME = re.compile(r'^[^\x00-\x1f<>{}\[\]\\`"]{1,8}$')
SPECIES = ('cat',)  # she only likes cats


def validate_request(data):
    """Only an adoption with a name or one bag of food leaves this computer."""
    if (isinstance(data, dict) and set(data) == {'action', 'breed', 'name'} and data['action'] == 'adopt'
            and isinstance(data['breed'], str) and _ID.fullmatch(data['breed'])
            and isinstance(data['name'], str) and _NAME.fullmatch(data['name']) and data['name'] == data['name'].strip()):
        return dict(data)
    if (isinstance(data, dict) and set(data) == {'action', 'species', 'request_id'} and data['action'] == 'food'
            and data['species'] in SPECIES and isinstance(data['request_id'], str) and _REQUEST.fullmatch(data['request_id'])):
        return dict(data)
    raise ValueError('PET_REQUEST_INVALID')


def _text(value, limit):
    return isinstance(value, str) and 1 <= len(value) <= limit


def validate_pets(result):
    """Accept only the bounded shape; unknown fields are dropped, not trusted."""
    try:
        breeds = []
        for item in result['breeds']:
            if (not _ID.fullmatch(item['id']) or item['species'] not in SPECIES or not _text(item['name'], 20)
                    or not _text(item['summary'], 120) or len(item['stages']) != 3
                    or any(not _text(stage, 12) for stage in item['stages'])):
                raise ValueError()
            breeds.append({key: item[key] for key in ('id', 'species', 'name', 'summary', 'stages')})
        pets = []
        for pet in result['pets']:
            if (not _ID.fullmatch(pet['breed']) or pet['species'] not in SPECIES or not _NAME.fullmatch(pet['name'])
                    or not _text(pet['breed_name'], 20) or pet['stage'] not in (1, 2, 3) or not _text(pet['stage_name'], 12)
                    or type(pet['fed_days']) is not int or type(pet['food_days']) is not int
                    or not (pet['next_stage_at'] is None or type(pet['next_stage_at']) is int)
                    or not isinstance(pet['adopted'], (int, float)) or not _text(pet.get('room'), 8)):
                raise ValueError()
            pets.append({key: pet[key] for key in ('breed', 'name', 'species', 'breed_name', 'stage', 'stage_name',
                                                   'fed_days', 'next_stage_at', 'food_days', 'adopted', 'room')})
        foods = result['foods']
        if set(foods) != set(SPECIES) or any(not _text(name, 8) for name in foods.values()):
            raise ValueError()
        prices = [result[key] for key in ('adopt_cents', 'food_cents', 'bag_days')]
        if any(type(value) is not int or not 0 < value <= 10000 for value in prices) or len(breeds) > 30 or len(pets) > 30:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ValueError('PETS_RESPONSE_INVALID') from None
    value = {'breeds': breeds, 'pets': pets, 'foods': dict(foods), 'adopt_cents': result['adopt_cents'],
             'food_cents': result['food_cents'], 'bag_days': result['bag_days']}
    balance = result.get('balance')
    if isinstance(balance, dict) and type(balance.get('balance_cents')) is int:
        value['balance_cents'] = balance['balance_cents']
    if type(result.get('charged_cents')) is int:
        value['charged_cents'] = result['charged_cents']
    return value


def context(pets):
    """What she knows about her pets in a reply: names, ages and how much food is left."""
    if not pets:
        return None
    return {'pets_from_user': [{'name': p['name'], 'kind': p['breed_name'], 'stage': p['stage_name'],
                                'food_days_left': p['food_days'], 'adopted_on': p.get('adopted_on'),
                                **({'usually_in_room_now': p['room']} if p.get('room') else {})} for p in pets],
            'pets_meaning': ('对方送你领养的宠物，你在家养着、每天喂它。usually_in_room_now 是它这会儿自己待着的房间（几小时前刷新），'
                             '它也常跑来你身边；你出门时它在家等你。'
                             'food_days_left 是粮还够吃几天：快吃完时可以像平常过日子那样顺口提一句，'
                             '不催对方买、不反复提，也不说它饿着或不开心——没粮时它只是长得慢一点。')}
