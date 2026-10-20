from runtime.reply.jev_limits import JEV_MAX_INPUT_BYTES
import asyncio
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from runtime.private_world import life_episode
from runtime.private_world.daily_life import DailyLifeStore
from tests.private_world.test_meal_lifecycle import at


class Port:
    def __init__(self, path='stuck', meaning='friction', effect='return'):
        self.path,self.meaning,self.effect=path,meaning,effect
        self.calls=[]
    async def ask(self,state,questions,**kwargs):
        self.calls.append((state,questions))
        return dict(trigger=next(iter(questions['trigger']['criteria'])),
                    experience=f'{self.path}:{self.meaning}:{self.effect}')


def episode(port,kind='practice',**kwargs):
    return asyncio.run(life_episode.create(port,'day:synthetic',at(11),kind,
                       dict(time=at(11).isoformat(),world={},rhythm={}),**kwargs))


def publish(store,value):
    return store.publish_day('day:synthetic',dict(location='琴房',activity='练习',note=value['result']['detail']),[],
                             occurred_at=at(11),activity_kind='practice',episode=value)


def test_episode_input_contains_only_selected_activity_and_its_prior_state():
    port=Port()
    previous={'activity':'练琴','note':'刚才这一小节还没练稳。','occurred_at':at(10).isoformat()}
    data={'time':at(11).isoformat(),'selected_activity':{'kind':'practice','focus':'第二小节'},
          'selected_project':{'id':'piano','title':'练习','detail':'第二小节','status':'ongoing'},
          'previous':previous,'rhythm':{'rest':'rested','phase':'free','private_detail':'UNRELATED'},
          'emotion':{'status':'ready','current_affect':{'label':'frustrated','reason':'小节没练稳'},
                     'reactions':[{'quote':'UNRELATED'}]},
          'projects':[{'detail':'UNRELATED'}]*20,
          'world':{'today_activities':[{'note':'UNRELATED'}]*100,'recent_episodes':[{'detail':'UNRELATED'}]*20}}
    asyncio.run(life_episode.create(port,'day:minimal',at(11),'practice',data))
    state=port.calls[0][0]['context']
    assert state['previous']==previous
    assert state['selected_project']['id']=='piano'
    assert state['emotion']['current_affect']['reason']=='小节没练稳'
    assert 'UNRELATED' not in json.dumps(state)
    assert len(port.calls)==1


def test_failed_experience_is_frozen_with_process_and_subjective_meaning(tmp_path):
    port=Port()
    value=episode(port)
    assert len(port.calls)==1
    assert value['result']['status']=='failed'
    assert value['process'][0]['response']=='缩小练习范围并尝试放慢'
    assert value['interpretation']['subjective'] is True
    assert value['effects']['open_loop']=='练习片段仍待巩固'
    store=DailyLifeStore(tmp_path/'world.db')
    assert publish(store,value)
    assert not publish(store,value)
    assert store.snapshot(at(11))['world']['recent_episodes']==[value]
    assert not store.snapshot(at(10))['world']['recent_episodes']
    reply=json.loads(store.reply_context('练习怎么样',now=at(11),max_chars=7000))
    assert reply['recent_episodes']==[value]
    assert reply['recent_episodes'][0]['interpretation']['subjective']


def test_old_event_never_gets_new_explanation(tmp_path):
    store=DailyLifeStore(tmp_path/'world.db')
    store.publish_day('old',dict(location='琴房',activity='练习',note='正在练习。'),[],occurred_at=at(10))
    assert store.snapshot(at(11))['world']['recent_episodes']==[]


def test_binding_failure_rolls_back_whole_publication(tmp_path):
    store=DailyLifeStore(tmp_path/'world.db')
    wrong=episode(Port())
    wrong['source_id']='other'
    with pytest.raises(ValueError,match='LIFE_EPISODE_INVALID'): publish(store,wrong)
    assert not store.has_source('day:synthetic')
    assert not store.snapshot(at(11))['world']['recent_episodes']


