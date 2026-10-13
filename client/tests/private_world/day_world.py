"""A synthetic Monday for the day-schedule tests: a seeded world, a day plan and a Jev port that counts requests."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

from runtime.private_world import day_plan, daily_life_runtime
from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime

MONDAY = '2026-10-12'  # classes at 9:00 and 14:00
PERSONA = json.dumps([{'declaration_id': 'anchor.residence', 'tier': 'core', 'confidence': 1.0,
                       'statement': '住在学校附近的小公寓里，是音乐专业的大学生。'}], ensure_ascii=False)
PLACES = {'home': ('家里音乐房', '家里的音乐房，钢琴靠窗'), 'campus': ('学校琴房', '学校琴房楼三层的小琴房'),
          'neighborhood': ('小区后面的河边', '住处附近的河边步道'), 'shop': ('校门口的文具店', '校门口的一家小文具店')}


def at(hour, minute=0, day=MONDAY):
    return datetime.fromisoformat(f'{day}T{hour:02d}:{minute:02d}:00+08:00').astimezone(timezone.utc)


def place(place_id, name=None):
    default = PLACES[place_id]
    return {'place_id': place_id, 'name': name or default[0], 'setting': default[1], 'stage': 'arrived'}


def synthetic_plan():
    path = lambda text, status='partial': {'obstacle': '一处不太顺', 'response': '放慢再来一遍', 'status': status, 'outcome': text}
    kinds = {'practice': (['乐团分谱第二乐章', '左手两处衔接', '视奏片段'], 'home'), 'reading': (['和声学作业第三题', '图书馆借的传记'], 'home'),
             'creative': (['随手记的四小节动机', '钢琴小品的主题'], 'home'), 'housework': (['阳台上的衣物', '书桌和书架'], 'home'),
             'walk': (['校园东门的银杏道', '小区后面的河边'], 'neighborhood'), 'errand': (['菜鸟驿站的快递'], 'shop'),
             'shopping': (['校门口的文具店'], 'shop')}
    activities = [{'kind': kind, 'focus': focus, 'place': place(where), 'paths': [path(focus + '做了一部分'), path(focus + '做完了', 'completed')]}
                  for kind, (foci, where) in kinds.items() for focus in foci]
    meal = lambda food, mode, where: {'food': food, 'mode': mode,
                                      'place': place(where, {'home': '家里厨房', 'campus': '二食堂', 'shop': '楼下小店'}[where])}
    meals = {'breakfast': [meal('二食堂的豆浆油条', 'canteen', 'campus'), meal('楼下的煎饼果子', 'takeaway', 'shop')],
             'lunch': [meal('二食堂的番茄牛腩饭', 'canteen', 'campus'), meal('外卖的酸辣粉', 'delivery', 'home'), meal('自己炒的青菜肉丝面', 'cook', 'home')],
             'dinner': [meal('三食堂的鸡排饭', 'canteen', 'campus'), meal('和同学去的麻辣香锅', 'dine_out', 'shop'), meal('自己做的馄饨', 'cook', 'home')],
             'snack': [meal('便利店的关东煮', 'takeaway', 'shop'), meal('橘子', 'cook', 'home')]}
    return day_plan.validate({'activities': activities, 'meals': meals})


def seed(store, day=MONDAY):
    """A quiet evening before the day, and the day's candidate plan."""
    start = datetime.fromisoformat(f'{day}T00:00:00+08:00')
    store.publish_day('day:seed', {'location': '家里', 'activity': '休息', 'note': '在家里休息。'}, [],
                      occurred_at=(start - timedelta(hours=2)).astimezone(timezone.utc), activity_kind='rest')
    with store._db() as db:
        day_plan.initialize(db)
        db.execute('INSERT OR REPLACE INTO life_day_plans VALUES (?,?)', (day, json.dumps(synthetic_plan(), ensure_ascii=False)))


class Clock(datetime):
    """Stands in for the runtime's datetime so that 'now' is the simulated time."""
    current = None

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current


class Port:
    """Counts every request. By default each question gets its first option, a block question rotates."""

    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer
        self.rotation = 0

    async def ask(self, state, questions, *, purpose):
        self.calls.append({'purpose': purpose, 'state': json.loads(json.dumps(state)), 'questions': json.loads(json.dumps(questions))})
        if self.answer is not None:
            given = self.answer(state, questions, purpose)
            if given is not None:
                return given
        return self.default(state, questions, purpose)

    def default(self, state, questions, purpose):
        answers = {}
        for key, question in questions.items():
            names = list(question['criteria'])
            if key.startswith('block_'):
                self.rotation += 1
                answers[key] = names[self.rotation % len(names)]
            elif (key.startswith('project_') or purpose == 'exchange-world-check') and 'none' in question['criteria']:
                answers[key] = 'none'
            else:
                answers[key] = names[0]
        return answers

    def count(self, purpose):
        return sum(1 for call in self.calls if call['purpose'] == purpose)

    def purposes(self):
        return [call['purpose'] for call in self.calls]


def make_world(tmp_path, monkeypatch, port=None, *, seeded=True, clock=True):
    """A store and runtime wired to the fake port; the exchange extraction itself is answered empty."""
    import runtime.reply.jev_questions as jev_questions
    port = port or Port()
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
    monkeypatch.setattr(daily_life_runtime, 'configured_duties', lambda: None)
    if clock:
        monkeypatch.setattr(daily_life_runtime, 'datetime', Clock)
    store = DailyLifeStore(tmp_path / 'life.db')
    if seeded:
        seed(store)
    runtime = DailyLifeRuntime(store, lambda: None, lambda: PERSONA)
    original = runtime._complete

    async def complete(prompt, data, request_id, **kwargs):
        if request_id.startswith('life:'):
            return {'updates': [], 'current_quote': None, 'relationship': None, 'routine': None, 'boundaries': []}
        return await original(prompt, data, request_id, **kwargs)

    runtime._complete = complete
    runtime.refreshes = []
    runtime.schedule_refresh = lambda now, **kwargs: runtime.refreshes.append(now)
    return store, runtime, port


def tick(runtime, when):
    Clock.current = when
    asyncio.run(runtime.refresh(when))


def exchange(runtime, n, when, user, reply, origin='user'):
    Clock.current = when
    return asyncio.run(runtime.consume_exchange(f'reply:t{n}:1', user, reply, occurred_at=when, origin=origin))


def moments(store):
    with store._db() as db:
        return [json.loads(payload) | {'at': at_} for payload, at_ in db.execute(
            "SELECT payload, occurred_at FROM life_moments WHERE kind='daily' ORDER BY occurred_at, source_id")]
