"""Fixed wardrobe styles shared by photo planning and video scene selection."""
from datetime import date
import re

# Keep each catalog's exact allowlist during rolling client/server upgrades.
DAILY_LOOK_COUNTS = {'mori': 14, 'rebellious': 1, 'dark': 1, 'cargo': 1,
                     'tie-shorts': 1, 'french': 8, 'earth': 3, 'japanese': 4,
                     'sweet': 6, 'doll': 8}
PREVIOUS_DAILY_COUNTS = dict(DAILY_LOOK_COUNTS)
DAILY_LOOK_COUNTS.update(dark=4, french=9, japanese=6, doll=10, fantasy=3)
DAILY_STYLES = ('original', *DAILY_LOOK_COUNTS)
LEGACY_DAILY_STYLES = DAILY_STYLES[:6]
DAILY_LOOKS = {style: frozenset(style + f'-{i:02}' for i in range(1, count + 1))
               for style, count in DAILY_LOOK_COUNTS.items()}
PREVIOUS_DAILY_CATALOG = '20261004-e94ffc6dd51b'
DAILY_CATALOG = '20261008-400d35c64251'
CLOUD_CATALOG_PROTOCOL = 'dynamic-v1'
DAILY_CATALOGS = {
    PREVIOUS_DAILY_CATALOG: {style: frozenset(style + f'-{i:02}' for i in range(1, count + 1))
                             for style, count in PREVIOUS_DAILY_COUNTS.items()},
    DAILY_CATALOG: DAILY_LOOKS,
}


def valid_style_id(value):
    return isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', value) is not None and len(value) <= 40


def valid_look_id(value):
    return isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9-]*-[0-9]{2,4}', value) is not None and len(value) <= 80


def valid_catalog_version(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9]{8}-[a-f0-9]{12}', value) is not None


def validate_daily_outfit(value):
    if value is None:
        return None
    keys={'date','timezone','preference_revision','style_id','look_id','catalog_version','reference_sha256'}
    if not isinstance(value,dict) or set(value)!=keys:
        raise ValueError('WARDROBE_OUTFIT_INVALID')
    date.fromisoformat(value['date'])
    if (value['timezone']!='Asia/Shanghai' or type(value['preference_revision']) is not int
            or value['preference_revision']<1 or not valid_style_id(value['style_id']) or value['style_id']=='original'
            or not valid_catalog_version(value['catalog_version']) or not valid_look_id(value['look_id'])
            or value['look_id'].rsplit('-',1)[0] != value['style_id']
            or value['catalog_version'] in DAILY_CATALOGS and value['look_id'] not in DAILY_CATALOGS[value['catalog_version']].get(value['style_id'], ())
            or not isinstance(value['reference_sha256'],str) or not re.fullmatch(r'[a-f0-9]{64}',value['reference_sha256'])):
        raise ValueError('WARDROBE_OUTFIT_INVALID')
    return dict(value)


def validate_cloud_state(value):
    if isinstance(value, dict) and value.get('catalog_protocol') == CLOUD_CATALOG_PROTOCOL:
        return validate_dynamic_state(value)
    if (not isinstance(value,dict) or not isinstance(value.get('catalog_version'), str)
            or value['catalog_version'] not in DAILY_CATALOGS or value.get('timezone')!='Asia/Shanghai'):
        raise ValueError('WARDROBE_STATE_INVALID')
    catalog_looks = DAILY_CATALOGS[value['catalog_version']]
    catalog_styles = {'original', *catalog_looks}
    date.fromisoformat(value['date'])
    if 'purchases' in value:
        purchases = value['purchases']
        if (not isinstance(purchases,dict) or purchases.get('price_cents') != 500 or purchases.get('free_limit') != 3
                or type(purchases.get('free_remaining')) is not int or not 0 <= purchases['free_remaining'] <= 3
                or not isinstance(purchases.get('owned'),list)
                or any(not isinstance(item,str) or item not in set().union(*catalog_looks.values()) for item in purchases['owned'])
                or len(set(purchases['owned'])) != len(purchases['owned'])):
            raise ValueError('WARDROBE_STATE_INVALID')
    preference=value['wardrobe']
    if (not isinstance(preference,dict) or set(preference)!={'style_id','preference_revision'}
            or preference['style_id'] not in catalog_styles or type(preference['preference_revision']) is not int
            or preference['preference_revision']<0):
        raise ValueError('WARDROBE_STATE_INVALID')
    styles=value['wardrobe_styles']
    if (not isinstance(styles,list) or any(not isinstance(s,dict) for s in styles)
            or len(styles) not in (len(LEGACY_DAILY_STYLES), len(catalog_styles))
            or {s.get('style_id') for s in styles} not in (set(LEGACY_DAILY_STYLES), catalog_styles)
            or len({s.get('style_id') for s in styles}) != len(styles)
            or preference['style_id'] not in {s['style_id'] for s in styles}):
        raise ValueError('WARDROBE_STATE_INVALID')
    for style in styles:
        allowed_looks = catalog_looks.get(style['style_id'], frozenset())
        if not all(isinstance(style.get(k),str) and len(style[k])<=1000 for k in ('label','description')) or not isinstance(style.get('looks'),list) or len(style['looks'])>len(allowed_looks):
            raise ValueError('WARDROBE_STATE_INVALID')
        seen_looks = set()
        for look in style['looks']:
            if (not isinstance(look,dict) or not isinstance(look.get('look_id'),str)
                    or look['look_id'] not in allowed_looks or look['look_id'] in seen_looks
                    or not isinstance(look.get('label'),str) or len(look['label'])>200):
                raise ValueError('WARDROBE_STATE_INVALID')
            seen_looks.add(look['look_id'])
    outfit=validate_daily_outfit(value.get('daily_outfit'))
    if outfit and (outfit['look_id'] not in catalog_looks.get(outfit['style_id'], ())
                  or outfit['date']!=value['date'] or outfit['style_id']!=preference['style_id'] or outfit['preference_revision']!=preference['preference_revision']):
        raise ValueError('WARDROBE_STATE_INVALID')
    return value