def test_saved_episode_is_immutable(tmp_path):
    store=DailyLifeStore(tmp_path/'world.db')
    value=episode(Port())
    publish(store,value)
    changed=deepcopy(value)
    changed['result']['detail']='改变旧结果'
    with store._db() as db, pytest.raises(ValueError,match='LIFE_EPISODE_REWRITE'):
        life_episode.save(db,changed,'day:synthetic',at(11))


def test_recovered_meal_does_not_invent_past_cause():
    port=Port()
    assert episode(port,kind='meal',meal={'recovered':True,'status':'eaten'}) is None
    assert not port.calls


def test_provider_failure_does_not_create_event(tmp_path):
    class Failed:
        async def ask(self,*args,**kwargs): raise ValueError('JEV_UNAVAILABLE')
    store=DailyLifeStore(tmp_path/'world.db')
    with pytest.raises(ValueError,match='JEV_UNAVAILABLE'): episode(Failed())
    assert not store.has_source('day:synthetic')


def test_oversize_context_is_explicit_and_never_calls_provider():
    port=Port()
    with pytest.raises(ValueError,match='LIFE_EPISODE_CONTEXT_TOO_LARGE'):
        asyncio.run(life_episode.create(port,'new',at(11),'practice',{'previous':{'note':'x'*15000}}))
    assert not port.calls


def test_runtime_episode_failure_changes_actual_project_and_next_world_context(tmp_path,monkeypatch):
    from runtime.private_world import daily_life_runtime as module
    from runtime.reply import jev_questions
    store=DailyLifeStore(tmp_path/'world.db')
    store.publish_day('breakfast',dict(location='住处',activity='吃早餐',note='早餐吃完了。'),[],occurred_at=at(8),
                      meals=[dict(slot='breakfast',food='包子',status='eaten')])
    runtime=module.DailyLifeRuntime(store,lambda:object(),lambda:'大学生')
    port=Port()
    async def no_emotion(now): pass
    async def choice(*a,**k):
        return dict(activity=dict(kind='practice',place_id='home',focus='衔接片段'),meal=None,
                    project=dict(id='small-task',title='片段练习',status='completed',progress='完成片段',next_activity=None))
    monkeypatch.setattr(runtime,'_complete',choice)
    monkeypatch.setattr(module,'configured_duties',lambda:None)
    monkeypatch.setattr(jev_questions,'configured_questions',lambda:port)
    asyncio.run(runtime.refresh(at(11)))
    assert runtime.error_code is None
    snapshot=store.snapshot(at(11))
    assert snapshot['projects'][0]['status']=='paused'
    assert snapshot['world']['recent_episodes'][0]['result']['status']=='failed'
    assert snapshot['projects'][0]['detail']==snapshot['world']['recent_episodes'][0]['result']['detail']


def test_failed_meal_episode_publishes_nothing_and_uses_meal_retry(tmp_path):
    from runtime.private_world.meal_lifecycle import advance
    class FailsEpisode:
        def __init__(self): self.calls=0
        async def ask(self,state,questions,**kwargs):
            self.calls+=1
            if kwargs['purpose']=='world-life-episode': raise ValueError('JEV_UNAVAILABLE')
            return {'meal':next(iter(questions['meal']['criteria']))}
    store,port=DailyLifeStore(tmp_path/'world.db'),FailsEpisode()
    asyncio.run(advance(store,port,at(8)))
    assert not store.snapshot(at(8))['world']['meals']
    assert not store.snapshot(at(8))['world']['recent_episodes']
    asyncio.run(advance(store,port,at(8)+timedelta(minutes=1)))
    assert port.calls==2


