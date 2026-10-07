import asyncio
from datetime import datetime, timezone, timedelta
import json
from types import SimpleNamespace
import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime
from runtime.private_world.world_decision import decision_context, compile_decision

NOW = datetime(2026, 9, 28, 6, 15, tzinfo=timezone.utc)


def data_for(store, now=NOW):
    state = store.snapshot(now)
    return decision_context({'time': now.isoformat(),
        'persona': json.dumps([{'declaration_id':'anchor.residence', 'statement':'角色独居在青岛的家里。'}]),
        'world':state['world'], 'rhythm':state['rhythm'], 'projects':state['projects']})


def decision(kind='class', place='campus', focus='左手衔接', meal=None, project=None):
    return {'activity':{'kind':kind,'place_id':place,'focus':focus}, 'meal':meal, 'project':project, 'development':[]}


def test_course_and_residence_are_resolved_facts_not_model_prose(tmp_path):
    data=data_for(DailyLifeStore(tmp_path/'world.db'))
    assert data['places']['home']['basis']['statement'] == '角色独居在青岛的家里。'
    current, projects, meals=compile_decision(decision(), data)
    assert current == {'location':'学校','activity':'上钢琴专业课','note':'在学校上钢琴专业课。'}
    with pytest.raises(ValueError, match='CLASS_CONFLICT'):
        compile_decision(decision('rest','home'), data)
    with pytest.raises(ValueError, match='DECISION_INVALID'):
        compile_decision({'current':{'location':'宿舍','activity':'下课','note':'正在下雨'},'projects':[]}, data)


def test_existing_illness_permits_home_rest_during_class(tmp_path):
    store=DailyLifeStore(tmp_path/'world.db')
    state=store.snapshot(NOW)
    data=decision_context({'time':NOW.isoformat(),'persona':'[]','world':state['world'],
        'rhythm':{**state['rhythm'],'wellbeing':{'state':'unwell'}},'projects':[]})
    current, _, _=compile_decision(decision('rest','home','休养'), data)
    assert current['location']=='住处'
    assert '上课' not in current['note']
    data['rhythm']['wellbeing']['care'] = 'rest'
    data['world'] = state['world']
    data = decision_context(data)
    assert data['allowed_activity_kinds'] == ['rest', 'meal']
    with pytest.raises(ValueError, match='CLASS_CONFLICT'):
        compile_decision(decision(), data)


def test_meal_has_one_source_for_fact_and_description(tmp_path):
    data=data_for(DailyLifeStore(tmp_path/'world.db'),NOW.replace(hour=4))
    meal={'slot':'lunch','food':'米饭和清蒸鱼','status':'eating'}
    current, _, meals=compile_decision(decision('meal','home','不用于事实的焦点',meal), data)
    assert meals == [meal]
    assert current['note']=='正在吃午饭：米饭和清蒸鱼。'
    assert '焦点' not in current['note']
    with pytest.raises(ValueError, match='MEAL_CONFLICT'):
        compile_decision(decision('meal','home'), data)


def test_project_progress_uses_current_decision_not_a_second_history(tmp_path):
    data=data_for(DailyLifeStore(tmp_path/'world.db'))
    project={'id':'left-hand','title':'左手练习','status':'ongoing','progress':None,
             'next_activity':{'kind':'practice','place_id':'home','focus':'两个衔接小节'}}
    _, updates, _=compile_decision(decision(project=project), data)
    assert updates[0]['detail']=='本次：上钢琴专业课。下一步打算在家里练琴：两个衔接小节。'
    assert 'updated_at' not in updates[0]
    with pytest.raises(ValueError, match='DECISION_INVALID'):
        compile_decision(decision(project={**project,'updated_at':NOW.isoformat()}), data)


def test_old_storage_survives_but_old_generated_envelope_requires_correction(tmp_path):
    store=DailyLifeStore(tmp_path/'world.db')
    old={'location':'家里','activity':'读书','note':'读了两页。'}
    before=NOW.replace(day=27)
    store.publish_day('day:old', old, [], occurred_at=before)
    calls=[]
    class Gateway:
        async def complete(self,messages,**kwargs):
            calls.append(json.loads(messages[1]['content']))
            return SimpleNamespace(text=json.dumps({'current':old,'projects':[]} if len(calls)==1 else decision()))
    runtime=DailyLifeRuntime(store,Gateway,lambda:'[]')
    asyncio.run(runtime.refresh(NOW))
    assert len(calls)==2
    assert calls[1]['validation_error']=='DAILY_LIFE_DECISION_INVALID'
    assert runtime.error_code is None
    assert store.snapshot(before)['current']['note']=='读了两页。'
    assert store.snapshot(NOW)['current']['activity']=='上钢琴专业课'


