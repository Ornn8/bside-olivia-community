"""A world decision must fit the upstream however long her journal is.

The upstream refuses world decisions near 70 KB (it counts tokens, not bytes). The request
used to carry every open project with its change history, so it grew with the journal until
it was refused or failed with JEV_INPUT_TOO_LARGE.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from runtime.private_world import daily_life, project_timing
from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.jev_world import decide
from runtime.private_world.world_decision import LIFE_PROMPT, decision_context

LOCAL = timezone(timedelta(hours=8))
END = datetime(2026, 10, 10, 6, tzinfo=timezone.utc)
TARGET = 45_000


def journal(path, days, *, open_per_day=3, closed_per_day=3):
    """Days of life and chat: published moments, and replies that promise things she never closes."""
    store = DailyLifeStore(path)
    opened, count = [], 0
    for day in range(days):
        start = END - timedelta(days=days - day)
        for slot in range(4):
            store.publish_day(f'day-{day}-{slot}', {'location': '琴房', 'activity': f'练琴{slot}', 'note': '在琴房练习，感觉还可以'},
                              [], occurred_at=start + timedelta(hours=7 + slot * 3))
        for slot in range(open_per_day + closed_per_day):
            count += 1
            quote = f'我会把第{count}件事做完'
            updates = [{'id': f'commit-{count}', 'title': f'第{count}件事的计划', 'detail': f'打算处理第{count}件事',
                        'status': 'planned' if slot < open_per_day else 'completed', 'kind': 'linli', 'actor': 'linli', 'quote': quote}]
            reply = f'好，{quote}，到时候告诉你。'
            if opened and slot < 2:  # revisit an older promise: it grows a change history
                old = opened[(count * 7) % len(opened)]
                reply += f'再看看{old}'
                updates.append({'id': old, 'title': f'第{old}件事的计划', 'detail': '继续推进中', 'status': 'ongoing',
                                'kind': 'linli', 'actor': 'linli', 'quote': f'再看看{old}'})
            store.record_exchange(f'reply:x-{day}-{slot}:1', f'聊聊第{count}件事', reply, updates,
                                  occurred_at=start + timedelta(hours=9, minutes=slot * 17))
            if slot < open_per_day:
                opened.append(f'commit-{count}')
    return store


def inflate_world(world, now):
    """The most her recent past can hold: a week of meals and the six latest episodes."""
    local = now.astimezone(LOCAL)
    meals = [{'date': (local - timedelta(days=back)).date().isoformat(), 'slot': slot, 'food': '米饭和番茄炒蛋', 'status': 'eaten',
              'mode': 'home', 'meal_stage': 'done', 'place': {'place_id': 'home', 'name': '家里', 'stage': 'arrived'},
              'occurred_at': local.isoformat(), 'started_at': local.isoformat(), 'ended_at': local.isoformat(),
              'scheduled_for': local.isoformat(), 'stale': False, 'source_id': f'meal:{back}:{slot}'}
             for back in range(7) for slot in ('breakfast', 'lunch', 'dinner', 'snack')]
    episodes = [{'schema': 'character-life-episode/1', 'source_id': f'day-{i}', 'occurred_at': local.isoformat(), 'actor': 'character',
                 'activity_kind': 'practice', 'trigger': '想把上次没弹顺的部分再练一遍' * 3,
                 'process': [{'obstacle': '左手跳进不稳' * 2, 'response': '放慢速度分手练习' * 2, 'outcome': '稍微顺一点了' * 2}] * 3,
                 'result': {'status': 'partial', 'detail': '练了一个小时，进步不大但手感回来了。' * 2},
                 'interpretation': {'subjective': True, 'text': '觉得有点累但还想继续' * 3}, 'effects': {}} for i in range(6)]
    return {**world, 'meals': meals, 'recent_episodes': episodes}


def request(store, now):
    """The wire request one world tick sends, with the persona and emotion at the sizes they reach."""
    snapshot = store.snapshot(now)
    persona = json.dumps([{'declaration_id': f'anchor.{i}', 'tier': 'A', 'confidence': 0.9,
                           'statement': '林离是一名音乐专业的大学生，住在家里，每天练琴阅读。' * 6} for i in range(12)], ensure_ascii=False)
    projects, coverage = store.world_projects(now)
    data = {'time': now.astimezone(LOCAL).isoformat(), 'persona': persona, 'character_development': store.development_view(now),
            'world': inflate_world(snapshot['world'], now), 'recent_life': store.recent_life(now) * 6,
            'exchange_actions': store.pending_exchange_actions(now, None), 'previous': snapshot['current'],
            'projects': projects, 'project_coverage': coverage, 'rhythm': snapshot['rhythm'],
            'recent_observations': store._recent_observations(now)}
    data = decision_context(data)
    data['emotion'] = {
        'reaction_subject': 'character', 'interpretation_only': True,
        'reactions': [{'source_id': f's{i}', 'occurred_at': now.isoformat(), 'quote': '今天练琴的时候有点烦躁，总是弹错',
                       'reaction': 'frustrated', 'goal_or_need': '按自己的节奏继续', 'action_tendency': 'rest'} for i in range(12)],
        'concerns': [{'id': f'emotion:{i}', 'summary': '在意明天的考试安排还没有确定', 'source_ids': [f's{j}' for j in range(24)],
                      'source_count': 24, 'occurred_at': now.isoformat()} for i in range(12)],
        'reported_affects': [{'source_id': f's{i}', 'occurred_at': now.isoformat(), 'subject': 'user', 'quote': '我今天有点累',
                              'affect': 'sad'} for i in range(12)],
        'current_affect': {'status': 'available', 'label': 'calm', 'intensity': 'low', 'as_of': now.isoformat(),
                           'reason': '近期真实生活经历的影响', 'basis': {'context_digest': 'x' * 64, 'kind': 'life', 'source_ids': ['a']}}}
    sent = []

    class Port:
        async def ask(self, state, questions, *, purpose):
            sent.append(len(json.dumps(dict(state=state, questions=questions, purpose=purpose),
                                       ensure_ascii=False, separators=(',', ':')).encode()))
            return {key: ('none' if 'none' in question['criteria'] else next(iter(question['criteria'])))
                    for key, question in questions.items()}
    asyncio.run(decide(Port(), data, LIFE_PROMPT))
    return sent[0], data


def test_world_decision_request_stays_under_the_target_whatever_the_journal_holds(tmp_path):
    short = journal(tmp_path / 'short.sqlite3', 3)
    long = journal(tmp_path / 'long.sqlite3', 45)
    now = END - timedelta(minutes=5)
    short_size, short_data = request(short, now)
    long_size, long_data = request(long, now)
    assert short_size <= TARGET and long_size <= TARGET
    assert abs(long_size - short_size) <= 6000  # a few more projects fit, never the whole journal
    assert len(long_data['projects']) <= daily_life.WORLD_OPEN_PROJECTS + daily_life.WORLD_FINISHED_PROJECTS
    assert long_data['project_coverage']['open_omitted'] > 0 and long_data['project_coverage']['finished_omitted'] > 0
    with long._db() as db:  # nothing is lost from the journal itself
        assert db.execute("SELECT COUNT(*) FROM life_moments WHERE kind='exchange'").fetchone()[0] == 45 * 6


def test_world_projects_are_open_first_newest_first_with_history_only_for_the_newest(tmp_path):
    store = journal(tmp_path / 'life.sqlite3', 20)
    now = END - timedelta(minutes=5)
    projects, coverage = store.world_projects(now)
    opened = [p for p in projects if p['status'] not in {'completed', 'cancelled'}]
    finished = [p for p in projects if p['status'] in {'completed', 'cancelled'}]
    assert len(opened) == daily_life.WORLD_OPEN_PROJECTS and coverage['open_shown'] == len(opened)
    assert coverage['open_omitted'] > 0
    stamps = [p['updated_at'] for p in opened]
    assert stamps == sorted(stamps, reverse=True)
    assert sum('history' in p for p in opened) == daily_life.WORLD_HISTORY_PROJECTS
    assert all('history' in p for p in opened[:daily_life.WORLD_HISTORY_PROJECTS])
    assert len(finished) <= daily_life.WORLD_FINISHED_PROJECTS
    assert all(set(p) == {'id', 'title', 'status', 'evidence_kind'} for p in finished)
    assert coverage['finished_shown'] == len(finished)


def test_a_dated_commitment_outranks_newer_undated_projects(tmp_path):
    store = journal(tmp_path / 'life.sqlite3', 12)
    now = END - timedelta(minutes=5)
    oldest = min((p for p in store.exchange_state(now=now, include_history=True)['projects']
                  if p['status'] not in {'completed', 'cancelled'}), key=lambda p: p['updated_at'])
    deadline = (now + timedelta(days=3)).isoformat()
    with store._db() as db:
        db.execute('INSERT INTO life_project_timing VALUES (?,?)', (project_timing.version(oldest), json.dumps(
            {'id': oldest['id'], 'scope': 'bounded', 'deadline_at': deadline, 'source_at': oldest['updated_at'],
             'version': project_timing.version(oldest)})))
    projects, _ = store.world_projects(now)
    assert projects[0]['id'] == oldest['id'] and projects[0]['deadline_at'] == deadline
    assert len([p for p in projects if p['status'] not in {'completed', 'cancelled'}]) == daily_life.WORLD_OPEN_PROJECTS


def test_a_short_journal_is_listed_whole(tmp_path):
    store = journal(tmp_path / 'life.sqlite3', 1, open_per_day=2, closed_per_day=2)
    now = END - timedelta(minutes=5)
    projects, coverage = store.world_projects(now)
    assert coverage['open_omitted'] == 0 and coverage['finished_omitted'] == 0
    assert {p['id'] for p in projects} == {f'commit-{n}' for n in range(1, 5)}


def test_the_journal_is_long_enough_to_overflow_the_request_without_the_bound(tmp_path, monkeypatch):
    long = journal(tmp_path / 'long.sqlite3', 45)
    monkeypatch.setattr(DailyLifeStore, 'world_projects',
                        lambda self, now: (self.exchange_state(now=now, include_history=True)['projects'], {}))
    try:
        size, _ = request(long, END - timedelta(minutes=5))
    except ValueError as error:
        assert str(error) == 'JEV_INPUT_TOO_LARGE'  # what the unbounded request ended in
    else:
        assert size > TARGET