@pytest.mark.parametrize('kind', ['class','practice','reading','meal','rest','housework','walk','errand','creative'])
def test_all_activity_kinds_have_bounded_own_process_and_persist(tmp_path, kind):
    class First:
        async def ask(self, state, questions, **kwargs):
            assert len(json.dumps({'state':state,'questions':questions},ensure_ascii=False).encode()) < JEV_MAX_INPUT_BYTES
            assert len(questions['experience']['criteria']) <= 48
            return {key: next(iter(question['criteria'])) for key, question in questions.items()}
    value = asyncio.run(life_episode.create(First(), 'own:1', at(11), kind,
        {'world': {'schedule': {'current_class': {'name':'当前课程'}}},
         'selected_activity': {'kind': kind, 'focus':'当前小步'}}))
    assert value['activity_kind'] == kind and value['actor'] == 'character'
    assert all(value['process'][0][key] for key in ('obstacle','response','outcome'))
    assert value['interpretation']['subjective'] is True
    assert set(value['effects']) == {'next_action','open_loop'}
    store = DailyLifeStore(tmp_path/'world.db')
    store.publish_day('own:1',dict(location='校园',activity='当前小步',note=value['result']['detail']),[],
                      occurred_at=at(11),activity_kind=kind,episode=value)
    snapshot = store.snapshot(at(11))
    assert snapshot['world']['recent_episodes'] == [value]
    assert snapshot['projects'] == []  # An ordinary activity must not invent a project.
    if kind == 'class':
        assert value['result']['status'] == 'partial'
        assert '课程仍在进行' in value['result']['detail']


def test_authored_rest_recovery_feeds_next_world_without_erasing_night_load(tmp_path):
    from runtime.private_world.world_decision import decision_context
    store = DailyLifeStore(tmp_path / 'world.db')
    store.record_exchange('reply:night', '聊聊。', '晚安。', [], received_at=at(1), occurred_at=at(3))
    baseline = store.snapshot(at(11))['rhythm']
    assert baseline['rest'] == 'depleted' and baseline['wellbeing']['state'] == 'well'
    for index, path in enumerate(('ongoing', 'settled', 'refreshed')):
        now = at(11) + timedelta(minutes=index * 30)
        before = store.snapshot(now)
        port = Port(path=path, meaning='recovery', effect='none')
        value = asyncio.run(life_episode.create(port, f'day:rest:{index}', now, 'rest',
            dict(time=now.isoformat(), rhythm=before['rhythm'], previous=before['current'], world={})))
        assert len(port.calls) == 1
        store.publish_day(value['source_id'], dict(activity='休息', location='家里', note=value['result']['detail']), [],
            occurred_at=now, activity_kind='rest', episode=value)
        body = store.snapshot(now)['rhythm']
        assert body['rest'] == ('depleted', 'tired', 'rested')[index]
        assert body['historical_rest'] == baseline['historical_rest']
    restarted = DailyLifeStore(store.path).snapshot(at(12))
    assert restarted['rhythm']['rest'] == 'rested'
    assert restarted['rhythm']['wellbeing']['care'] == 'none'
    context = decision_context(dict(time=at(12).isoformat(), persona='[]', projects=[],
        world=restarted['world'], rhythm=restarted['rhythm']))
    assert 'walk' in context['allowed_activity_kinds']
    assert not any('睡着' in p.get('response', '') for p in value['process'])


@pytest.mark.parametrize('state', ['unwell', 'recovering'])
def test_rest_does_not_author_illness_recovery(state):
    from runtime.private_world.life_rhythm import with_recovery
    port = Port(path='refreshed', meaning='recovery', effect='none')
    body = {'wellbeing': {'state': state, 'care': 'rest'}, 'rest': 'depleted', 'historical_rest': {'load_minutes': 300}}
    value = asyncio.run(life_episode.create(port, 'day:rest', at(11), 'rest',
        {'rhythm': body}))
    recovered = with_recovery(body, [value], at(11))
    assert recovered['rest'] == 'rested'
    assert recovered['wellbeing'] == body['wellbeing']


