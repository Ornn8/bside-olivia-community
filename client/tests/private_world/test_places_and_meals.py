import asyncio
import json
import time
import pytest
from datetime import datetime, timedelta, timezone

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world import day_plan, jev_world, meal_lifecycle
from runtime.private_world.world_decision import decision_context, compile_decision

NOW = datetime(2026, 10, 11, 4, tzinfo=timezone.utc)


def place(name='商场服装店', place_id='shop'):
    return dict(place_id=place_id, name=name, setting=name+'，木质货架和暖白灯光。', stage='arrived')


def plan(store):
    meal = dict(food='番茄牛腩饭', mode='delivery', place=place('家里餐桌', 'home'))
    value = {'activities': {'shopping': [{'focus': '秋天穿的外套', 'place': place(), 'paths': []}]},
             'meals': {slot: [meal] for slot in day_plan.MEAL_SLOTS}}
    with store._db() as db:
        day_plan.initialize(db)
        db.execute('INSERT INTO life_day_plans VALUES (?,?)', ('2026-10-11', json.dumps(value)))
    return value


def test_precise_place_survives_world_and_reply_projection(tmp_path):
    store = DailyLifeStore(tmp_path/'world.db')
    state = store.snapshot(NOW)
    data = decision_context(dict(time=NOW.isoformat(), persona='[]', world=state['world'],
                                 rhythm=state['rhythm'], projects=[]))
    value = dict(activity=dict(kind='errand', place_id='shop', focus='秋天穿的外套', place=place()), meal=None, project=None)
    current, projects, meals = compile_decision(value, data)
    store.publish_day('synthetic:shop', current, projects, meals=meals, activity_kind='errand', occurred_at=NOW)
    actual = store.snapshot(NOW)['current']
    assert actual['location'] == '商场服装店'
    assert actual['place']['setting'] == place()['setting']
    assert '商场服装店' in store.reply_context('', now=NOW, max_chars=8000)
    assert place()['setting'] in store.reply_context('', now=NOW, max_chars=8000)


def test_outing_from_home_has_travel_before_arrival(tmp_path):
    store = DailyLifeStore(tmp_path/'world.db')
    data = dict(allowed_activity_kinds=['shopping','errand'], projects=[], day_plan=plan(store),
                previous=dict(place=place('卧室','home')), world={'schedule': {}}, time=NOW.isoformat())
    options = jev_world._activities(data)
    chosen = next(a for a in options.values() if a['focus']=='秋天穿的外套')
    assert chosen['kind'] == 'errand'
    assert chosen['place']['stage'] == 'travelling'
    data['previous'] = dict(place=chosen['place'])
    chosen = next(a for a in jev_world._activities(data).values() if a['focus']=='秋天穿的外套')
    assert chosen['kind'] == 'shopping'
    assert chosen['place']['stage'] == 'arrived'


class Port:
    async def ask(self, state, questions, **kwargs):
        if kwargs.get('purpose') == 'world-life-episode':
            return {k: next(iter(v['criteria'])) for k,v in questions.items()}
        options = questions['meal']['criteria']
        key = next((k for k in options if k.startswith(('eat_', 'finished_'))), next(iter(options)))
        return {'meal': key}


def test_delivery_plan_waits_then_eats_without_rewriting_history(tmp_path):
    store = DailyLifeStore(tmp_path/'world.db'); plan(store)
    asyncio.run(meal_lifecycle.advance(store, Port(), NOW))
    def lunch(at):
        return next(m for m in store.snapshot(at)['world']['meals'] if m['slot']=='lunch')
    first = lunch(NOW)
    assert first['food'] == '番茄牛腩饭'
    assert first['mode'] == 'delivery'
    assert first['status'] == 'planned' and first['meal_stage'] == 'ordered'
    assert not store.daily_video_sources(now=NOW)
    asyncio.run(meal_lifecycle.advance(store, Port(), NOW+timedelta(minutes=5)))
    assert lunch(NOW+timedelta(minutes=5))['status'] == 'planned'
    asyncio.run(meal_lifecycle.advance(store, Port(), NOW+timedelta(minutes=35)))
    eating = lunch(NOW+timedelta(minutes=35))
    assert eating['status'] == 'eating' and eating['mode'] == 'delivery'
    assert eating['place'] == first['place']
    assert any(s['event_kind']=='meal' and s['location']=='家里餐桌'
               for s in store.daily_video_sources(now=NOW+timedelta(minutes=35)))
    asyncio.run(meal_lifecycle.advance(store, Port(), NOW+timedelta(minutes=60)))
    assert lunch(NOW+timedelta(minutes=60))['status'] == 'eaten'
    assert lunch(NOW) == first
    assert '番茄牛腩饭' in day_plan.recent_week(store, NOW+timedelta(minutes=60))['foods']


