"""Gift cameras: the cloud catalog, what she owns, and how she knows about them."""
import re

_ID = re.compile(r'^[a-z0-9-]{3,40}$')


def validate_custom(value):
    if (not isinstance(value, dict) or set(value) != {'title'}
            or not isinstance(value['title'], str) or not 1 <= len(value['title'].strip()) <= 60):
        raise ValueError('GIFT_CUSTOM_INVALID')
    return {'title': value['title'].strip()}


def validate_purchase(value):
    if (not isinstance(value, dict) or set(value) - {'custom'} != {'item'}
            or not isinstance(value['item'], str) or not _ID.fullmatch(value['item'])):
        raise ValueError('GIFT_REQUEST_INVALID')
    result = {'item': value['item']}
    if 'custom' in value:
        result['custom'] = validate_custom(value['custom'])
    return result


def validate_gifts(result):
    """Accept only the bounded catalog shape; unknown fields are dropped, not trusted."""
    if not isinstance(result, dict) or not isinstance(result.get('cameras'), list) or len(result['cameras']) > 40:
        raise ValueError('GIFTS_RESPONSE_INVALID')
    cameras = []
    for item in result['cameras']:
        if (not isinstance(item, dict) or not isinstance(item.get('id'), str) or not _ID.fullmatch(item['id'])
                or not isinstance(item.get('name'), str) or not 1 <= len(item['name']) <= 80
                or type(item.get('year')) is not int or type(item.get('price_cents')) is not int
                or not 0 < item['price_cents'] <= 10000 or type(item.get('owned')) is not bool
                or not isinstance(item.get('summary'), str) or len(item['summary']) > 300
                or not isinstance(item.get('use'), str) or len(item['use']) > 300
                or not isinstance(item.get('specs'), list) or len(item['specs']) > 8
                or any(not isinstance(spec, str) or len(spec) > 80 for spec in item['specs'])):
            raise ValueError('GIFTS_RESPONSE_INVALID')
        cameras.append({key: item[key] for key in ('id', 'name', 'year', 'summary', 'specs', 'use', 'price_cents', 'owned')})
    balance = result.get('balance')
    value = {'cameras': cameras}
    if 'items' in result:
        if not isinstance(result['items'], list) or len(result['items']) > 100:
            raise ValueError('GIFTS_RESPONSE_INVALID')
        value['items'] = []
        for item in result['items']:
            if (not isinstance(item, dict) or not isinstance(item.get('id'), str) or not _ID.fullmatch(item['id'])
                    or item.get('category') not in {'home', 'jewelry', 'music', 'consumable'}
                    or any(not isinstance(item.get(key), str) or not 1 <= len(item[key]) <= limit
                           for key, limit in (('name', 80), ('summary', 300), ('use', 300)))
                    or type(item.get('price_cents')) is not int or not 0 <= item['price_cents'] <= 9007199254740991
                    or any(type(item.get(key)) is not bool for key in ('owned', 'repeatable', 'customizable'))):
                raise ValueError('GIFTS_RESPONSE_INVALID')
            custom = validate_custom(item['custom']) if item.get('custom') is not None else None
            value['items'].append({**{key: item[key] for key in (
                'id', 'category', 'name', 'summary', 'use', 'price_cents', 'owned', 'repeatable', 'customizable')},
                'custom': custom})
    if isinstance(balance, dict) and type(balance.get('balance_cents')) is int:
        value['balance_cents'] = balance['balance_cents']
    if type(result.get('charged_cents')) is int:
        value.update(charged_cents=result['charged_cents'], item=result.get('item'),
                     already_owned=result.get('already_owned') is True)
    return value