def test_consecutive_identical_rest_records_group_without_deleting_evidence(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    for index, minute in enumerate((0, 30, 60, 120)):
        store.publish_day(f'day:r{index}', dict(activity='休息', location='家里', note='安静休息。'), [],
                          occurred_at=at(11)+timedelta(minutes=minute), activity_kind='rest')
    rows = store.snapshot(at(14))['world']['today_activities']
    assert len(rows) == 2 and rows[0]['record_count'] == 3
    assert rows[0]['source_ids'] == ['day:r0', 'day:r1', 'day:r2']
    assert rows[0]['last_recorded_at'] == at(12).astimezone(life_episode.timezone.utc).isoformat()
    with store._db() as db:
        assert db.execute("SELECT count(*) FROM life_moments WHERE kind='daily'").fetchone()[0] == 4


def test_same_activity_records_group_with_the_latest_note(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    for index, (minute, note) in enumerate(((0, '翻开第一章。'), (28, '看到第二章。'), (92, '第二章快看完。'), (100, '第三章开头。'))):
        store.publish_day(f'day:b{index}', dict(activity='阅读：图书馆借的记忆心理学', location='家里', note=note), [],
                          occurred_at=at(20)+timedelta(minutes=minute), activity_kind='reading')
    store.publish_day('day:p0', dict(activity='练琴', location='家里', note='弹音阶。'), [],
                      occurred_at=at(20)+timedelta(minutes=110), activity_kind='practice')
    store.publish_day('day:b4', dict(activity='阅读：图书馆借的记忆心理学', location='家里', note='接着看。'), [],
                      occurred_at=at(20)+timedelta(minutes=120), activity_kind='reading')
    rows = store.snapshot(at(23))['world']['today_activities']
    assert [(r['activity'], r.get('record_count', 1)) for r in rows] == [
        ('阅读：图书馆借的记忆心理学', 4), ('练琴', 1), ('阅读：图书馆借的记忆心理学', 1)]
    assert rows[0]['source_ids'] == ['day:b0', 'day:b1', 'day:b2', 'day:b3'] and rows[0]['note'] == '第三章开头。'
    assert rows[0]['last_recorded_at'] == (at(20)+timedelta(minutes=100)).astimezone(life_episode.timezone.utc).isoformat()
    with store._db() as db:
        assert db.execute("SELECT count(*) FROM life_moments WHERE kind='daily'").fetchone()[0] == 6


def test_class_plan_alone_cannot_author_an_attended_episode():
    with pytest.raises(ValueError,match='LIFE_EPISODE_CLASS_NOT_CURRENT'):
        episode(Port(),kind='class')


def test_unresolved_class_process_names_the_known_course_in_one_call():
    port = Port(path='2', meaning='friction', effect='none')
    value = asyncio.run(life_episode.create(port, 'day:class', at(11), 'class',
        {'world': {'schedule': {'current_class': {'title': '合成钢琴课程'}}}, 'rhythm': {}}))
    assert len(port.calls) == 1
    assert value['result']['status'] == 'failed'
    assert value['result']['detail'] == '合成钢琴课程：课堂上有一部分内容还没理解，已记下疑问，留待继续梳理。'
    assert value['process'][0]['obstacle'] == '合成钢琴课程：有一部分课堂内容还没理解'
    assert value['process'][0]['outcome'] == value['result']['detail']


def test_activity_results_keep_internal_evidence_rules_out_of_visible_prose():
    for kind, (_, candidates) in life_episode._ACTIVITY_PATHS.items():
        for _, _, _, detail in candidates:
            assert not any(marker in detail for marker in ('没有编造', '没有声称', '未声称', '不代表', '并非整个'))
    port = Port(path='1', meaning='ordinary', effect='none')
    value = episode(port, kind='errand')
    assert value['result']['detail'] == '整理好了这次出门需要带的东西。'
    assert '出门准备不代表外部事务办妥' in port.calls[0][1]['experience']['instructions']
    assert len(port.calls) == 1
