"""A day's authored candidates replace the fixed catalog; Jev still chooses."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from runtime.private_world import day_plan
from runtime.private_world.daily_life import DailyLifeStore

NOW = datetime(2026, 9, 30, 1, tzinfo=timezone.utc)  # 09:00 Shanghai


def draft():
    path = lambda outcome, status='partial': {'obstacle': '第二乐章的分谱记号很乱', 'response': '先用铅笔重新标一遍',
                                              'status': status, 'outcome': outcome}
    activities = [{'kind': kind, 'focus': focus, 'paths': [path(focus + '整理了一半'), path(focus + '做完了', 'completed')]}
                  for kind, focus in (('practice', '乐团分谱第二乐章'), ('reading', '和声学作业第三题'),
                                      ('creative', '随手记的四小节动机'), ('housework', '阳台上的衣物'),
                                      ('walk', '校园东门的银杏道'), ('errand', '菜鸟驿站的快递'))]
    return {'activities': activities, 'meals': {'breakfast': ['楼下的煎饼果子', '二食堂的豆浆油条'],
            'lunch': ['二食堂的番茄牛腩饭', '外卖的酸辣粉'], 'dinner': ['和同学去的麻辣香锅', '三食堂的鸡排饭'],
            'snack': ['便利店的关东煮', '橘子']}}


class Gateway:
    def __init__(self, value=None, fail=False):
        self.calls, self.value, self.fail = [], value or draft(), fail

    async def complete_structured_scoped(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.fail:
            raise RuntimeError('offline')
        return SimpleNamespace(text=json.dumps(self.value, ensure_ascii=False))


def test_plan_is_drafted_once_per_day_with_last_week_as_input(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.db')
    store.publish_day('day:old', {'location': '家里', 'activity': '练琴：乐句衔接', 'note': '在家练琴。'}, [],
                      occurred_at=datetime(2026, 9, 29, 3, tzinfo=timezone.utc), activity_kind='practice')
    gateway = Gateway()
    plan = asyncio.run(day_plan.ensure(store, gateway, now=NOW, persona='音乐专业大学生'))
    again = asyncio.run(day_plan.ensure(store, gateway, now=NOW, persona='音乐专业大学生'))
    assert plan == again and len(gateway.calls) == 1
    packet = json.loads(gateway.calls[0][0][1]['content'])
    assert '练琴：乐句衔接' in packet['recent_week']['activities']
    assert gateway.calls[0][1]['response_format'] == day_plan.FORMAT
    assert day_plan.focuses(plan, 'practice', ('乐句衔接',)) == ('乐团分谱第二乐章',)
    assert day_plan.foods(plan, 'lunch', ('鸡肉盖饭',)) == ('二食堂的番茄牛腩饭', '外卖的酸辣粉')


def test_failed_draft_falls_back_and_does_not_retry_every_refresh(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.db')
    day_plan._FAILED_AT.clear()
    gateway = Gateway(fail=True)
    assert asyncio.run(day_plan.ensure(store, gateway, now=NOW, persona='')) is None
    assert asyncio.run(day_plan.ensure(store, gateway, now=NOW, persona='')) is None
    assert len(gateway.calls) == 1
    assert day_plan.focuses(None, 'practice', ('乐句衔接',)) == ('乐句衔接',)
    day_plan._FAILED_AT.clear()


def test_slow_reasoning_draft_is_still_accepted(tmp_path, monkeypatch):
    # Drafts routinely take minutes; waiting only 60 s discarded paid, finished plans.
    store = DailyLifeStore(tmp_path / 'life.db')
    day_plan._FAILED_AT.clear()
    waited = []
    real_wait_for = asyncio.wait_for
    async def recording_wait_for(call, timeout):
        waited.append(timeout)
        return await real_wait_for(call, timeout)
    monkeypatch.setattr(day_plan.asyncio, 'wait_for', recording_wait_for)
    assert asyncio.run(day_plan.ensure(store, Gateway(), now=NOW, persona='')) is not None
    assert waited == [day_plan.PLAN_TIMEOUT_SECONDS] and waited[0] >= 240


@pytest.mark.parametrize('bad', ['{"activities": [], "meals": {}}', '[[image:linli-01]]'])
def test_invalid_draft_is_rejected(bad):
    with pytest.raises(Exception):
        day_plan.validate(json.loads(bad) if bad.startswith('{') else {**draft(), 'activities': [
            {'kind': 'practice', 'focus': bad, 'paths': draft()['activities'][0]['paths']}] * 6})


def test_world_options_use_the_plan_and_keep_it_out_of_jev_state():
    from runtime.private_world import jev_world
    plan = day_plan.validate(draft())
    data = {'allowed_activity_kinds': ['practice', 'meal'], 'world': {'schedule': {}, 'meals': []},
            'time': NOW.isoformat(), 'day_plan': plan}
    options = jev_world._activities(data)
    assert {value['focus'] for value in options.values() if value['kind'] == 'practice'} == {'乐团分谱第二乐章'}
    assert '二食堂的番茄牛腩饭' in {value['food'] for value in jev_world._meals(data).values()}
    assert 'day_plan' not in jev_world._compact_context({**data, 'persona': '[]', 'recent_life': [], 'projects': []})


def test_episode_uses_todays_authored_outcomes():
    from runtime.private_world import life_episode
    plan = day_plan.validate(draft())
    seen = {}

    class Port:
        async def ask(self, state, questions, *, purpose):
            seen.update(state=state, questions=questions)
            return {'trigger': 'own_activity', 'experience': 'plan_1:ordinary:none'}

    episode = asyncio.run(life_episode.create(Port(), 'day:new', NOW, 'reading',
        {'day_plan': plan, 'selected_activity': {'kind': 'reading', 'focus': '和声学作业第三题'}}))
    assert episode['result']['detail'] == '和声学作业第三题做完了'
    assert set(seen['state']['paths']) == {'plan_0', 'plan_1', 'pause'}
