from runtime.reply.jev_limits import JEV_MAX_INPUT_BYTES
import asyncio
from datetime import datetime, timezone

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.jev_world import decide
from runtime.private_world.world_decision import decision_context, compile_decision, LIFE_PROMPT


def test_wire_identity_aliases_keep_sources_distinct_and_host_originals_intact():
    from runtime.private_world.jev_world import _share_original_text
    source = {'projects': [
        {'id': 'long-project-identity', 'source_id': 'reply:one', 'actor': 'linli', 'quote': '明天继续练习。'},
        {'id': 'other-project-identity', 'source_id': 'reply:two', 'actor': 'user', 'quote': '明天继续练习。'}],
        'emotion': {'source_ids': ['reply:one', 'reply:two']}}
    state = {'context': source}
    _share_original_text(state)
    first, second = state['context']['projects']
    assert first['source_id'] != second['source_id']
    assert state['context']['emotion']['source_ids'] == [first['source_id'], second['source_id']]
    assert [first['actor'], second['actor']] == ['linli', 'user']
    assert first['quote'] == second['quote'] == '明天继续练习。'
    assert source['projects'][0]['id'] == 'long-project-identity'
    assert source['projects'][0]['source_id'] == 'reply:one'


def context(tmp_path, *, hour=4):
    store = DailyLifeStore(tmp_path / 'world.db')
    now = datetime(2026, 9, 28, hour, 0, tzinfo=timezone.utc)
    snapshot = store.snapshot(now)
    return decision_context({'time': now.isoformat(), 'persona': '[]',
        'world': snapshot['world'], 'rhythm': snapshot['rhythm'], 'projects': []})


class Choices:
    def __init__(self, **selections):
        self.selections = selections
        self.calls = []

    async def ask(self, state, questions, *, purpose):
        self.calls.append((state, questions, purpose))
        answers = {}
        for key, question in questions.items():
            expected = self.selections.get(key, 'none' if 'none' in question['criteria'] else next(iter(question['criteria'])))
            selected = next(k for k, v in question['criteria'].items() if expected(v)) if callable(expected) else expected
            assert selected in question['criteria']
            answers[key] = selected
        return answers


@pytest.mark.parametrize('kind,key,location', [
    ('bath_started', 'bath_started_0_home', '住处'),
    ('bath_finished', 'bath_finished_0_home', '住处'), ('shopping', 'shopping_0_shop', '店里')])
def test_same_world_choice_exposes_actual_bath_and_shopping_without_extra_call(tmp_path, kind, key, location):
    data = context(tmp_path)
    port = Choices(activity=key, project='none')
    result = asyncio.run(decide(port, data, LIFE_PROMPT))
    current, projects, meals = compile_decision(result, data)
    assert len(port.calls) == 1 and result['activity']['kind'] == kind
    assert current['location'] == location and not projects and not meals


def test_class_and_illness_constraints_survive_jev(tmp_path):
    data = context(tmp_path, hour=6)
    assert data['allowed_activity_kinds'] == ['class']
    port = Choices(activity='class', project='none')
    result = asyncio.run(decide(port, data, LIFE_PROMPT))
    current, _, _ = compile_decision(result, data)
    assert current['location'] == '学校'
    assert all(v['kind'] == 'class' for v in port.calls[0][1]['activity']['criteria'].values())
    data['rhythm']['wellbeing'] = {'state': 'unwell', 'care': 'rest'}
    # decision_context consumes the full schedule only at the runtime boundary.
    data['world']['schedule']['classes'] = []
    data = decision_context(data)
    port = Choices(activity='rest_0_home')
    result = asyncio.run(decide(port, data, LIFE_PROMPT))
    assert result['activity']['kind'] == 'rest' and result['project'] is None


def test_meals_are_variable_and_keep_frozen_context(tmp_path):
    data = context(tmp_path)
    data['emotion'] = {'reactions': [{'reaction': 'frustrated'}]}
    data['world']['meals'] = []
    for food in ('青菜肉丝面', '蔬菜饺子'):
        port = Choices(activity='meal_0_campus', meal=lambda v: v['food'] == food and v['status'] == 'eating')
        result = asyncio.run(decide(port, data, LIFE_PROMPT))
        assert result['meal']['food'] == food
        assert len(port.calls) == 1
        assert port.calls[0][0]['context']['emotion'] == data['emotion']
        compile_decision(result, data)


def test_old_meal_cannot_regress_or_change_food_mid_meal(tmp_path):
    data = context(tmp_path)
    data['world']['meals'] = [
        {'date': '2026-09-28', 'slot': 'breakfast', 'food': '包子', 'status': 'eaten', 'stale': True},
        {'date': '2026-09-28', 'slot': 'lunch', 'food': '菜饭', 'status': 'eating', 'stale': False}]
    port = Choices(activity='meal_0_home', meal=lambda v: v['slot'] == 'lunch' and v['status'] == 'eaten')
    result = asyncio.run(decide(port, data, LIFE_PROMPT))
    assert result['meal'] == {'slot': 'lunch', 'food': '菜饭', 'status': 'eaten'}
    offered = port.calls[0][1]['meal']['criteria'].values()
    assert not any(v['slot'] == 'breakfast' for v in offered)
    assert all(v['food'] == '菜饭' and v['status'] != 'planned' for v in offered if v['slot'] == 'lunch')


