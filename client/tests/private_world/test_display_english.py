"""The World page in English: the program's own wording, every stock text covered, model wording passed through."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from original_client_server import create_original_client_server_runtime
from runtime import ui_language
from runtime.private_world import display_english as english
from runtime.private_world import jev_world, life_episode, place_detail, student_world
from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime
from runtime.private_world.life_rhythm import rhythm
from runtime.private_world.world_decision import KINDS, compile_decision, decision_context
from runtime.reply import media_delivery

HAN = re.compile(r'[一-鿿]')
NOW = datetime(2026, 10, 18, 10, 45, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def interface_language():
    ui_language.reset()
    yield
    ui_language.reset()


def to_english():
    ui_language.set_language('en')


# --- the reported case --------------------------------------------------------------------------------------------

def test_rest_at_home_shows_english_activity_and_place():
    to_english()
    current = {'location': '卧室', 'activity': '休息', 'note': '在卧室休息。', 'occurred_at': NOW.isoformat(),
               'source_id': 'day:rest', 'place': {'place_id': 'home', 'name': '卧室', 'setting': '卧室，沿用已有布局与陈设。', 'stage': 'arrived'}}
    shown = english.localize_life({'current': current})['current']
    assert shown['activity'] == 'Resting'
    assert shown['location'] == 'Bedroom'
    assert shown['note'] == 'In her bedroom, resting.'
    # Ids, times and the stored place stay as they were.
    assert shown['source_id'] == 'day:rest' and shown['occurred_at'] == current['occurred_at']
    assert shown['place'] == current['place']


def test_chinese_mode_hands_back_the_same_objects():
    payload = {'current': {'location': '卧室', 'activity': '休息', 'note': '在卧室休息。'}, 'moments': []}
    assert english.localize_life(payload) is payload
    history = {'moments': [{'kind': 'daily', 'content': {'activity': '休息'}}]}
    assert english.localize_moments(history) is history


def test_localizing_never_changes_the_stored_payload():
    to_english()
    payload = {'current': {'location': '卧室', 'activity': '休息', 'note': '在卧室休息。'},
               'rhythm': {'activity': '晚饭时间', 'note': '按自己的节奏生活。', 'wellbeing': {'summary': '没有持续身体不适的记录。'}},
               'world': {'schedule': {'classes': [{'title': '和声'}], 'next_class': {'title': '体育'}},
                         'today_activities': [{'activity': '休息', 'note': '在卧室休息。'}],
                         'meals': [{'food': '馄饨', 'slot': 'dinner'}], 'recent_episodes': []},
               'projects': [{'title': '音乐与回忆个人研究', 'detail': '本次：休息。'}], 'shared': [],
               'moments': [{'kind': 'daily', 'content': {'activity': '休息', 'location': '卧室', 'note': '在卧室休息。'}}]}
    before = deepcopy(payload)
    shown = english.localize_life(payload)
    assert payload == before
    assert shown is not payload and shown['current'] is not payload['current']


# --- places, activities, notes -------------------------------------------------------------------------------------

@pytest.mark.parametrize('name,label', [
    ('家里', 'Home'), ('住处', 'Her place'), ('学校', 'School'), ('住处附近', 'Near home'), ('店里', 'In a shop'),
    ('浴室', 'Bathroom'), ('卧室', 'Bedroom'), ('家里音乐房', 'Music room at home'),
    ('学校琴房', 'School practice room'), ('学校教室', 'School classroom'),
])
def test_every_place_the_program_names_has_a_label(name, label):
    assert english.location_text(name) == label


def test_travelling_to_a_place_reads_as_a_sentence_and_keeps_the_model_name():
    assert english.location_text('准备前往街角书店') == 'Getting ready to go to 街角书店'
    assert english.location_text('前往学校琴房的路上') == 'On the way to a practice room at school'
    assert english.location_text('前往街角书店的路上') == 'On the way to 街角书店'
    assert english.location_text('前往商场服装店的路上') == 'On the way to a clothing store in a mall'


def test_model_written_names_and_focuses_are_kept_inside_the_english_frame():
    to_english()
    shown = english.localize_life({'current': {
        'location': '街角书店', 'activity': '买日常用品：秋天穿的外套', 'note': '在街角书店买日常用品：秋天穿的外套。'}})['current']
    assert shown['location'] == '街角书店'
    assert shown['activity'] == 'Shopping for everyday things: 秋天穿的外套'
    assert shown['note'] == 'At 街角书店, shopping for everyday things: 秋天穿的外套.'


def test_free_text_without_a_known_frame_is_left_alone():
    to_english()
    said = {'location': None, 'activity': None, 'note': '刚在信里说，下午想去琴房。'}
    assert english.localize_life({'current': said})['current'] == said
    odd = {'location': '家里', 'activity': '休息', 'note': '今天有点累，想早点睡。'}
    assert english.localize_life({'current': odd})['current']['note'] == '今天有点累，想早点睡。'


def test_a_note_for_another_place_is_not_rewritten():
    to_english()
    odd = {'location': '学校', 'activity': '休息', 'note': '在宿舍休息。'}
    assert english.localize_life({'current': odd})['current']['note'] == '在宿舍休息。'


@pytest.mark.parametrize('text,title', [
    ('休息', 'Resting'), ('醒来', 'Waking up'), ('开始洗澡', 'Starting her bath'), ('刚洗完澡', 'Just finished her bath'),
    ('上和声', 'In class: Harmony'), ('准备吃早饭', 'About to have breakfast'), ('正在吃午饭', 'Having lunch'),
    ('刚吃完晚饭', 'Just finished dinner'), ('没吃加餐', 'Skipped a snack'),
    ('已下单，等待外卖早餐', 'Waiting for the breakfast delivery'), ('正在准备餐食午餐', 'Preparing lunch'),
    ('准备出门就餐晚餐', 'Heading out for dinner'), ('准备买饭带回午餐', 'Going to pick up lunch'),
    ('练琴：乐句衔接', 'Piano practice: phrase transitions'),
])
def test_activity_titles(text, title):
    assert english.activity_title(text) == title


def test_meal_notes_name_the_food():
    assert english.note_text('正在吃午饭：馄饨。') == 'Having lunch: wontons.'
    assert english.note_text('已下单，等待外卖：妈妈做的汤面。') == 'Delivery ordered, waiting: 妈妈做的汤面.'
    assert english.note_text('已下单，等待外卖：番茄牛腩饭。') == 'Delivery ordered, waiting: tomato beef rice.'
    assert english.note_text('没吃早饭。') == 'Skipped breakfast.'


def test_project_updates_the_program_composes():
    detail = '本次：练琴：乐句衔接。本次变化：完成本次选定片段的连贯练习。下一步打算在学校上和声。'
    assert english.project_detail(detail) == (
        'This time: Piano practice: phrase transitions. What changed: Played the chosen passage through smoothly. '
        'Planned next, not done yet: at school, in class: Harmony.')
    assert english.project_detail('本次：休息。') == 'This time: Resting.'
    assert english.project_detail('我自己写的一句话') == '我自己写的一句话'
    assert english.project_title('乐句衔接 · 本次小任务') == 'Phrase transitions · a small task for now'


# --- every text the program can compose has English ----------------------------------------------------------------

def decision_data():
    return {
        'world': {'schedule': {'current_class': {'title': '和声'}, 'next_class': {'title': '体育'}}},
        'places': {'home': {'label': '家里'}, 'campus': {'label': '学校'}, 'neighborhood': {'label': '住处附近'},
                   'shop': {'label': '店里'}},
        'allowed_activity_kinds': list(KINDS), 'projects': [],
    }


def test_every_activity_the_program_composes_has_english():
    data = decision_data()
    compiled = 0
    for kind in KINDS:
        if kind == 'meal':
            continue
        for place_id in ('home', 'campus', 'neighborhood', 'shop'):
            for focus in jev_world._FOCUSES.get(kind, ('',)):
                detail = place_detail.activity_place(kind, place_id)
                activity = {'kind': kind, 'place_id': place_id, 'focus': focus, **({'place': detail} if detail else {})}
                try:
                    current, _, _ = compile_decision({'activity': activity, 'meal': None, 'project': None}, data)
                except ValueError:
                    continue
                compiled += 1
                shown = english._current(current)
                assert not HAN.search(json.dumps([shown[key] for key in ('location', 'activity', 'note')], ensure_ascii=False)), (
                    kind, place_id, focus)
    assert compiled > 40


def test_every_meal_the_program_composes_has_english():
    data = decision_data()
    for slot, foods in jev_world._FOODS.items():
        for status in ('planned', 'eating', 'eaten', 'skipped'):
            for food in foods:
                current, _, _ = compile_decision(
                    {'activity': {'kind': 'meal', 'place_id': 'home', 'focus': ''}, 'project': None,
                     'meal': {'slot': slot, 'food': '' if status == 'skipped' else food, 'status': status}}, data)
                shown = english._current(current)
                assert not HAN.search(json.dumps([shown[key] for key in ('location', 'activity', 'note')], ensure_ascii=False)), (
                    slot, status, food)


def test_meal_stages_composed_by_the_clock_have_english():
    for stage in ('已下单，等待外卖', '正在准备餐食', '准备出门就餐', '准备买饭带回', '正在吃'):
        for title in ('早餐', '午餐', '晚餐'):
            assert not HAN.search(english.activity_title(stage + title)), (stage, title)
            assert not HAN.search(english.note_text(stage + '：' + '馄饨。')), stage


def test_every_project_update_the_program_composes_has_english():
    data = decision_data()
    for kind, (partial, completed, difficulty) in jev_world._PROGRESS.items():
        place_id = 'campus' if kind == 'class' else 'home'
        for progress in (None, partial, completed, difficulty):
            for following in (None, {'kind': 'class', 'place_id': 'campus', 'focus': ''},
                              {'kind': 'practice', 'place_id': 'home', 'focus': '乐句衔接'}):
                project = {'id': 'p', 'title': '乐句衔接 · 本次小任务', 'status': 'ongoing', 'progress': progress,
                           'next_activity': following}
                activity = {'kind': kind, 'place_id': place_id, 'focus': '' if kind == 'class' else jev_world._FOCUSES[kind][0]}
                _, updates, _ = compile_decision({'activity': activity, 'meal': None, 'project': project}, data)
                shown = english.project_detail(updates[0]['detail'])
                assert not HAN.search(shown), (kind, progress, shown)
                assert not HAN.search(english.project_title(updates[0]['title']))


def test_the_timetable_and_the_fixed_catalogs_are_covered():
    for week in student_world.WEEK:
        for _, _, _, title in week:
            assert title in english.COURSES
    for catalog in jev_world._FOCUSES.values():
        for focus in catalog:
            assert not focus or focus in english.FOCUSES
    for foods in jev_world._FOODS.values():
        assert set(foods) <= set(english.FOODS)
    for progress in jev_world._PROGRESS.values():
        assert set(progress) <= set(english.PROGRESS)
    assert set(media_delivery._SUMMARIES.values()) == set(english.SUMMARIES)
    from runtime.private_world.jev_emotion import NEEDS
    assert {text for text in NEEDS.values() if text} == set(english.NEEDS)


def test_the_rest_rhythm_has_english_for_every_phase_and_state():
    seen = set()
    start = datetime(2026, 10, 5, tzinfo=timezone(timedelta(hours=8)))
    for day in range(14):
        for hour in range(24):
            for minute in (0, 30):
                now = (start + timedelta(days=day, hours=hour, minutes=minute)).astimezone(timezone.utc)
                for heavy in (False, True):
                    exchanges = []
                    if heavy:
                        for n in range(13):
                            begin = start + timedelta(hours=n * 24 + 1 + (n % 3) * 0.5)
                            exchanges.append((begin.astimezone(timezone.utc), (begin + timedelta(minutes=150)).astimezone(timezone.utc)))
                    state = rhythm(now, exchanges)
                    seen.update({state['activity'], state['note'], state['wellbeing']['summary']})
    assert len(seen) >= 12
    assert {text for text in seen if HAN.search(english.RHYTHM.get(text, text))} == set()


class Recorder:
    """Records every stock text offered for one activity and chooses nothing."""

    def __init__(self, texts):
        self.texts = texts

    async def ask(self, state, questions, **kwargs):
        self.texts.update(questions['trigger']['criteria'].values())
        for path in state['paths'].values():
            for step in path['process']:
                self.texts.update([step['obstacle'], step['response'], step['outcome']])
            self.texts.add(path['result']['detail'])
        self.texts.update(item['meaning'] for item in state['interpretations'].values())
        for effect in state['effects'].values():
            self.texts.update(value for value in effect.values() if isinstance(value, str))
        raise LookupError('recorded')


def record(texts, kind, context, when=NOW, **kwargs):
    try:
        asyncio.run(life_episode.create(Recorder(texts), 'day:probe', when, kind, context, **kwargs))
    except LookupError:
        pass


def test_every_stock_episode_text_has_english():
    texts = set()
    base = dict(time=NOW.isoformat(), world={'schedule': {'current_class': {'title': '和声'}, 'next_class': None}}, rhythm={})
    for kind in KINDS:
        record(texts, kind, base)
    for status in ('eaten', 'skipped', 'planned', 'eating'):
        for stage in (None, 'ordered', 'preparing', 'travelling', 'collecting'):
            record(texts, 'meal', base, meal={'status': status, 'meal_stage': stage})
    for kind, interrupted in (('overnight', False), ('overnight', True), ('nap', False), ('nap', True)):
        sleep = {'status': 'due', 'kind': kind, 'interrupted': interrupted, 'end_at': NOW.isoformat(), 'source_id': 's'}
        record(texts, 'rest', dict(base, rhythm={'authored_sleep': sleep}))
    night = datetime(2026, 10, 12, 14, 30, tzinfo=timezone.utc)
    candidate = dict(end_at=(night + timedelta(hours=8)).isoformat())
    record(texts, 'rest', dict(base, overnight_sleep_candidate=candidate), when=night)
    for course in student_world_titles():
        record(texts, 'class', dict(base, world={'schedule': {'current_class': {'title': course}, 'next_class': None}}))
    assert len(texts) > 150
    assert sorted(text for text in texts if HAN.search(english.episode_text(text))) == []


def student_world_titles():
    return sorted({title for week in student_world.WEEK for _, _, _, title in week})


def test_the_phase_projects_of_the_persona_have_english():
    persona = json.loads((Path(__file__).parents[2] / 'linli_character' / 'persona_release_v2.json').read_text(encoding='utf-8'))

    def seeds(value):
        if isinstance(value, dict):
            if value.get('phase_seed'):
                yield value['phase_seed']
            for item in value.values():
                yield from seeds(item)
        elif isinstance(value, list):
            for item in value:
                yield from seeds(item)

    found = list(seeds(persona))
    assert found
    for seed in found:
        assert not HAN.search(english.project_title(seed['title'])), seed['title']
        assert not HAN.search(english.project_detail(seed['detail'])), seed['detail']


# --- the stored world, end to end ------------------------------------------------------------------------------------

class Choosing:
    async def ask(self, state, questions, **kwargs):
        return {key: next(iter(question['criteria'])) for key, question in questions.items()}


def publish(store, index, kind, place_id, focus, when):
    state = store.snapshot(when)
    data = decision_context(dict(time=when.isoformat(), persona='[]', world=state['world'], rhythm=state['rhythm'], projects=[]))
    detail = place_detail.activity_place(kind, place_id)
    activity = dict(kind=kind, place_id=place_id, focus=focus, **({'place': detail} if detail else {}))
    current, projects, meals = compile_decision(dict(activity=activity, meal=None, project=None), data)
    source_id = f'day:synthetic{index}'
    episode = asyncio.run(life_episode.create(Choosing(), source_id, when, kind, {**state, 'time': when.isoformat()}))
    store.publish_day(source_id, current, projects, occurred_at=when, activity_kind=kind, episode=episode)


def lived_in_store(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    days = [('rest', 'home', '', -540), ('practice', 'home', '乐句衔接', -420), ('reading', 'campus', '借阅的书', -240),
            ('walk', 'neighborhood', '附近的街道', -180), ('bath_started', 'home', '', -100),
            ('bath_finished', 'home', '', -80), ('rest', 'home', '', -30)]
    for index, (kind, place_id, focus, minutes) in enumerate(days):
        publish(store, index, kind, place_id, focus, NOW + timedelta(minutes=minutes))
    return store


def displayed_texts(payload):
    """Every string the World page prints from a payload, by the keys the page reads."""
    shown = []

    def take(value):
        if isinstance(value, str):
            shown.append(value)

    current = payload.get('current') or {}
    for key in ('location', 'activity', 'note'):
        take(current.get(key))
    rhythm_state = payload.get('rhythm') or {}
    take(rhythm_state.get('activity'))
    take(rhythm_state.get('note'))
    take((rhythm_state.get('wellbeing') or {}).get('summary'))
    world = payload.get('world') or {}
    for item in world.get('today_activities') or []:
        take(item.get('activity'))
        take(item.get('note'))
    for item in (world.get('schedule') or {}).get('classes') or []:
        take(item.get('title'))
    for episode in world.get('recent_episodes') or []:
        take((episode.get('trigger') or {}).get('detail'))
        for step in episode.get('process') or []:
            for key in ('obstacle', 'response', 'outcome'):
                take(step.get(key))
        take((episode.get('result') or {}).get('detail'))
        take((episode.get('interpretation') or {}).get('meaning'))
        for key in ('next_action', 'open_loop'):
            take((episode.get('effects') or {}).get(key))
    for moment in payload.get('moments') or []:
        content = moment.get('content') or {}
        for key in ('location', 'activity', 'note'):
            take(content.get(key))
    return shown


def test_a_lived_in_world_shows_no_chinese_on_the_page(tmp_path):
    store = lived_in_store(tmp_path)
    payload = store.snapshot(NOW)
    assert any(HAN.search(text) for text in displayed_texts(payload))
    to_english()
    shown = displayed_texts(english.localize_life(payload))
    assert len(shown) > 40
    assert [text for text in shown if HAN.search(text)] == []
    history = store.history()
    assert [text for text in displayed_texts(english.localize_moments(history)) if HAN.search(text)] == []


def test_the_life_endpoint_answers_in_the_interface_language(tmp_path):
    store = lived_in_store(tmp_path)

    async def run():
        life = DailyLifeRuntime(store, lambda: None, lambda: '')

        async def fallback(request):
            return web.Response(status=404)

        runtime = create_original_client_server_runtime(fallback, daily_life=life, trusted_origins=('https://client.example',))
        headers = {'Origin': 'https://client.example'}
        async with TestClient(TestServer(runtime.app)) as client:
            current = (await (await client.get('/toy/companion/private-world/life', headers=headers)).json())['current']
            chinese_history = await (await client.get('/toy/companion/private-world/life?history=1', headers=headers)).json()
            to_english()
            shown = (await (await client.get('/toy/companion/private-world/life', headers=headers)).json())['current']
            english_history = await (await client.get('/toy/companion/private-world/life?history=1', headers=headers)).json()
        return current, shown, chinese_history, english_history

    chinese, shown, chinese_history, english_history = asyncio.run(run())
    assert (chinese['activity'], chinese['location']) == ('休息', '卧室')
    assert (shown['activity'], shown['location'], shown['note']) == ('Resting', 'Bedroom', 'In her bedroom, resting.')
    assert shown['source_id'] == chinese['source_id'] and shown['occurred_at'] == chinese['occurred_at']
    assert chinese_history['moments'][0]['content']['activity'] == '休息'
    assert english_history['moments'][0]['content']['activity'] == 'Resting'
    assert english_history['moments'][0]['id'] == chinese_history['moments'][0]['id']
    assert english_history['next_cursor'] == chinese_history['next_cursor']


# --- what she has on her mind, and quotes of her own notes ---------------------------------------------------------------

def test_quotes_of_her_own_notes_and_the_stock_concern_lines_have_english():
    to_english()
    shown = english.localize_life({
        'emotion': {'reactions': [{'reaction': 'relieved', 'quote': '在卧室休息。', 'goal_or_need': '休息与恢复精力'},
                                  {'reaction': 'calm', 'quote': '我今天有点累。', 'goal_or_need': None}],
                    'concerns': [{'summary': '上钢琴专业课时，有一部分内容还没理解；具体疑问尚未记录。'},
                                 {'summary': '这一处暂时没弄懂，不代表整节课失败或已经结束。'},
                                 {'summary': '明天的课堂展示'}]},
        'reply_basis': {'status': 'available', 'activity': '休息',
                        'reactions': [{'reaction': 'pleased', 'quote': '正在吃午饭：馄饨。', 'goal_or_need': '得到理解与陪伴'}],
                        'concerns': ['这一处暂时没弄懂，不代表整节课失败或已经结束。', '她自己写的牵挂']}})
    reactions = shown['emotion']['reactions']
    assert reactions[0]['quote'] == 'In her bedroom, resting.'
    assert reactions[0]['goal_or_need'] == 'resting and recovering her energy'
    assert reactions[0]['reaction'] == 'relieved'
    assert reactions[1]['quote'] == '我今天有点累。' and reactions[1]['goal_or_need'] is None
    assert [item['summary'] for item in shown['emotion']['concerns']] == [
        "Part of Piano major class wasn't clear yet; the specific questions haven't been noted.",
        "She hasn't got this part yet, which doesn't mean the whole class went badly or is over.",
        '明天的课堂展示']
    basis = shown['reply_basis']
    assert basis['activity'] == 'Resting'
    assert basis['reactions'][0]['quote'] == 'Having lunch: wontons.'
    assert basis['reactions'][0]['goal_or_need'] == 'being understood and having company'
    assert basis['concerns'][1] == '她自己写的牵挂'


# --- what the day-plan writer is told to use -------------------------------------------------------------------------

def test_the_examples_in_the_day_plan_prompt_have_english():
    from runtime.private_world import day_plan
    quoted = re.findall(r'“([^”]+)”', day_plan.PROMPT)
    assert len(quoted) >= 8
    for example in quoted:
        assert example in english.FOCUSES or example in english.FOODS, example
    for name in ('卧室', '厨房', '音乐房', '商场服装店', '超市', '文具店', '花店', '面包店', '展馆', '公园'):
        assert english.place_forms(name) is not None, name


def test_plan_places_read_naturally_in_a_sentence():
    to_english()
    assert english.location_text('家里餐桌') == 'Dining table at home'
    assert english.location_text('前往超市的路上') == 'On the way to the supermarket'
    current = {'location': '超市', 'activity': '买日常用品：牙膏', 'note': '在超市买日常用品：牙膏。'}
    shown = english.localize_life({'current': current})['current']
    assert (shown['location'], shown['activity'], shown['note']) == (
        'Supermarket', 'Shopping for everyday things: 牙膏', 'At the supermarket, shopping for everyday things: 牙膏.')
    assert english.localize_life({'current': {'location': '二食堂', 'activity': '休息', 'note': '在二食堂休息。'}})['current'][
        'note'] == 'At 二食堂, resting.'
