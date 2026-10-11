"""One autonomous life decision, projected against immutable world facts."""
from __future__ import annotations

from datetime import datetime
from copy import deepcopy
import json
from jsonschema import Draft202012Validator, ValidationError
from .place_detail import SCHEMA as PLACE_SCHEMA, validate as validate_place, label as place_label

KINDS = ('class', 'practice', 'reading', 'meal', 'rest', 'housework', 'walk', 'errand', 'creative', 'bath_started', 'bath_finished', 'shopping')
_ACTIVITY = {'type': 'object', 'additionalProperties': False, 'required': ['kind', 'place_id', 'focus'],
    'properties': {'kind': {'enum': list(KINDS)}, 'place_id': {'enum': ['home', 'campus', 'neighborhood', 'shop']},
                   'focus': {'type': 'string', 'maxLength': 40}, 'place': {'anyOf': [{'type':'null'}, PLACE_SCHEMA]}}}
SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['activity', 'meal', 'project'], 'properties': {
    'activity': _ACTIVITY,
    'meal': {'anyOf': [{'type': 'null'}, {'type': 'object', 'additionalProperties': False,
        'required': ['slot', 'food', 'status'], 'properties': {
            'slot': {'enum': ['breakfast', 'lunch', 'dinner', 'snack']},
            'food': {'type': 'string', 'maxLength': 60},
            'status': {'enum': ['planned', 'eating', 'eaten', 'skipped']}}}]},
    'project': {'anyOf': [{'type': 'null'}, {'type': 'object', 'additionalProperties': False,
        'required': ['id', 'title', 'status', 'progress', 'next_activity'], 'properties': {
            'id': {'type': 'string', 'minLength': 1, 'maxLength': 160},
            'title': {'type': 'string', 'minLength': 1, 'maxLength': 60},
            'status': {'enum': ['planned', 'ongoing', 'paused', 'completed', 'cancelled']},
            'progress': {'anyOf': [{'type': 'null'}, {'type': 'string', 'minLength': 1, 'maxLength': 100}]},
            'next_activity': {'anyOf': [{'type': 'null'}, _ACTIVITY]}}}]}}}
_DEVELOPMENT = {'type': 'array', 'maxItems': 3, 'items': {'type': 'object', 'additionalProperties': False,
    'required': ['source_id', 'key', 'stance', 'quote', 'reason'], 'properties': {
        'source_id': {'type': 'string', 'maxLength': 160}, 'key': {'type': 'string', 'maxLength': 48},
        'stance': {'enum': ['positive', 'negative']}, 'quote': {'type': 'string', 'minLength': 1, 'maxLength': 240},
        'reason': {'type': 'string', 'minLength': 1, 'maxLength': 160}}}}
_STRICT_SCHEMA = deepcopy(SCHEMA)
# The shared activity schema also covers next_activity; old stored choices may
# omit place, while strict provider output must explicitly supply it or null.
_STRICT_SCHEMA['properties']['activity']['required'].append('place')
FORMAT = {'type': 'json_schema', 'json_schema': {'name': 'life_decision', 'strict': True,
    'schema': {**_STRICT_SCHEMA, 'required': [*_STRICT_SCHEMA['required'], 'development'],
               'properties': {**_STRICT_SCHEMA['properties'], 'development': _DEVELOPMENT}}}}
LIFE_FORMAT = {'type': 'json_schema', 'json_schema': {'name': 'life_decision', 'strict': True, 'schema': _STRICT_SCHEMA}}
_VALIDATOR = Draft202012Validator(SCHEMA)

