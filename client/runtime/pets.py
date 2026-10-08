"""Cats the user adopts for her: the cloud state, requests, and how she knows about them."""
import re

_ID = re.compile(r'^[a-z0-9-]{3,40}$')
_NAME = re.compile(r'^[^\x00-\x1f<>{}\[\]\\`"]{1,8}$')
BUILDS = ('slim', 'normal', 'chubby')
PERSONALITY_TEXT = {'clingy': '很黏你，走到哪跟到哪', 'aloof': '有点高冷，喜欢远远看着你，不太让抱',
                    'playful': '调皮好动，爱钻爱扑', 'greedy': '贪吃，一听到动静就往厨房跑'}


def validate_request(data):
    """Only an adoption with a name, a free feed, or one item leaves this computer."""
    if not isinstance(data, dict):
        raise ValueError('PET_REQUEST_INVALID')
    if (set(data) == {'action', 'breed', 'name'} and data['action'] == 'adopt'
            and isinstance(data['breed'], str) and _ID.fullmatch(data['breed'])
            and isinstance(data['name'], str) and _NAME.fullmatch(data['name']) and data['name'] == data['name'].strip()):
        return dict(data)
    if (set(data) == {'action', 'breed'} and data['action'] == 'feed'
            and isinstance(data['breed'], str) and _ID.fullmatch(data['breed'])):
        return dict(data)
    if (set(data) == {'action', 'item'} and data['action'] == 'item'
            and isinstance(data['item'], str) and _ID.fullmatch(data['item'])):
        return dict(data)
    raise ValueError('PET_REQUEST_INVALID')


def _text(value, limit):
    return isinstance(value, str) and 1 <= len(value) <= limit


def _image(value):
    """The preset picture id of a stage and build (optional for older services)."""
    return value is None or isinstance(value, str) and bool(_ID.fullmatch(value))


def _count(value, limit):
    return type(value) is int and 0 <= value <= limit


def validate_pets(result):
    """Accept only the bounded shape; unknown fields are dropped, not trusted."""
    try:
        breeds = []
        for item in result['breeds']:
            if (not _ID.fullmatch(item['id']) or not _text(item['name'], 20) or not _text(item['summary'], 120)
                    or not 3 <= len(item['stages']) <= 6 or any(not _text(stage, 12) for stage in item['stages'])
                    or not _image(item.get('image'))):
                raise ValueError()
            breeds.append({key: item[key] for key in ('id', 'name', 'summary', 'stages', 'image') if key in item})
        items = []
        for item in result['items']:
            if (not _ID.fullmatch(item['id']) or item['kind'] not in ('wear', 'home') or not _text(item['name'], 20)
                    or not _text(item['summary'], 120) or not _count(item['price_cents'], 10000) or not item['price_cents']
                    or type(item['owned']) is not bool):
                raise ValueError()
            items.append({key: item[key] for key in ('id', 'kind', 'name', 'summary', 'price_cents', 'owned')})
        pets = []
        for pet in result['pets']:
            if (not _ID.fullmatch(pet['breed']) or not _NAME.fullmatch(pet['name']) or not _text(pet['breed_name'], 20)
                    or pet['personality'] not in PERSONALITY_TEXT or not _text(pet['personality_name'], 8)
                    or pet['stage'] not in range(1, 7) or not _text(pet['stage_name'], 12) or not _count(pet['days'], 100000)
                    or not (pet['next_stage_in'] is None or _count(pet['next_stage_in'], 100))
                    or pet['build'] not in BUILDS or not _text(pet['build_name'], 8) or not _image(pet.get('image'))
                    or not _count(pet['feeds_left_today'], 10) or not isinstance(pet['adopted'], (int, float))
                    or not _text(pet['room'], 8)):
                raise ValueError()
            pets.append({key: pet[key] for key in ('breed', 'name', 'breed_name', 'personality', 'personality_name',
                                                   'stage', 'stage_name', 'days', 'next_stage_in', 'build', 'build_name',
                                                   'feeds_left_today', 'adopted', 'room', 'image') if key in pet})
        limits = [result[key] for key in ('adopt_cents', 'max_pets', 'feeds_per_day')]
        if (any(not _count(value, 10000) or not value for value in limits)
                or len(breeds) > 30 or len(items) > 30 or len(pets) > 10):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ValueError('PETS_RESPONSE_INVALID') from None
    value = {'breeds': breeds, 'items': items, 'pets': pets, 'adopt_cents': result['adopt_cents'],
             'max_pets': result['max_pets'], 'feeds_per_day': result['feeds_per_day']}
    balance = result.get('balance')
    if isinstance(balance, dict) and type(balance.get('balance_cents')) is int:
        value['balance_cents'] = balance['balance_cents']
    if type(result.get('charged_cents')) is int:
        value['charged_cents'] = result['charged_cents']
    if type(result.get('fed')) is bool:
        value['fed'] = result['fed']
    return value


def context(pets, items=()):
    """What she knows about her cats in a reply: names, age, character, build, things they have."""
    if not pets:
        return None
    return {'pets_from_user': [{'name': p['name'], 'kind': p['breed_name'], 'stage': p['stage_name'],
                                'character': PERSONALITY_TEXT.get(p.get('personality'), ''),
                                'build': p.get('build_name'), 'adopted_on': p.get('adopted_on'),
                                **({'usually_in_room_now': p['room']} if p.get('room') else {})} for p in pets],
            **({'pet_things_from_user': list(items)} if items else {}),
            'pets_meaning': ('对方送你领养的猫，你在家养着、每天喂它们。character 是它的性格，build 是它现在的体态'
                             '（成年后才看得出：对方常让你多喂几次会圆滚滚，只有你每天喂一顿、对方不喂就会偏瘦）。usually_in_room_now 是它这会儿自己待着的房间（几小时前刷新），'
                             '它也常跑来你身边；你出门时它在家等你。pet_things_from_user 是对方给猫买的东西，都在家里用着。')}
