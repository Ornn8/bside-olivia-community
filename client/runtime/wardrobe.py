"""Fixed wardrobe styles shared by photo planning and video scene selection."""
from datetime import date
import re

DAILY_STYLES = ('original', 'mori', 'rebellious', 'dark', 'cargo', 'tie-shorts')
DAILY_CATALOG = '20261004-e94ffc6dd51b'


def validate_daily_outfit(value):
    if value is None:
        return None
    keys={'date','timezone','preference_revision','style_id','look_id','catalog_version','reference_sha256'}
    if not isinstance(value,dict) or set(value)!=keys:
        raise ValueError('WARDROBE_OUTFIT_INVALID')
    date.fromisoformat(value['date'])
    if (value['timezone']!='Asia/Shanghai' or type(value['preference_revision']) is not int
            or value['preference_revision']<1 or value['style_id'] not in DAILY_STYLES[1:]
            or value['catalog_version']!=DAILY_CATALOG
            or not isinstance(value['look_id'],str) or value['look_id'] not in {
                value['style_id']+f'-{i:02}' for i in range(1,15 if value['style_id']=='mori' else 2)}
            or not isinstance(value['reference_sha256'],str) or not re.fullmatch(r'[a-f0-9]{64}',value['reference_sha256'])):
        raise ValueError('WARDROBE_OUTFIT_INVALID')
    return dict(value)


def validate_cloud_state(value):
    if not isinstance(value,dict) or value.get('catalog_version')!=DAILY_CATALOG or value.get('timezone')!='Asia/Shanghai':
        raise ValueError('WARDROBE_STATE_INVALID')
    date.fromisoformat(value['date'])
    preference=value['wardrobe']
    if (not isinstance(preference,dict) or set(preference)!={'style_id','preference_revision'}
            or preference['style_id'] not in DAILY_STYLES or type(preference['preference_revision']) is not int
            or preference['preference_revision']<0):
        raise ValueError('WARDROBE_STATE_INVALID')
    styles=value['wardrobe_styles']
    if not isinstance(styles,list) or len(styles)!=len(DAILY_STYLES) or any(not isinstance(s,dict) for s in styles) or {s.get('style_id') for s in styles}!=set(DAILY_STYLES):
        raise ValueError('WARDROBE_STATE_INVALID')
    for style in styles:
        if not all(isinstance(style.get(k),str) and len(style[k])<=1000 for k in ('label','description')) or not isinstance(style.get('looks'),list) or len(style['looks'])>14:
            raise ValueError('WARDROBE_STATE_INVALID')
        for look in style['looks']:
            if (not isinstance(look,dict) or not isinstance(look.get('look_id'),str)
                    or not re.fullmatch(re.escape(style['style_id'])+r'-\d{2}',look['look_id'])
                    or not isinstance(look.get('label'),str) or len(look['label'])>200):
                raise ValueError('WARDROBE_STATE_INVALID')
    outfit=validate_daily_outfit(value.get('daily_outfit'))
    if outfit and (outfit['date']!=value['date'] or outfit['style_id']!=preference['style_id'] or outfit['preference_revision']!=preference['preference_revision']):
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