LIFE_PROMPT = '''为角色决定现在这一小段自己的生活，只返回契约JSON，不写场景散文或历史叙述。
生活由她自身课程、兴趣、精力、未完事项和选择推进，用户没发消息也有生活；平淡和休息都正常，不必每次有成果。既有私密用户经历不在生成范围。
rhythm.historical_rest是历史通信负荷；当下精力看rest/current_load_minutes/recovery。night_correspondence不是疾病证据，不把熬夜变成全天只能休息；可选择补觉、吃饭和适量活动。authored_sleep到时只续接补觉，醒后恢复由新过程判断，不自动痊愈。
world和places是不可更改的已知事实和计划，previous/recent_life是有时间的过去，不能改成现在。新增一个合理的当下活动、具体新食物或事项进展是允许的；不补造上午、昨天发生了什么。
activity决定正在做的动作、地点类别和具体对象。focus只填具体名词短语，如“左手两处衔接”“记忆主题的随笔”“窗边的植物”，不能塞入天气、住处、课程时间、过去事件或完整叙述。程序会组合事实描述，不输出note/current/weather或感想。
activity.kind只从allowed_activity_kinds选择；它由课表和已有身体/作息状态计算。class必须有world.schedule.current_class且地点campus；课程尚未结束。课表只是计划：本次明确选择class才形成上课生活记录，不能声称已上完此前课程。已有持续身体不适时可选择在家休养，睡眠/洗澡时段选择休息，不为逃课临时编造生病或请假。home严格指persona中的既有住处，places中的地点类别由程序提供，不添加宿舍或搬家。
meal仅在activity.kind=meal时填写且不可空，其他活动填null。同一餐食物与状态只在这里决定，不在focus另写；planned准备吃、eating正在吃、eaten刚吃完、skipped未吃。已吃的餐不能重写，昨天的饭不是今天已吃的饭；food只填菜名，不带昨晚剩菜等来源故事。根据便利、位置、食欲和近期饮食自由选择，不照搬人格例子。
project只更新当前活动确实关联的一个她自己的事项，无关联填null；沿用已有id/title，时间过去不自动完成。status表达这次实际决定的状态，next_activity只选下一步打算，不冒充已经做过。没有完成就ongoing/paused，不随意重开完成事项；不改shared或用户事项。下一步class只允许引用world.schedule.next_class；用餐计划由meal字段承载，不写入next_activity。
projects保留全部事项身份；未结束事项的history按时间顺序保留同一id最近三次变化。历史每条source_id/updated_at是其原事件来源与时间，published_life是正式发布的生活事件，character_statement仅证明她曾这样说，user_statement仅证明用户这样说，不把说法自动当成实际进展。completed/cancelled仅保留id/title/status/evidence_kind身份摘要，完整正文与来源仍在账本；它们仍是已有终结身份，不能当新建。
progress是当前这个事项或动作本次实际变化、卡点或暂停缘由的简短说明，最多100字，没有变化可null，也可整个project=null。承接已有进展，下一步打算只能写next_activity；不得重复上一条来假装新进展。completed必须有非空progress说明本次完成结果，不能仅凭已有计划、时间过去或下一步安排完成；新建的一次短步骤确已做完可以直接completed，不把整个长期事项跟着完成。
progress不用于扩写世界：不得添写天气、住处、课程出勤、用餐、第三方或用户经历，不编造昨天或离线期间做过什么。当前地点/活动/餐食仍只由activity与meal决定；未知继续保留未知。暂停、无进展与失败也可是真实变化，不必每天成功。
未知天气没有天气剧情入口；真实观测仅由程序提供。人格声明保持原有层级与置信度，不改写旧背景。不要添加契约外字段。
character_development是同一时间截面的有限发展投影，仅影响对应key的倾向；未成熟的trying不是喜欢，baseline为avoid的重大差异最多有限尝试。不改写核心、生平、其他偏好、当前事实或关系权限。'''
LIFE_PROMPT += '\nbath_finished只创作此刻确实完成的一次洗澡，地点home且focus为空；不是睡前时段自动已洗，也不是把计划或用户洗澡写成她的经历。没有这次实际行动选择其他活动。'
LIFE_PROMPT += '\nbath_started是她此刻实际开始洗澡，home且focus为空，尚未洗完；已有rhythm.authored_bath时不再开始第二次澡，只在本次真实结束时选bath_finished，时间过去不自动完成。'
LIFE_PROMPT += '\nshopping是她此刻在shop实际进行的一次购物，具体是否买到由本次过程决定；不等同errand出门准备或购物计划，不补造交易、品牌、用户或他人购物。'