def test_custom_place_flows_from_world_to_video_and_cannot_be_rewritten(tmp_path):
    from runtime.personal_chat.daily_video import DailyVideoWorker, select_candidate
    store = DailyLifeStore(tmp_path/'world.db')
    store.publish_day('synthetic:walk', dict(location='公园湖畔', activity='散步', note='绕着湖边走走。',
        place=place('公园湖畔','neighborhood')), [], activity_kind='walk', occurred_at=NOW)
    worker = DailyVideoWorker.__new__(DailyVideoWorker)
    worker.scenes = [dict(scene_id='daily_place', event_kinds=['walk'], locations=[])]
    worker.capabilities_at = time.monotonic()
    worker.event_store = store
    worker.get = lambda _: None
    worker.preparation = lambda _: None
    candidates = worker.candidates(now=NOW)
    assert len(candidates)==1 and candidates[0]['scene_id']=='daily_place'
    request = select_candidate(dict(event_id='synthetic:walk',spoken_text='出来走走，这里挺舒服的。'), candidates)
    assert request['place']['name']=='公园湖畔'
    assert store.daily_video_can_prepare(request,now=NOW)
    assert not store.daily_video_can_prepare({**request,'place':place('商场','shop')},now=NOW)
    assert not store.daily_video_sources(now=NOW-timedelta(seconds=1))


@pytest.mark.parametrize('mode,stage', [('cook','preparing'),('delivery','ordered'),
                                     ('dine_out','travelling'),('takeaway','collecting'),('canteen','eating')])
def test_meal_modes_keep_place_and_progress(mode,stage):
    value={'meals': {'lunch':[dict(food='咖喱鸡饭',mode=mode,place=place('食堂','campus'))]}}
    choices=meal_lifecycle.options('lunch',None,NOW,plan=value)
    assert choices['eat_0']['meal_stage']==stage
    assert choices['eat_0']['mode']==mode
    if stage!='eating':
        assert choices['eat_0']['status']=='planned' and choices['eat_0']['started_at'] is None


def test_structured_meal_cannot_bypass_preparation_through_world_selector(tmp_path):
    store=DailyLifeStore(tmp_path/'world.db')
    data=dict(time=NOW.isoformat(),day_plan=plan(store),world={'meals':[]})
    assert not any(key.startswith(('breakfast','lunch','dinner')) for key in jev_world._meals(data))


def test_ordering_delivery_keeps_current_location_and_records_actual_action(tmp_path):
    store=DailyLifeStore(tmp_path/'world.db'); plan(store)
    store.publish_day('synthetic:campus', dict(location='学校图书馆', activity='看书', note='看书。',
        place=place('学校图书馆','campus')), [], activity_kind='reading', occurred_at=NOW-timedelta(minutes=1))
    asyncio.run(meal_lifecycle.advance(store,Port(),NOW))
    assert store.snapshot(NOW)['current']['location']=='学校图书馆'
    assert store.snapshot(NOW)['current']['place']['name']=='学校图书馆'
    assert '已经下单' in store.reply_context('',now=NOW,max_chars=12000)


def test_provider_formats_require_every_declared_object_field():
    from runtime.private_world.world_decision import FORMAT, LIFE_FORMAT
    def check(schema):
        if isinstance(schema,dict):
            if schema.get('type')=='object':
                assert set(schema['required'])==set(schema['properties'])
            for value in schema.values():
                check(value)
        elif isinstance(schema,list):
            for value in schema:
                check(value)
    for response_format in (FORMAT,LIFE_FORMAT,day_plan.FORMAT):
        check(response_format['json_schema']['schema'])


def test_bath_preparation_cannot_change_recorded_place(tmp_path):
    from runtime.private_world.life_episode import create
    from runtime.personal_chat.daily_video import select_candidate
    store=DailyLifeStore(tmp_path/'world.db')
    episode=asyncio.run(create(Port(),'bath:start',NOW,'bath_started',{}))
    store.publish_day('bath:start',dict(location='浴室',activity='开始洗澡',note='开始洗澡。',
        place=place('浴室','home')),[],activity_kind='bath_started',occurred_at=NOW,episode=episode)
    source=store.daily_video_sources(now=NOW)[0]
    source.update(version=1,certainty='live',scene_id='bathroom',target_date='2026-10-11')
    request=select_candidate(dict(event_id='bath:start',spoken_text='洗好了。'),[source])
    assert store.daily_video_can_prepare(request,now=NOW)
    assert not store.daily_video_can_prepare({**request,'place':place('商场','shop')},now=NOW)