def test_new_project_can_complete_and_is_not_silently_omitted(tmp_path):
    data = context(tmp_path)
    port = Choices(activity='practice_0_home', project='new', project_outcome='completed')
    result = asyncio.run(decide(port, data, LIFE_PROMPT))
    assert result['project']['status'] == 'completed'
    assert result['project']['progress']
    assert result['project']['next_activity'] is None
    current, updates, _ = compile_decision(result, data)
    assert len(updates) == 1 and updates[0]['status'] == 'completed'
    assert current['activity'].startswith('练琴')


def test_terminal_and_shared_projects_are_not_reopened(tmp_path):
    data = context(tmp_path)
    data['projects'] = [{'id': 'old', 'title': '旧任务', 'status': 'completed'},
                        {'id': 'shared', 'title': '用户约定', 'status': 'ongoing', 'kind': 'shared'},
                        {'id': 'active', 'title': '本人练习', 'status': 'ongoing'}]
    port = Choices(activity='practice_1_home', project='existing_2',
                   project_outcome='difficulty', next_activity='practice_0_home')
    result = asyncio.run(decide(port, data, LIFE_PROMPT))
    options = port.calls[0][1]['project']['criteria']
    assert 'existing_0' not in options and 'existing_1' not in options
    assert result['project']['id'] == 'active'
    assert result['project']['status'] == 'paused'
    assert result['project']['next_activity']['kind'] == 'practice'
    compile_decision(result, data)


def test_rest_only_pauses_existing_project_without_inventing_progress(tmp_path):
    data = context(tmp_path)
    data['allowed_activity_kinds'] = ['rest']
    data['projects'] = [{'id': 'active', 'title': '既有练习', 'status': 'ongoing'}]
    port = Choices(activity='rest_0_home', project='existing_0', project_outcome='paused', next_activity='none')
    result = asyncio.run(decide(port, data, LIFE_PROMPT))
    assert result['project']['progress'] is None
    assert len(port.calls) == 1
    invalid = Choices(activity='rest_0_home', project='existing_0', project_outcome='completed')
    with pytest.raises(ValueError, match='JEV_WORLD_INCOMPATIBLE_OUTCOME'):
        asyncio.run(decide(invalid, data, LIFE_PROMPT))


def test_invalid_or_unavailable_choice_cannot_fabricate_a_world(tmp_path):
    data = context(tmp_path)
    class Broken:
        async def ask(self, *args, **kwargs):
            return {'activity': 'invented'}
    with pytest.raises(ValueError, match='JEV_RESPONSE_INVALID'):
        asyncio.run(decide(Broken(), data, LIFE_PROMPT))
    class Unavailable:
        async def ask(self, *args, **kwargs):
            raise ValueError('JEV_UNAVAILABLE')
    with pytest.raises(ValueError, match='JEV_UNAVAILABLE'):
        asyncio.run(decide(Unavailable(), data, LIFE_PROMPT))


def test_selected_project_stages_keep_original_scope_conditions_and_world(tmp_path):
    import copy
    import json

    data = context(tmp_path)
    selected = dict(id='concert', title='Complete concert', status='ongoing',
        scope='Three movements, not just this practice fragment',
        history=[dict(source_id='reply:1', updated_at='2026-09-27T18:00:00+08:00',
                      evidence_kind='character_statement',
                      quote='Only after all three movements are rehearsed; today just the first page.')])
    data['projects'] = [selected] + [dict(selected, id=f'other-{i}', title=f'Other project {i}') for i in range(12)]
    data['emotion'] = {'reaction': 'frustrated', 'condition': 'Still tired after practice'}
    data['exchange_actions'] = [{'reply_text': 'If recovered tomorrow, continue; this is not finished.'}]
    original = copy.deepcopy(data)
    port = Choices(activity='practice_0_home', project='existing_0',
                   project_outcome='partial', next_activity='none')
    result = asyncio.run(decide(port, data, LIFE_PROMPT))
    assert result['project']['status'] == 'ongoing'
    assert data == original
    assert port.calls[0][0]['context'] == original
    assert len(port.calls) == 1
    size = lambda value: len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())
    state, questions, purpose = port.calls[0]
    assert state['context']['projects'][0] == selected
    assert purpose == 'world-decision'
    assert 'completed' in questions['project_outcome']['criteria']
    actual = size(dict(state=state, questions=questions, purpose=purpose))
    assert actual < JEV_MAX_INPUT_BYTES
    print('world one-request bytes:', actual)