PROMPT = LIFE_PROMPT + '\ndevelopment默认为空数组，不为凑成长制造评价。它仅评价development_basis.sources中已经正式发布的旧活动，不能评价本次新决定或未来安排；最多3项，严格为source_id,key,stance,quote,reason，key只能来自development_topics，quote是旧world.note连续原文，主题可自然使用同义表达，reason是160字内的有限体验评价。\n结合旧活动具体内容及其已批准appraisal，判断她对活动本身的正面或负面体验，stance为positive或negative；情绪高兴/沮丧、做得成功/失败、用户爱好、被催促或被迫活动、一般疲惫，均不能机械换成喜欢/讨厌该活动。证据不足填空数组。trait只评价受控领域中这次具体行为的体验，不自夸性格或宣布已改变；taste只可评价已明确eaten的食物。成长由程序按多日证据慢慢累计，单次不成熟，不把模糊结果具体化。'



def decision_context(data: dict) -> dict:
    from .project_timing import prepare_context
    data = prepare_context(data)
    try:
        declarations = json.loads(data['persona'])
    except (ValueError, TypeError):
        declarations = []
    residence = next((d for d in declarations if isinstance(d, dict) and d.get('declaration_id') == 'anchor.residence'), None)
    schedule = data['world']['schedule']
    future = [c for c in schedule['classes'] if datetime.fromisoformat(c['start']) > datetime.fromisoformat(data['time'])]
    # Do not invite a new story about a morning class already over. Current and
    # next scheduled lessons remain precise, and the UI retains the full day.
    current_schedule = {k: v for k, v in schedule.items() if k != 'classes'}
    current_schedule['next_class'] = future[0] if future else None
    places = {'home': {'label': '家里' if residence else '住处', 'basis': residence},
              'campus': {'label': '学校', 'basis': '课程安排'},
              'neighborhood': {'label': '住处附近', 'basis': residence},
              'shop': {'label': '店里', 'basis': '本次日常外出'}}
    rest_phase = data['rhythm']['phase'] in {'sleep', 'bathing', 'interrupted_rest'}
    wellbeing = data['rhythm'].get('wellbeing', {})
    unwell = wellbeing.get('state') in {'unwell', 'recovering'} and wellbeing.get('basis') != 'night_correspondence'
    # Escalated care retains the existing rest constraint; it must not reopen
    # class/practice choices merely because the care enum changed.
    needs_rest = unwell and wellbeing.get('care') in {'rest', 'consider_consultation'}
    sleep_due = (data['rhythm'].get('authored_sleep') or {}).get('status') == 'due'
    authored_bath = data['rhythm'].get('authored_bath')
    allowed = (['bath_finished', 'rest'] if authored_bath else ['rest', 'bath_started'] if data['rhythm']['phase'] == 'bathing' else
               ['rest'] if rest_phase or sleep_due else
               ['rest', 'meal'] if needs_rest else
               ['class', 'rest'] if current_schedule['current_class'] and (unwell or data['rhythm'].get('rest') in {'tired', 'depleted'}) else
               ['class'] if current_schedule['current_class'] else
               [kind for kind in KINDS if kind != 'class'])
    return {**data, 'places': places, 'allowed_activity_kinds': allowed,
            'world': {**{key: value for key, value in data['world'].items() if key != 'today_activities'}, 'schedule': current_schedule}}