def validate_dynamic_state(value):
    """Trust the authenticated server's catalog, never paths or arbitrary URLs."""
    from runtime.image_assets import validate_entry
    if not valid_catalog_version(value.get('catalog_version')) or value.get('timezone') != 'Asia/Shanghai':
        raise ValueError('WARDROBE_STATE_INVALID')
    date.fromisoformat(value['date'])
    styles = value.get('wardrobe_styles')
    if not isinstance(styles, list) or not 1 <= len(styles) <= 100:
        raise ValueError('WARDROBE_STATE_INVALID')
    style_ids, look_ids, hashes = set(), set(), {}
    for style in styles:
        if not isinstance(style, dict) or not valid_style_id(style.get('style_id')) or style['style_id'] in style_ids:
            raise ValueError('WARDROBE_STATE_INVALID')
        sid = style['style_id']; style_ids.add(sid)
        if (not all(isinstance(style.get(k), str) and len(style[k]) <= 1000 for k in ('label','description'))
                or not isinstance(style.get('looks'), list) or len(style['looks']) > 1000):
            raise ValueError('WARDROBE_STATE_INVALID')
        for look in style['looks']:
            if (not isinstance(look, dict) or not valid_look_id(look.get('look_id')) or sid == 'original'
                    or look['look_id'].rsplit('-',1)[0] != sid or look['look_id'] in look_ids
                    or not isinstance(look.get('label'), str) or not 1 <= len(look['label']) <= 200):
                raise ValueError('WARDROBE_STATE_INVALID')
            validate_entry('wardrobe', look['look_id'], look.get('image_asset'))
            look_ids.add(look['look_id'])
            hashes[look['look_id']] = look['image_asset']['sha256']
    if 'original' not in style_ids or len(look_ids) > 1000:
        raise ValueError('WARDROBE_STATE_INVALID')
    pref = value.get('wardrobe')
    if (not isinstance(pref, dict) or set(pref) != {'style_id','preference_revision'}
            or pref['style_id'] not in style_ids or type(pref['preference_revision']) is not int or pref['preference_revision'] < 0):
        raise ValueError('WARDROBE_STATE_INVALID')
    purchases = value.get('purchases')
    if purchases is not None and (not isinstance(purchases, dict) or purchases.get('price_cents') != 500
            or purchases.get('free_limit') != 3 or type(purchases.get('free_remaining')) is not int
            or not 0 <= purchases['free_remaining'] <= 3 or not isinstance(purchases.get('owned'), list)
            or any(not valid_look_id(lid) for lid in purchases['owned'])
            or len(set(purchases['owned'])) != len(purchases['owned']) or len(purchases['owned']) > 1000):
        raise ValueError('WARDROBE_STATE_INVALID')
    outfit = validate_daily_outfit(value.get('daily_outfit'))
    if outfit and (outfit['look_id'] not in look_ids or outfit['reference_sha256'] != hashes[outfit['look_id']] or outfit['style_id'] != pref['style_id']
            or outfit['preference_revision'] != pref['preference_revision'] or outfit['date'] != value['date']):
        raise ValueError('WARDROBE_STATE_INVALID')
    return value