def test_real_structured_gateway_format_failure_gets_only_one_world_correction(tmp_path):
    from llm_gateway import GatewayConfig, OpenAICompatibleAdapter
    gateway=OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible',base_url='https://175.24.191.6/v1',model='qwen3.7-flash'))
    calls=[]
    emotion_calls=[]
    async def post(body,*args,**kwargs):
        packet=json.loads(body['messages'][-1]['content'])
        if 'assessment' in packet:
            emotion_calls.append(body)
            content=json.dumps({'appraisals': [dict(source_id=s['source_id'], quote='',
                reported_affect=None, reaction='none', goal_or_need=None,
                action_tendency='none', concern=None, revises=None)
                for s in packet['assessment']['sources']]})
            return {'choices':[{'finish_reason':'stop','message':{'content':content}}]}
        calls.append(body)
        value = decision()
        value['activity']['place'] = None
        content='{"current":{"note":"obsolete prose"}}' if len(calls)==1 else json.dumps(value)
        return {'choices':[{'finish_reason':'stop','message':{'content':content}}]}
    gateway._post_json=post
    store=DailyLifeStore(tmp_path/'world.db')
    runtime=DailyLifeRuntime(store,lambda:gateway,lambda:'[]')
    asyncio.run(runtime.refresh(NOW))
    assert len(calls)==2
    assert emotion_calls==[]  # Appraisal is paid for only when the user writes.
    assert runtime.emotion.error_code is None
    assert runtime.error_code is None
    assert store.snapshot(NOW)['current']['activity']=='上钢琴专业课'
    assert 'life_decision' in json.dumps(calls[0]) or 'place_id' in calls[0]['messages'][0]['content']
    assert json.loads(calls[1]['messages'][-1]['content'])['validation_error']=='DAILY_LIFE_DECISION_INVALID'


@pytest.mark.parametrize('failure',['length','auth','http'])
def test_world_does_not_correct_truncation_or_provider_rejection(tmp_path,failure):
    from llm_gateway import GatewayConfig, OpenAICompatibleAdapter, ProviderRejected
    gateway=OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible',base_url='https://175.24.191.6/v1',model='qwen3.7-flash'))
    calls=[]
    async def post(body,*args,**kwargs):
        calls.append(body)
        if failure!='length':raise ProviderRejected(401 if failure=='auth' else 500)
        return {'choices':[{'finish_reason':'length','message':{'content':'{"activity":'}}]}
    gateway._post_json=post
    store=DailyLifeStore(tmp_path/'world.db')
    runtime=DailyLifeRuntime(store,lambda:gateway,lambda:'[]')
    asyncio.run(runtime.refresh(NOW))
    assert len(calls)==1
    assert runtime.error_code=='DAILY_LIFE_GENERATION_UNAVAILABLE'
    assert store.snapshot(NOW)['current'] is None


def test_rest_cannot_embed_an_unrecorded_meal_in_object_text(tmp_path):
    data=data_for(DailyLifeStore(tmp_path/'world.db'),NOW.replace(hour=4))
    current, _, meals=compile_decision(decision('rest','home','刚吃完的餐'),data)
    assert current['note']=='在家里休息。'
    assert meals==[]


def test_persisted_terminal_project_is_protected_beyond_prompt_limit(tmp_path):
    store=DailyLifeStore(tmp_path/'world.db')
    current={'location':'家里','activity':'练琴','note':'继续练习。'}
    old_id='p'*150
    store.publish_day('day:closed',current,[{'id':old_id,'title':'已完成作品','detail':'完成了','status':'completed'}],occurred_at=NOW-timedelta(days=1))
    for batch in range(2):
        store.publish_day('day:active'+str(batch),current,[{'id':'active'+str(i+batch*3),'title':'在练'+str(i+batch*3),'detail':'练习中','status':'ongoing'} for i in range(3)],occurred_at=NOW-timedelta(hours=2-batch))
    data=data_for(store)
    assert old_id not in {p['id'] for p in data['projects']}
    plan=decision(project={'id':old_id,'title':'不能改名','status':'ongoing','progress':None,'next_activity':None})
    generated,projects,meals=compile_decision(plan,data)  # Long legacy ID is legal.
    before=store.snapshot(NOW)['current']
    with pytest.raises(ValueError,match='PROJECT_REOPEN'):
        store.publish_day('day:reopen',generated,projects,occurred_at=NOW,meals=meals)
    assert not store.has_source('day:reopen')
    assert store.snapshot(NOW)['current']==before
    with store._db() as db:
        persisted=json.loads(db.execute('SELECT payload FROM life_projects WHERE id=?',(old_id,)).fetchone()[0])
    assert persisted['status']=='completed'
    assert persisted['title']=='已完成作品'
    projects[0]['status']='completed'
    store.publish_day('day:same-terminal',generated,projects,occurred_at=NOW,meals=meals)
    with store._db() as db:
        unchanged=json.loads(db.execute('SELECT payload FROM life_projects WHERE id=?',(old_id,)).fetchone()[0])
    assert unchanged==persisted
    assert store.snapshot(NOW)['current']['progress']==[]