def compile_decision(value: dict, data: dict) -> tuple[dict, list, list]:
    """Only this projection authors factual current/meal/project descriptions."""
    try:
        if isinstance(value, dict):
            if 'project_timing' in value:
                from .project_timing import validate as validate_project_timing
                validate_project_timing(value['project_timing'], data)
            value = {k: v for k, v in value.items() if k not in {'development', 'project_timing'}}
        _VALIDATOR.validate(value)
    except ValidationError:
        raise ValueError('DAILY_LIFE_DECISION_INVALID') from None
    activity, meal, project = value['activity'], value['meal'], value['project']
    schedule = data['world']['schedule']
    course = schedule['current_class']
    if activity['kind'] not in data['allowed_activity_kinds']:
        raise ValueError('DAILY_LIFE_DECISION_CLASS_CONFLICT')
    if activity['kind'] == 'class' and (not course or activity['place_id'] != 'campus'):
        raise ValueError('DAILY_LIFE_DECISION_CLASS_CONFLICT')
    if activity['kind'] in {'bath_started', 'bath_finished'} and (activity['place_id'] != 'home' or activity['focus'].strip()):
        raise ValueError('DAILY_LIFE_DECISION_BATH_INVALID')
    if activity['kind'] == 'shopping' and activity['place_id'] != 'shop':
        raise ValueError('DAILY_LIFE_DECISION_SHOPPING_INVALID')
    if (activity['kind'] == 'meal') != (meal is not None):
        raise ValueError('DAILY_LIFE_DECISION_MEAL_CONFLICT')
    location = data['places'][activity['place_id']]['label']
    place = validate_place(activity['place']) if activity.get('place') else None
    if place:
        if place['place_id'] != activity['place_id'] or (place['stage'] != 'arrived' and activity['kind'] != 'errand'):
            raise ValueError('DAILY_LIFE_PLACE_INVALID')
        location = place_label(place)
    labels = {'practice': '练琴', 'reading': '阅读', 'rest': '休息', 'housework': '整理家务',
              'walk': '散步', 'errand': '办日常杂事', 'creative': '创作', 'bath_started': '开始洗澡', 'bath_finished': '刚洗完澡', 'shopping': '买日常用品'}

    def describe(action, lesson=None):
        if action['kind'] == 'class':
            if not lesson or action['place_id'] != 'campus':
                raise ValueError('DAILY_LIFE_DECISION_NEXT_CLASS_INVALID')
            return '上' + lesson['title']
        if action['kind'] == 'meal':
            raise ValueError('DAILY_LIFE_DECISION_MEAL_CONFLICT')
        if action['kind'] == 'rest':
            # Rest has no object. Free focus text must not smuggle an unrecorded
            # prior meal/sleep event into the current factual description.
            return labels['rest']
        return labels[action['kind']] + ('：' + action['focus'].strip() if action['focus'].strip() else '')

    if meal is not None:
        if meal['status'] != 'skipped' and not meal['food'].strip():
            raise ValueError('DAILY_LIFE_MEALS_INVALID')
        slot = {'breakfast': '早饭', 'lunch': '午饭', 'dinner': '晚饭', 'snack': '加餐'}[meal['slot']]
        verb = {'planned': '准备吃', 'eating': '正在吃', 'eaten': '刚吃完', 'skipped': '没吃'}[meal['status']]
        description = verb + slot
        note = description + ('：' + meal['food'].strip() if meal['food'].strip() else '') + '。'
    else:
        description = describe(activity, course)
        note = '在' + location + description + '。'
    current = {'location': location, 'activity': description, 'note': note}
    if place:
        current['place'] = place
    updates = []
    if project is not None:
        if activity['kind'] in {'meal', 'walk', 'errand', 'bath_started', 'bath_finished', 'shopping'}:
            raise ValueError('DAILY_LIFE_DECISION_UNRELATED_PROJECT')
        old = next((p for p in data['projects'] if p['id'] == project['id']), None)
        if old and (old.get('deadline_expired') or old.get('time_scope') == 'transient'):
            raise ValueError('DAILY_LIFE_DECISION_PROJECT_EXPIRED')
        if activity['kind'] == 'rest' and (not old or project['status'] not in {'paused', 'cancelled'}):
            raise ValueError('DAILY_LIFE_DECISION_UNRELATED_PROJECT')
        if old and old['status'] in {'completed', 'cancelled'} and project['status'] != old['status']:
            raise ValueError('DAILY_LIFE_DECISION_PROJECT_REOPEN')
        progress = project['progress']
        if progress is not None:
            progress = progress.strip()
            if not progress:
                raise ValueError('DAILY_LIFE_DECISION_PROGRESS_INVALID')
        if project['status'] == 'completed' and not progress:
            raise ValueError('DAILY_LIFE_DECISION_RESULT_REQUIRED')
        detail = '本次：' + description + '。'
        if progress:
            detail += '本次变化：' + progress + '。'
        if project['next_activity']:
            following = project['next_activity']
            detail += '下一步打算在' + data['places'][following['place_id']]['label'] + describe(following, schedule['next_class']) + '。'
        if len(detail) > 240:
            raise ValueError('DAILY_LIFE_DECISION_PROGRESS_TOO_LONG')
        updates.append({'id': project['id'], 'title': old['title'] if old else project['title'],
                        'detail': detail, 'status': project['status']})
    return current, updates, [meal] if meal is not None else []