CATALOG_VERSION = 1
DEFAULT_STYLE = 'original'
# Keep IDs stable: video services bind their approved motion assets to these IDs.
STYLES = {
    'original': {'label': '原版日常', 'description': '黑色贴身长袖、黑色短裤与铆钉腰带，保留原版造型。',
                 'pieces': ['黑色罗纹贴身长袖 · 微波浪边', '黑色牛仔短裤', '铆钉腰带 · 深色长靴'], 'colors': ['#252528', '#434046', '#82746b'],
                 'outfit': 'a fitted black ribbed long-sleeve top with subtly scalloped edges, black denim shorts, a studded belt and dark boots'},
    'home_knit': {'label': '居家针织', 'description': '炭灰色宽松针织衫、柔软长裤，轻松的居家穿着。',
                  'pieces': ['炭灰色宽松针织衫', '深色柔软居家长裤', '简洁室内拖鞋'], 'colors': ['#55534f', '#343537', '#a79b8b'],
                  'outfit': 'a relaxed charcoal knit sweater, soft dark full-length lounge trousers and simple house slippers'},
    'city_casual': {'label': '城市休闲', 'description': '深色短袖、蓝色直筒牛仔裤与轻便运动鞋。',
                    'pieces': ['深色贴身短袖 T 恤', '蓝色直筒牛仔裤', '轻便低帮运动鞋'], 'colors': ['#2c2d30', '#536b7b', '#c2bcae'],
                    'outfit': 'a fitted dark short-sleeve T-shirt, straight-leg blue jeans and simple low-top sneakers'},
    'rehearsal': {'label': '排练街头', 'description': '黑色背心、宽松工装裤与薄外套，方便排练活动。',
                  'pieces': ['黑色背心 · 炭灰薄外套', '深色宽松工装长裤', '黑色运动鞋'], 'colors': ['#252528', '#53574e', '#9a958c'],
                  'outfit': 'a black sleeveless top under an open lightweight charcoal jacket, loose dark cargo trousers and black sneakers'},
    'literary': {'label': '学院文艺', 'description': '米白衬衫、深棕针织马甲与黑色长裤，清爽低调。',
                 'pieces': ['米白长袖衬衫 · 深棕针织马甲', '黑色剪裁长裤', '深色乐福鞋'], 'colors': ['#d7cfbc', '#6f5544', '#2d2b2a'],
                 'outfit': 'an ivory long-sleeve shirt under a dark brown knitted vest, tailored black full-length trousers and dark loafers'},
    'stage': {'label': '舞台演出', 'description': '黑色缎面上衣、高腰长裤与简洁银饰，克制的舞台感。',
              'pieces': ['黑色缎面贴身长袖上衣', '黑色高腰剪裁长裤', '深色短靴 · 简洁银饰'], 'colors': ['#28282e', '#44434b', '#bbb9b2'],
              'outfit': 'a fitted black satin long-sleeve blouse, high-waisted black tailored trousers, dark ankle boots and minimal silver jewelry'},
}


def validate(value):
    if (not isinstance(value, dict) or set(value) != {'style_id'}
            or not isinstance(value['style_id'], str) or value['style_id'] not in STYLES):
        raise ValueError('WARDROBE_STYLE_INVALID')
    return dict(value)


def catalog():
    return [{'style_id': key, 'label': style['label'], 'description': style['description'],
             'pieces': list(style['pieces']), 'colors': list(style['colors'])}
            for key, style in STYLES.items()]


def style_for(settings):
    return validate({'style_id': settings.get('wardrobe_style', DEFAULT_STYLE)})['style_id']


def photo_reference(settings):
    style = style_for(settings)
    return {} if style == DEFAULT_STYLE else {'wardrobe': {'catalog_version': CATALOG_VERSION, 'style_id': style}}


def dress_photo(plan, settings):
    """Legacy image API: deterministic clothing constraints, never a second person."""
    style = style_for(settings)
    if style == DEFAULT_STYLE or plan.get('photo_type') == 'snapshot' or not plan.get('attach', True):
        return dict(plan)
    prompt = (plan['prompt'] + '\nWardrobe override: replace all earlier clothing descriptions. '
              'Linli wears ' + STYLES[style]['outfit'] + '. '
              'Keep her face, short brown bob with side braid, body, pose and scene unchanged. '
              'This outfit does not imply a different location or activity.')
    if len(prompt) > 4000:
        raise ValueError('IMAGE_PLAN_INVALID')
    return {**plan, 'prompt': prompt}
