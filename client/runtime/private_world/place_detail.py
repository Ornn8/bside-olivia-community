"""A named place and its arrival state, independent of media asset catalogs."""
from jsonschema import Draft202012Validator, ValidationError

SCHEMA = {'type': 'object', 'additionalProperties': False,
    'required': ['place_id', 'name', 'setting', 'stage'], 'properties': {
        'place_id': {'enum': ['home', 'campus', 'neighborhood', 'shop']},
        'name': {'type': 'string', 'minLength': 1, 'maxLength': 40},
        'setting': {'type': 'string', 'minLength': 1, 'maxLength': 160},
        'stage': {'enum': ['preparing', 'travelling', 'arrived']}}}


def validate(value):
    try:
        Draft202012Validator(SCHEMA).validate(value)
        if any(not value[k].strip() or any(ord(c) < 32 for c in value[k]) for k in ('name', 'setting')):
            raise ValueError()
    except (ValidationError, TypeError, ValueError):
        raise ValueError('DAILY_LIFE_PLACE_INVALID') from None
    return dict(value)


def label(value):
    value = validate(value)
    return {'preparing': '准备前往', 'travelling': '前往', 'arrived': ''}[value['stage']] + value['name'] + (
        '的路上' if value['stage'] == 'travelling' else '')


def activity_place(kind, place_id):
    """Precise routine rooms; no invented shop identity for old generic events."""
    if kind in ('bath_started', 'bath_finished'):
        name = '浴室'
    elif kind == 'rest' and place_id == 'home':
        name = '卧室'
    elif kind == 'practice':
        name = '家里音乐房' if place_id == 'home' else '学校琴房'
    elif kind == 'class':
        name = '学校教室'
    else:
        return None
    return dict(place_id=place_id, name=name, setting=name+'，沿用已有布局与陈设。', stage='arrived')
