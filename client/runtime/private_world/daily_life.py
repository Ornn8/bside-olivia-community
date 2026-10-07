"""Persistent, user-isolated character life; not a second user memory store.

Only public character moments live here. Relationship permissions and Mem0
facts stay with their existing owners. Reading never invents elapsed events.
"""
from __future__ import annotations

from contextlib import contextmanager
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from runtime.memory.private_world_relationship import validate_exchange_relationship, validate_boundary_changes
from runtime.reply.media_delivery import validate_delivery, grouped_delivery_evidence, MEDIA_EVIDENCE_MEANING

# A reply can change several independent promises; autonomous day planning
# retains its separate three-project limit.
MAX_EXCHANGE_UPDATES = 12
from runtime.private_world.life_rhythm import rhythm, LOCAL
from statistics import median
from runtime.private_world.student_world import student_schedule, weather_view
from runtime.private_world.world_decision import KINDS as _ACTIVITY_KINDS


_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_STATUSES = {"planned", "ongoing", "paused", "completed", "cancelled", "awaiting_user"}
_EXCHANGE_UPDATE_FIELDS = frozenset({"id", "title", "detail", "status", "kind", "actor", "quote"})
_VISIBLE = "(kind IN ('daily','media','image') OR json_array_length(payload,'$.updates') > 0 OR json_type(payload,'$.current')='object')"
# Recent observations remain bounded for reply context; publication timing is
# derived from the last published moment instead of a fixed six-hour deadline.
RECENT_OBSERVATION_WINDOW = timedelta(hours=6)
# Ended threads stay reply candidates this long; older ones remain in the
# journal and in memory recall, but no longer grow every reply's world context.
ENDED_THREAD_WINDOW = timedelta(days=7)
# Observation validity, not an inferred duration or proof of completion.
_SHORT_ACTIVITY_MINUTES = {"meal": 60, "walk": 90, "errand": 90, "housework": 90}


def _refresh_delay(source_id: str) -> timedelta:
    spread = int.from_bytes(hashlib.sha256(source_id.encode("utf-8")).digest()[:2], "big") % 121
    return timedelta(minutes=180 + spread)  # A stable 3-5 hours per moment.


# Every reconsideration is a paid judgment. Repeating the same activity backs
# off (x2 per repeat, at most four hours) and, while the user has not written
# for a while, life advances only every 3-5 hours.
_UNCHANGED_BACKOFF_CAP = timedelta(hours=4)
IDLE_AFTER = timedelta(hours=2)


def _activity_refresh_delay(current: dict, *, repeats: int = 0, idle: bool = False) -> timedelta:
    kind = current.get("activity_kind")
    if kind == 'bath_started':
        return timedelta(minutes=20)  # Reconsider the actual bath; never finish it by clock.
    if kind is None and current.get("meals"):
        kind = "meal"  # Older published records already carry structured meals.
    # Reconsider an awake activity at a bounded cadence. This only expires
    # the observation; a new decision/episode must still establish what
    # happens next, and the runtime continues to protect sleep and bathing.
    if kind in {"rest", "practice", "reading", "creative", "housework", "walk", "errand", "bath_finished", "shopping"}:
        base = timedelta(minutes=60)
    elif kind in _SHORT_ACTIVITY_MINUTES:
        base = timedelta(minutes=_SHORT_ACTIVITY_MINUTES[kind])
    else:
        base = _refresh_delay(current["source_id"])
    delay = min(base * 2 ** min(max(repeats, 0), 3), max(base, _UNCHANGED_BACKOFF_CAP))
    return max(delay, _refresh_delay(current["source_id"])) if idle else delay


def _user_idle(exchanges, now: datetime) -> bool:
    """No message from the user in the last IDLE_AFTER (or none at all)."""
    return not exchanges or now - max(received for received, _ in exchanges) >= IDLE_AFTER


def _same_activity_repeats(db, current: dict | None, now: datetime) -> int:
    """How many earlier published moments in a row share the current activity kind."""
    kind = current.get("activity_kind") if current else None
    if not kind:
        return 0
    repeats = 0
    for (payload,) in db.execute(
            "SELECT payload FROM life_moments WHERE kind='daily' AND occurred_at<=? "
            "ORDER BY occurred_at DESC, source_id DESC LIMIT 9", (_time(now),)):
        if json.loads(payload).get("activity_kind") != kind:
            break
        repeats += 1
    return max(repeats - 1, 0)


def _meal_observation(meal: dict, now: datetime) -> dict:
    basis = meal.get('scheduled_for') if meal['status'] == 'planned' else None
    until = (datetime.fromisoformat(basis or meal["occurred_at"]) + timedelta(minutes=_SHORT_ACTIVITY_MINUTES["meal"])
             if meal["status"] in {"planned", "eating"} else None)
    return {**meal, "stale": until is not None and now >= until,
            "valid_until": _time(until) if until is not None else None}


# Common conversational/time words are not evidence that a task is relevant.
_QUERY_STOP_WORDS = set("今天 明天 昨天 晚上 现在 这次 上次 已经 还是 一下 一些 一点 我们 你们 我的 你的 她的 自己 时候 最近 然后 但是 还有 就是 觉得 可以 没有 怎么 什么 这个 那个 这件 那件".split())
# Single-character recall uses grammatical/time words, never an activity lexicon.
_QUERY_TOPIC_STOP_CHARS = set(''.join(_QUERY_STOP_WORDS)) | set(
    '我你他她它们的了着过吗呢吧啊呀哦嗯是有在也都就还又再才先不没无别能会要该'
    '让把被将和与或及而从对为来去给做打说想看好很更最时间年月日周早午晚'
    '一二三四五六七八九十百千万零两几每各打算计划准备完成')

_MEAL_WINDOWS = {
    "breakfast": (5 * 60, 11 * 60 + 30),
    "lunch": (10 * 60 + 30, 16 * 60),
    "dinner": (16 * 60 + 30, 23 * 60),
}
_MEAL_WORDS = {
    "breakfast": r"(?:早餐|早饭)",
    "lunch": r"(?:午饭|午餐)",
    "dinner": r"(?:晚饭|晚餐)",
}
_MEAL_DONE = r"(?:正在吃|正吃|刚吃(?:完|过)?|吃完(?:了)?|吃过(?:了)?|已经.{0,6}吃(?:完|过)?|收尾|解决(?:了)?|搞定(?:了)?|用过(?:了)?)"


def _current_time_consistent(note: object, occurred_at: object) -> bool:
    """Reject impossible current meal claims without treating user wording as time."""
    if not isinstance(note, str) or not note.strip():
        return True
    try:
        when = occurred_at if isinstance(occurred_at, datetime) else datetime.fromisoformat(str(occurred_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    if when.tzinfo is None:
        return True
    minute = when.astimezone(LOCAL).hour * 60 + when.astimezone(LOCAL).minute
    for kind, word in _MEAL_WORDS.items():
        if re.search(word + r".{0,16}" + _MEAL_DONE, note) or re.search(_MEAL_DONE + r".{0,16}" + word, note):
            start, end = _MEAL_WINDOWS[kind]
            return start <= minute < end
    return True



_FINISHED_IDENTITY_DAYS = 14


def _recently_finished(item: dict, now: datetime) -> bool:
    """A finished item stays matchable for two weeks; older ones return only when mentioned."""
    try:
        stamp = datetime.fromisoformat(str(item.get("updated_at")).replace("Z", "+00:00"))
    except ValueError:
        return True  # Unknown time: keep the identity rather than mint a duplicate.
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return now - stamp <= timedelta(days=_FINISHED_IDENTITY_DAYS)


def _time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("DAILY_LIFE_TIME_INVALID")
    return value.astimezone(timezone.utc).isoformat()


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("DAILY_LIFE_TEXT_INVALID")
    if any(ord(c) < 32 for c in value):
        raise ValueError("DAILY_LIFE_TEXT_INVALID")
    return value.strip()


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("DAILY_LIFE_ID_INVALID")
    return value


def _source_quote(value: object, source: str) -> str:
    """Keep one exact source span, including material omitted by extraction."""
    if (not isinstance(value, str) or not value.strip() or len(value) > 240
            or any(ord(c) < 32 and c not in "\r\n" for c in value)):
        raise ValueError("DAILY_LIFE_TEXT_INVALID")
    quote = value.strip()
    if quote in source:
        return quote
    parts = [part.strip() for part in re.split(r"(?<=[。！？.!?])", quote) if part.strip()]
    if len(parts) < 2:
        raise ValueError("DAILY_LIFE_EVIDENCE_INVALID")
    start, end = None, 0
    for part in parts:
        position = source.find(part)
        if position < end or position != source.rfind(part) or position < 0:
            raise ValueError("DAILY_LIFE_EVIDENCE_INVALID")
        if start is None:
            start = position
        end = position + len(part)
    restored = source[start:end]
    if len(restored) > 240 or any(ord(c) < 32 and c not in "\r\n" for c in restored):
        raise ValueError("DAILY_LIFE_EVIDENCE_INVALID")
    return restored


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _explicitly_unfinished(quote: str) -> bool:
    """Reject clear prospective/negative evidence, not infer completion from prose."""
    return bool(re.match(r'^(?:我(?:们)?(?:这边)?(?:也|还|现在|今天)?|现在|今天)?(?:还没|尚未|暂未|打算|计划|准备(?!好))', quote.strip()))


def _current_source_quote(value: object, source: str) -> str:
    quote = _text(value, 180)
    if quote in source:
        return quote
    # An extractor sometimes terminates the first clause with a period. Recover
    # the entire original sentence, never discard following qualifications.
    if not quote.endswith("。"):
        raise ValueError("DAILY_LIFE_EVIDENCE_INVALID")
    prefix = quote[:-1]
    start = source.find(prefix)
    if (not prefix or start < 0 or start != source.rfind(prefix)
            or (start > 0 and source[start - 1] not in "。！？\r\n")):
        raise ValueError("DAILY_LIFE_EVIDENCE_INVALID")
    end = start + len(prefix)
    if end >= len(source) or source[end] != "，":
        raise ValueError("DAILY_LIFE_EVIDENCE_INVALID")
    while end < len(source) and source[end] not in "。！？\r\n":
        end += 1
    if end >= len(source) or source[end] != "。":
        raise ValueError("DAILY_LIFE_EVIDENCE_INVALID")
    return _text(source[start:end + 1], 180)


def _project(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"id", "title", "detail", "status"}:
        raise ValueError("DAILY_LIFE_PROJECT_INVALID")
    if value["status"] not in _STATUSES:
        raise ValueError("DAILY_LIFE_STATUS_INVALID")
    return {"id": _identifier(value["id"]), "title": _text(value["title"], 60),
            "detail": _text(value["detail"], 240), "status": value["status"]}


def validate_exchange_updates(source_id, user_text, reply_text, updates, *, stamp, origin):
    """Check new episode identities before evaluation; storage repeats this check."""
    if not isinstance(updates, list) or len(updates) > MAX_EXCHANGE_UPDATES:
        raise ValueError("DAILY_LIFE_UPDATES_INVALID")
    checked = []
    for update in updates:
        if not isinstance(update, dict) or set(update) != _EXCHANGE_UPDATE_FIELDS:
            raise ValueError("DAILY_LIFE_UPDATE_INVALID")
        item = _project({k: update[k] for k in ("id", "title", "detail", "status")})
        actor, kind = update["actor"], update["kind"]
        if actor not in {"user", "linli"} or kind not in {"linli", "shared"} or (actor == "user" and kind != "shared"):
            raise ValueError("DAILY_LIFE_ACTOR_INVALID")
        if origin == "proactive" and (actor == "user" or (kind == "shared" and item["status"] != "awaiting_user")):
            raise ValueError("DAILY_LIFE_PROACTIVE_UPDATE_INVALID")
        quote = _source_quote(update["quote"], user_text if actor == "user" else reply_text)
        if item['status'] == 'completed' and _explicitly_unfinished(quote):
            raise ValueError('DAILY_LIFE_PHASE_CONFLICT')
        item.update(kind=kind, actor=actor, quote=quote, source_id=source_id, updated_at=stamp)
        checked.append(item)
    if len({p["id"] for p in checked}) != len(checked):
        raise ValueError("DAILY_LIFE_UPDATES_INVALID")
    return checked


def _thread_ended_at(project: dict) -> datetime | None:
    """When a thread stopped being live: finished, cancelled or a lapsed short-term item."""
    if project.get("status") in {"completed", "cancelled"}:
        stamp = project.get("updated_at")
    elif project.get("deadline_expired") and project.get("time_scope") == "transient":
        stamp = project.get("deadline_at") or project.get("valid_until") or project.get("updated_at")
    else:
        return None
    try:
        ended = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)  # Ended at an unknown time: treat as old.
    return ended if ended.tzinfo else ended.replace(tzinfo=timezone.utc)


def _live_threads(projects: list[dict], now: datetime) -> list[dict]:
    kept = []
    for project in projects:
        ended = _thread_ended_at(project)
        if ended is None or now - ended <= ENDED_THREAD_WINDOW:
            kept.append(project)
    return kept


def _project_evidence(project: dict) -> dict:
    if project.get('evidence_kind') == 'initial_plan':
        return dict(project)
    # An extractor's paraphrase is not something either participant said.
    if 'quote' in project:
        return {**project, 'detail': project['quote'], 'evidence_kind':
                'user_statement' if project.get('actor') == 'user' else 'character_statement'}
    return {**project, 'evidence_kind': 'published_life'}


def _current_evidence(current: dict | None) -> dict | None:
    # Older quote records put UI labels in factual fields. Normalize the view,
    # preserving the stored quotation, provenance and original journal bytes.
    if (current and str(current.get("source_id", "")).startswith("reply:")
            and current.get("location") == "她刚在信里说"
            and current.get("activity") == "新的近况"):
        current = {**current, "location": None, "activity": None}
    if current and not _current_time_consistent(current.get("note"), current.get("occurred_at")):
        return None
    return current


def _query_tokens(text: str) -> set[str]:
    tokens = set()
    for part in re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", text.lower()):
        if re.fullmatch(r"[\u3400-\u9fff]{2,}", part):
            tokens.update(part[i:i + 2] for i in range(len(part) - 1))
        else:
            tokens.add(part)
    return tokens - _QUERY_STOP_WORDS


def _query_topic_chars(text: str) -> set[str]:
    return set(re.findall(r"[\u3400-\u9fff]", text)) - _QUERY_TOPIC_STOP_CHARS


class DailyLifeStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS life_moments (
                    source_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS life_projects (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS life_current (
                    id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS life_exchange_world_gate (
                    source_id TEXT PRIMARY KEY, decision TEXT NOT NULL, reply_text TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS life_exchange_user_text (
                    source_id TEXT PRIMARY KEY, user_text TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS life_exchange_world_gate_versions (
                    source_id TEXT PRIMARY KEY, version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS life_rest_exchanges (
                    source_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, replied_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS life_routine_days (
                    day TEXT PRIMARY KEY, shift_minutes INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS life_user_routine (
                    source_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS life_addressing (
                    source_id TEXT NOT NULL, kind TEXT NOT NULL, quote TEXT NOT NULL,
                    occurred_at TEXT NOT NULL, PRIMARY KEY(source_id, kind));
                CREATE TABLE IF NOT EXISTS life_weather (
                    fetched_at TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS life_media_reservations (
                    source_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL,
                    task_id TEXT NOT NULL, verified_at TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS life_baths (
                    source_id TEXT PRIMARY KEY, started_at TEXT NOT NULL,
                    completed_source_id TEXT UNIQUE, completed_at TEXT);
                CREATE TABLE IF NOT EXISTS character_development_topics (
                    key TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS character_development_events (
                    source_id TEXT NOT NULL, topic TEXT NOT NULL,
                    occurred_at TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (source_id,topic));
                CREATE INDEX IF NOT EXISTS life_moments_chronology ON life_moments(occurred_at DESC, source_id DESC);
            """)
            from .meal_lifecycle import initialize as initialize_meals
            initialize_meals(db)
            from .life_episode import initialize as initialize_episodes
            initialize_episodes(db)
            from .project_timing import initialize as initialize_project_timing
            initialize_project_timing(db)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def has_source(self, source_id: str) -> bool:
        with self._db() as db:
            return db.execute("SELECT 1 FROM life_moments WHERE source_id=?", (source_id,)).fetchone() is not None

    def configure_development(self, persona_json: str) -> list[dict]:
        from .character_development import configure
        with self._db() as db:
            return configure(db, persona_json)

    def development_view(self, as_of: datetime) -> dict:
        from .character_development import view
        _time(as_of)
        with self._db() as db:
            db.execute('BEGIN')
            return view(db, as_of)

    def development_world_assessment(self, now: datetime) -> dict:
        from .character_development import world_assessment
        _time(now)
        with self._db() as db:
            db.execute('BEGIN')
            return world_assessment(db, now)

    def development_episodes(self, as_of: datetime) -> list[dict]:
        from .character_development import episodes
        with self._db() as db:
            return episodes(db, _time(as_of))

    def development_exchange_context(self, as_of: datetime) -> dict:
        from .character_development import episodes, withdrawal_candidates
        stamp = _time(as_of)
        with self._db() as db:
            db.execute('BEGIN')
            return {'episodes': episodes(db, stamp),
                    'withdrawal_candidates': withdrawal_candidates(db, as_of)}

    def record_weather(self, weather: dict, now: datetime) -> None:
        # This observation exists independently of whether life generation succeeds.
        if weather_view(weather, now)['status'] != 'fresh':
            return
        with self._db() as db:
            db.execute('INSERT OR REPLACE INTO life_weather VALUES (?,?)', (_time(now), _json(weather)))
            db.execute('DELETE FROM life_weather WHERE fetched_at<?', (_time(now-timedelta(days=7)),))

    def adapt_routine(self, now: datetime, *, affinity: float) -> None:
        """At most 15 minutes per active day, from >=3 distinct evening dates.

        This learns availability, not an assertion about the user's bedtime.
        A new plan starts at the next bedtime window, never during its bath.
        """
        _time(now)
        affinity = max(0.0, min(1.0, float(affinity)))
        local = now.astimezone(LOCAL)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            day = local.date()
            active = db.execute('SELECT shift_minutes FROM life_routine_days WHERE day<=? ORDER BY day DESC LIMIT 1', (day.isoformat(),)).fetchone()
            bath_start = datetime.combine(day, datetime.min.time(), tzinfo=LOCAL) + timedelta(hours=23, minutes=30 + (active[0] if active else 0))
            if local >= bath_start:
                day += timedelta(days=1)
            if db.execute('SELECT 1 FROM life_routine_days WHERE day=?', (day.isoformat(),)).fetchone():
                return
            old = db.execute('SELECT shift_minutes FROM life_routine_days WHERE day<? ORDER BY day DESC LIMIT 1', (day.isoformat(),)).fetchone()
            previous = old[0] if old else 0
            daily = {}
            for row in db.execute('SELECT received_at FROM life_rest_exchanges WHERE received_at>=? AND received_at<?',
                                  (_time(now - timedelta(days=7)), _time(now))):
                stamp = datetime.fromisoformat(row[0]).astimezone(LOCAL)
                minute = stamp.hour * 60 + stamp.minute
                if minute < 180:
                    minute += 1440
                if 21 * 60 <= minute <= 27 * 60:
                    evening = (stamp - timedelta(hours=12)).date()
                    daily[evening] = max(daily.get(evening, 0), minute)
            target = previous
            preference = db.execute('SELECT payload FROM life_user_routine WHERE occurred_at<=? ORDER BY occurred_at DESC, source_id DESC LIMIT 1', (_time(now),)).fetchone()
            preference = json.loads(preference[0]) if preference else None
            if affinity < 0.2:
                target = 0
            elif preference and preference['sleep_minute'] is not None:
                minute = (preference['sleep_minute'] - preference['utc_offset_minutes'] + 480) % 1440
                offset = (minute - 24 * 60 + 720) % 1440 - 720
                target = round(max(-60, min(120, offset)) * affinity)
            elif len(daily) >= 3:
                target = round(max(-60, min(120, median(daily.values()) - 24 * 60)) * affinity)
            shift = previous + max(-15, min(15, target - previous))
            db.execute('INSERT INTO life_routine_days VALUES (?,?)', (day.isoformat(), shift))

    def publish_day(self, source_id: str, current: dict, projects: list, *, occurred_at: datetime, meals: list | None = None, weather: dict | None = None, activity_kind: str | None = None, development: list | None = None, development_basis: dict | None = None, episode: dict | None = None, project_timing: list | None = None) -> bool:
        _identifier(source_id)
        stamp = _time(occurred_at)
        if activity_kind is not None and activity_kind not in _ACTIVITY_KINDS:
            raise ValueError("DAILY_LIFE_ACTIVITY_KIND_INVALID")
        if not isinstance(current, dict) or not {"location", "activity", "note"} <= set(current) <= {"location", "activity", "note", "place"}:
            raise ValueError("DAILY_LIFE_CURRENT_INVALID")
        if activity_kind == 'bath_started' and (not episode or episode.get('activity_kind') != 'bath_started'
                or episode.get('result', {}).get('status') != 'partial'):
            raise ValueError('DAILY_LIFE_BATH_EPISODE_REQUIRED')
        if activity_kind == 'bath_finished' and (not episode or episode.get('activity_kind') != 'bath_finished'
                or episode.get('result', {}).get('status') != 'completed'):
            raise ValueError('DAILY_LIFE_BATH_EPISODE_REQUIRED')
        if activity_kind == 'shopping' and (not episode or episode.get('activity_kind') != 'shopping'):
            raise ValueError('DAILY_LIFE_SHOPPING_EPISODE_REQUIRED')
        from .place_detail import validate as validate_place, label as place_label
        place = validate_place(current['place']) if 'place' in current else None
        if place and current['location'] != place_label(place):
            raise ValueError('DAILY_LIFE_PLACE_INVALID')
        current = {k: _text(current[k], 180 if k == "note" else 60) for k in ('location', 'activity', 'note')}
        if place:
            current['place'] = place
        if not isinstance(projects, list) or len(projects) > 3:
            raise ValueError("DAILY_LIFE_PROJECTS_INVALID")
        checked = [_project(p) for p in projects]
        checked_meals = []
        if meals is not None:
            if not isinstance(meals, list) or len(meals) > 3:
                raise ValueError('DAILY_LIFE_MEALS_INVALID')
            for meal in meals:
                if not isinstance(meal, dict) or set(meal) != {'slot', 'food', 'status'}:
                    raise ValueError('DAILY_LIFE_MEALS_INVALID')
                if meal['slot'] not in {'breakfast', 'lunch', 'dinner', 'snack'} or meal['status'] not in {'planned', 'eating', 'eaten', 'skipped'}:
                    raise ValueError('DAILY_LIFE_MEALS_INVALID')
                food = '' if meal['status'] == 'skipped' and meal['food'] == '' else _text(meal['food'], 120)
                checked_meals.append({**meal, 'food': food,
                                      'date': occurred_at.astimezone(LOCAL).date().isoformat(),
                                      'occurred_at': stamp, 'source_id': source_id})
            if len({m['slot'] for m in checked_meals}) != len(checked_meals):
                raise ValueError('DAILY_LIFE_MEALS_INVALID')
        if len({p["id"] for p in checked}) != len(checked):
            raise ValueError("DAILY_LIFE_PROJECTS_INVALID")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM life_moments WHERE source_id=?", (source_id,)).fetchone():
                return False
            active_bath = db.execute('SELECT source_id,started_at FROM life_baths WHERE completed_source_id IS NULL '
                                     'AND started_at<=? ORDER BY started_at DESC LIMIT 1', (stamp,)).fetchone()
            if activity_kind == 'bath_started' and active_bath:
                raise ValueError('DAILY_LIFE_BATH_ALREADY_STARTED')
            if project_timing:
                from .project_timing import validate as validate_project_timing
                timings = validate_project_timing(project_timing, {'projects': self._projects_at(db, occurred_at)})
                for timing in timings:
                    db.execute('INSERT OR IGNORE INTO life_project_timing VALUES (?,?)', (timing['version'], _json(timing)))
            previous_meals = self._world(db, occurred_at)['meals']
            for meal in checked_meals:
                old = next((m for m in previous_meals if m['date'] == meal['date'] and m['slot'] == meal['slot']), None)
                if old and old['status'] == 'eating' and old['food'] != meal['food']:
                    raise ValueError('DAILY_LIFE_MEAL_REWRITE')
                if old and (old['status'] in {'eaten', 'skipped'} or (old['status'] == 'eating' and meal['status'] == 'planned')):
                    if (old['status'], old['food']) != (meal['status'], meal['food']):
                        raise ValueError('DAILY_LIFE_MEAL_REWRITE')
            # Terminal meals retain the original occurrence time/source. A
            # later life activity must not publish the same meal all over again.
            terminal = {(m['date'], m['slot']) for m in previous_meals if m['status'] in {'eaten', 'skipped'}}
            checked_meals = [m for m in checked_meals if (m['date'], m['slot']) not in terminal]
            published_projects = []
            for p in checked:
                existing = db.execute("SELECT payload FROM life_projects WHERE id=?", (p["id"],)).fetchone()
                old_project = json.loads(existing[0]) if existing else None
                if old_project and old_project.get("kind") == "shared":
                    raise ValueError("DAILY_LIFE_SHARED_EVENT_REQUIRES_LETTER")
                p.update(kind="linli", source_id=source_id, updated_at=stamp)
                if old_project and old_project["updated_at"] > stamp:
                    continue
                if old_project:
                    if old_project['status'] in {'completed', 'cancelled'}:
                        if p['status'] != old_project['status']:
                            raise ValueError('DAILY_LIFE_DECISION_PROJECT_REOPEN')
                        continue
                    p['title'] = old_project['title']
                db.execute("INSERT OR REPLACE INTO life_projects VALUES (?,?)", (p["id"], _json(p)))
                from .project_timing import inherit_world_progress
                inherit_world_progress(db, old_project, p)
                published_projects.append(p)
            current.update(source_id=source_id, occurred_at=stamp, progress=published_projects)
            if activity_kind is not None:
                current["activity_kind"] = activity_kind
            current['meals'] = checked_meals
            if weather:
                current['weather'] = weather
            db.execute("INSERT INTO life_moments VALUES (?,?,?,?)", (source_id, stamp, "daily", _json(current)))
            from .life_episode import save as save_episode
            save_episode(db, episode, source_id, occurred_at)
            if activity_kind == 'bath_started':
                db.execute('INSERT INTO life_baths VALUES (?,?,NULL,NULL)', (source_id, stamp))
            elif activity_kind == 'bath_finished' and active_bath:
                db.execute('UPDATE life_baths SET completed_source_id=?,completed_at=? WHERE source_id=?',
                           (source_id, stamp, active_bath['source_id']))
            if episode and episode.get('effects', {}).get('morning_wake'):
                current['event_kind'] = 'wake_up'
                db.execute('UPDATE life_moments SET payload=? WHERE source_id=?', (_json(current), source_id))
            from .character_development import record_world
            record_world(db, occurred_at, development, development_basis)
            self._set_current(db, current)
        return True

    @staticmethod
    def _set_current(db, current):
        if _explicitly_unfinished(current.get('note', '')):
            return  # Preserve the statement in the journal, not as a live activity.
        if not _current_time_consistent(current.get("note"), current.get("occurred_at")):
            return
        old = db.execute("SELECT payload FROM life_current WHERE id=1").fetchone()
        if not old or json.loads(old[0])["occurred_at"] <= current["occurred_at"]:
            db.execute("INSERT OR REPLACE INTO life_current VALUES (1,?)", (_json(current),))

    ADDRESSING_KINDS = ('user_calls_linli', 'user_self', 'linli_calls_user')

    def record_addressing(self, source_id: str, addressing: dict, *, occurred_at: datetime) -> None:
        """Keep exact addressing quotes from one delivered exchange (idempotent per exchange)."""
        _identifier(source_id)
        rows = [(source_id, kind, quote, _time(occurred_at)) for kind, quote in (addressing or {}).items()
                if kind in self.ADDRESSING_KINDS and isinstance(quote, str) and 0 < len(quote) <= 80]
        if not rows:
            return
        with self._db() as db:
            db.executemany('INSERT OR IGNORE INTO life_addressing VALUES (?,?,?,?)', rows)

    def addressing_profile(self, *, now: datetime, per_kind: int = 2) -> dict:
        """Most recent distinct quotes per kind; a later explicit change naturally wins."""
        profile = {}
        with self._db() as db:
            for kind in self.ADDRESSING_KINDS:
                seen = []
                for quote, stamp in db.execute(
                        'SELECT quote, occurred_at FROM life_addressing WHERE kind=? AND occurred_at<=? '
                        'ORDER BY occurred_at DESC, source_id DESC', (kind, _time(now))):
                    if quote not in (item['quote'] for item in seen):
                        seen.append({'quote': quote, 'occurred_at': stamp})
                    if len(seen) == per_kind:
                        break
                if seen:
                    profile[kind] = seen
        return profile

    def record_exchange(self, source_id: str, user_text: str, reply_text: str, updates: list, *, occurred_at: datetime, current_quote: str | None = None, relationship: dict | None = None, received_at: datetime | None = None, routine: dict | None = None, boundaries: list | None = None, origin: str = "user", contact_choice: dict | None = None, development: list | None = None) -> bool:
        """Consume only final letter text; exact quotations bind each update to its actor."""
        _identifier(source_id)
        if not source_id.startswith("reply:"):
            raise ValueError("DAILY_LIFE_SOURCE_INVALID")
        if not isinstance(origin, str) or origin not in {"user", "proactive"}:
            raise ValueError("DAILY_LIFE_ORIGIN_INVALID")
        if origin == "proactive" and user_text != "":
            raise ValueError("DAILY_LIFE_PROACTIVE_USER_TEXT_INVALID")
        stamp = _time(occurred_at)
        received = _time(received_at or occurred_at)
        if received > stamp:
            raise ValueError("DAILY_LIFE_TIME_INVALID")
        digest_value = [user_text, reply_text] if origin == "user" else [origin, user_text, reply_text]
        digest = hashlib.sha256(_json(digest_value).encode("utf-8")).hexdigest()
        if origin == "proactive" and relationship is not None:
            raise ValueError("DAILY_LIFE_PROACTIVE_RELATIONSHIP_INVALID")
        if origin == "proactive" and routine is not None:
            raise ValueError("DAILY_LIFE_PROACTIVE_ROUTINE_INVALID")
        from runtime.personal_chat.contact_invitation import validate_choice
        contact_choice = validate_choice(contact_choice, user_text)
        if origin == "proactive" and contact_choice is not None:
            raise ValueError("DAILY_LIFE_CONTACT_CHOICE_INVALID")
        relationship = validate_exchange_relationship(relationship, user_text, reply_text)
        boundaries = validate_boundary_changes(boundaries, reply_text)
        if routine is not None:
            if not isinstance(routine, dict) or set(routine) != {'sleep_minute', 'utc_offset_minutes', 'quote'}:
                raise ValueError('DAILY_LIFE_ROUTINE_INVALID')
            if _text(routine['quote'], 240) not in user_text:
                raise ValueError('DAILY_LIFE_EVIDENCE_INVALID')
            minute, offset = routine['sleep_minute'], routine['utc_offset_minutes']
            if not (minute is None and offset is None):
                if type(minute) is not int or not 0 <= minute < 1440 or type(offset) is not int or not -720 <= offset <= 840:
                    raise ValueError('DAILY_LIFE_ROUTINE_INVALID')
        current = None
        if current_quote is not None:
            quote = _current_source_quote(current_quote, reply_text)
            current = {"location": None, "activity": None, "note": quote,
                       "source_id": source_id, "occurred_at": stamp}
        checked = validate_exchange_updates(source_id, user_text, reply_text, updates, stamp=stamp, origin=origin)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT payload FROM life_moments WHERE source_id=?", (source_id,)).fetchone()
            if old:
                if json.loads(old[0]).get("digest") != digest:
                    raise ValueError("DAILY_LIFE_SOURCE_CONFLICT")
                return False
            from .character_development import record_exchange as record_development
            record_development(db, source_id, digest, user_text, reply_text, development, relationship, stamp, origin, checked, received)
            for item in checked:
                old = db.execute("SELECT payload FROM life_projects WHERE id=?", (item["id"],)).fetchone()
                if old:
                    existing = json.loads(old[0])
                    if existing["kind"] != item["kind"]:
                        raise ValueError("DAILY_LIFE_PROJECT_KIND_CONFLICT")
                    if existing["updated_at"] > stamp:
                        continue  # A delayed delivery cannot roll current life backwards.
                db.execute("INSERT OR REPLACE INTO life_projects VALUES (?,?)", (item["id"], _json(item)))
            db.execute("INSERT INTO life_moments VALUES (?,?,?,?)", (source_id, stamp, "exchange", _json({"updates": checked, "digest": digest, "current": current, "relationship": relationship, "contact_choice": contact_choice, "boundaries": boundaries, "origin": origin})))
            if origin == "user":
                db.execute("INSERT INTO life_rest_exchanges VALUES (?,?,?)", (source_id, received, stamp))
            if routine is not None and origin == "user":
                db.execute('INSERT INTO life_user_routine VALUES (?,?,?)', (source_id, stamp, _json(routine)))
            # Dialogue records what was said. Only publish_day advances the
            # authored world; self-reporting cannot certify its own truth.
        return True

    def record_media_delivery(self, event: dict) -> bool:
        event = validate_delivery(event)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if event['component'] == 'daily_life':
                reservation = db.execute('SELECT * FROM life_media_reservations WHERE source_id=?',
                                         (event['daily_event']['event_id'],)).fetchone()
                if reservation is None:
                    raise ValueError('DAILY_VIDEO_RESERVATION_REQUIRED')
                if reservation['request_sha256'] != event['reply_sha256'] or reservation['task_id'] != event['task_id']:
                    raise ValueError('DAILY_VIDEO_EVENT_CONFLICT')
                verified = datetime.fromisoformat(reservation['verified_at'])
                if not datetime.fromisoformat(event['daily_event']['event_at']) <= verified <= datetime.fromisoformat(event['occurred_at']):
                    raise ValueError('DAILY_VIDEO_SOURCE_UNAVAILABLE')
                previous = db.execute("SELECT payload FROM life_moments WHERE source_id=?", (event['event_id'],)).fetchone()
                if previous and json.loads(previous[0])['delivery']['reply_sha256'] != event['reply_sha256']:
                    raise ValueError('DAILY_VIDEO_EVENT_CONFLICT')
            cursor = db.execute('INSERT OR IGNORE INTO life_moments VALUES (?,?,?,?)',
                (event['event_id'], _time(datetime.fromisoformat(event['occurred_at'])), 'media', _json({'delivery': event})))
            return cursor.rowcount == 1

    def daily_video_event_matches(self, request: dict, *, now: datetime | None = None) -> bool:
        from runtime.personal_chat.daily_video import validate_input
        request = validate_input(request)
        now = now or datetime.now(timezone.utc)
        with self._db() as db:
            return self._daily_video_event_matches(db, request, now)

    def daily_video_can_prepare(self, request: dict, *, now: datetime | None = None) -> bool:
        """A real start may prepare a clip; only the actual finish may send it."""
        from runtime.personal_chat.daily_video import validate_input
        request = validate_input(request)
        now = now or datetime.now(timezone.utc)
        with self._db() as db:
            if self._daily_video_event_matches(db, request, now):
                return True
            if request['event_kind'] != 'bath_finished':
                return False
            if request.get('place'):
                source = db.execute("SELECT payload FROM life_moments WHERE source_id=? AND kind='daily'",
                                    (request['event_id'],)).fetchone()
                if not source or json.loads(source[0]).get('place') != request['place']:
                    return False
            bath = db.execute('SELECT started_at FROM life_baths WHERE source_id=?', (request['event_id'],)).fetchone()
            return bool(bath and datetime.fromisoformat(bath[0]) == datetime.fromisoformat(request['event_at'])
                        and datetime.fromisoformat(bath[0]) <= now)

    def daily_video_sources(self, *, now: datetime, limit: int = 6) -> list[dict]:
        """Small author candidates from actual sources; a plan is never a source."""
        from runtime.personal_chat.daily_video import EVENT_KINDS
        start = now.astimezone(LOCAL).replace(hour=0, minute=0, second=0, microsecond=0)
        sources = []
        with self._db() as db:
            rows = db.execute("SELECT source_id,occurred_at,payload FROM life_moments "
                "WHERE kind='daily' AND occurred_at>=? AND occurred_at<=? ORDER BY occurred_at DESC LIMIT 30",
                (_time(start), _time(now))).fetchall()
            for source_id, occurred_at, raw in rows:
                item = json.loads(raw)
                if item.get('place', {}).get('stage', 'arrived') != 'arrived':
                    continue
                kind = item.get('event_kind') or item.get('activity_kind')
                if kind == 'bath_started':
                    bath = db.execute('SELECT completed_source_id FROM life_baths WHERE source_id=?', (source_id,)).fetchone()
                    if not bath or bath[0] is not None:
                        continue
                    sources.append(dict(event_id=source_id, event_at=occurred_at, event_kind='bath_finished',
                        event_status='preparing', location=item.get('location'),
                        **({'place': item['place']} if item.get('place') else {}),
                        detail='已实际开始这次洗澡，视频可提前准备；尚未完成，不可发送。'))
                    if len(sources) >= limit:
                        break
                    continue
                if kind == 'bath_finished' and db.execute(
                        'SELECT 1 FROM life_baths WHERE completed_source_id=?', (source_id,)).fetchone():
                    continue  # The prepared clip retains the original start identity.
                if kind not in EVENT_KINDS or db.execute(
                        'SELECT 1 FROM life_media_reservations WHERE source_id=?', (source_id,)).fetchone():
                    continue
                if kind == 'shopping':
                    episode_row = db.execute('SELECT payload FROM life_episodes WHERE source_id=?', (source_id,)).fetchone()
                    if not episode_row or json.loads(episode_row[0]).get('result', {}).get('status') != 'completed':
                        continue
                if kind == 'meal' and not any(m.get('status') in {'eating', 'eaten'} for m in item.get('meals', [])):
                    meal_row = db.execute('SELECT payload FROM life_meal_events WHERE source_id=?', (source_id,)).fetchone()
                    meal = json.loads(meal_row[0]) if meal_row else {}
                    if (meal.get('status') not in {'eating', 'eaten'}
                            or meal.get('occurred_at') != occurred_at):
                        continue
                sources.append(dict(event_id=source_id, event_at=occurred_at, event_kind=kind,
                    location=item.get('location'), detail=item.get('note', '')[:180],
                    **({'place': item['place']} if item.get('place') else {})))
                if len(sources) >= limit:
                    break
        return sources

    def reserve_daily_video_delivery(self, request: dict, *, task_id: str,
                                    now: datetime | None = None) -> bool:
        """Retain a checked source binding before QQ; this is not a share fact."""
        from runtime.personal_chat.daily_video import validate_input
        request = validate_input(request)
        now = now or datetime.now(timezone.utc)
        stamp = _time(now)
        if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', task_id):
            raise ValueError('DAILY_VIDEO_TASK_INVALID')
        digest = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            # Always recheck the author, even if a previous send was deferred.
            if not self._daily_video_event_matches(db, request, now):
                raise ValueError('DAILY_VIDEO_SOURCE_UNAVAILABLE')
            old = db.execute('SELECT request_sha256,task_id FROM life_media_reservations WHERE source_id=?',
                             (request['event_id'],)).fetchone()
            if old:
                if old['request_sha256'] != digest or old['task_id'] != task_id:
                    raise ValueError('DAILY_VIDEO_EVENT_CONFLICT')
                return False
            bath = db.execute('SELECT completed_source_id FROM life_baths WHERE source_id=?',
                              (request['event_id'],)).fetchone() if request['event_kind'] == 'bath_finished' else None
            source_id = bath[0] if bath else request['event_id']
            source = db.execute("SELECT payload FROM life_moments WHERE source_id=? AND kind='daily'",
                                (source_id,)).fetchone()
            if source is None:
                source = db.execute('SELECT payload FROM life_meal_events WHERE source_id=?',
                                    (request['event_id'],)).fetchone()
            source_digest = hashlib.sha256(source[0].encode()).hexdigest()
            db.execute('INSERT INTO life_media_reservations VALUES (?,?,?,?,?)',
                       (request['event_id'], digest, task_id, stamp, source_digest))
            return True

    def record_daily_video_delivery(self, request: dict, *, task_id: str, message_id: str,
                                    delivered_at: datetime) -> bool:
        from runtime.reply.media_delivery import make_daily_delivery
        return self.record_media_delivery(make_daily_delivery(request, task_id=task_id,
            message_id=message_id, occurred_at=delivered_at))

    @staticmethod
    def _daily_video_event_matches(db, request, now):
        when = datetime.fromisoformat(request['event_at'])
        if when > now:
            return False
        if request.get('place'):
            source = db.execute("SELECT payload FROM life_moments WHERE source_id=? AND kind='daily'",
                                (request['event_id'],)).fetchone()
            if not source or json.loads(source[0]).get('place') != request['place']:
                return False
        if request['event_kind'] == 'bath_finished':
            bath = db.execute('SELECT started_at,completed_source_id,completed_at FROM life_baths WHERE source_id=?',
                              (request['event_id'],)).fetchone()
            if bath:
                if (datetime.fromisoformat(bath['started_at']) != when or not bath['completed_source_id']
                        or datetime.fromisoformat(bath['completed_at']) > now):
                    return False
                ended = db.execute('SELECT payload FROM life_episodes WHERE source_id=?',
                                   (bath['completed_source_id'],)).fetchone()
                episode = json.loads(ended[0]) if ended else {}
                return (episode.get('activity_kind') == 'bath_finished'
                        and episode.get('result', {}).get('status') == 'completed')
        row = db.execute("SELECT occurred_at,payload FROM life_moments WHERE source_id=? AND kind='daily'",
                         (request['event_id'],)).fetchone()
        if row:
            payload = json.loads(row[1])
            kind = payload.get('event_kind') or payload.get('activity_kind')
            matches_kind = kind == request['event_kind']
            if (datetime.fromisoformat(row[0]) == when
                    and matches_kind):
                if request['event_kind'] == 'shopping':
                    episode_row = db.execute('SELECT payload FROM life_episodes WHERE source_id=?', (request['event_id'],)).fetchone()
                    episode = json.loads(episode_row[0]) if episode_row else {}
                    return (kind == 'shopping' and episode.get('activity_kind') == 'shopping'
                            and episode.get('result', {}).get('status') == 'completed')
                if request['event_kind'] != 'meal' or any(
                        meal.get('status') in {'eating', 'eaten'} for meal in payload.get('meals', [])):
                    return True
        if request['event_kind'] == 'meal':
            row = db.execute('SELECT payload FROM life_meal_events WHERE source_id=?', (request['event_id'],)).fetchone()
            if row:
                meal = json.loads(row[0])
                return (meal.get('status') in {'eating', 'eaten'}
                        and datetime.fromisoformat(meal['occurred_at']) == when)
        # Clock-based sleep/bath schedules never certify wake/bath completion.
        return False

    def record_image_observation(self, event: dict) -> bool:
        from runtime.image_understanding import validate_observation
        validate_observation(event)
        identity = str(event['parent_id']) + ':' + event['source'] + ':' + event['sha256']
        if event.get('event_id') != 'image:' + hashlib.sha256(identity.encode()).hexdigest():
            raise ValueError('IMAGE_OBSERVATION_ID_INVALID')
        with self._db() as db:
            cursor = db.execute('INSERT OR IGNORE INTO life_moments VALUES (?,?,?,?)',
                (event['event_id'], _time(datetime.fromisoformat(event['observed_at'])), 'image', _json({'image': event})))
            return cursor.rowcount == 1

    def exchange_relationship(self, source_id: str, user_text: str, reply_text: str, *, origin: str = "user") -> dict | None:
        payload = self._exchange_payload(source_id, user_text, reply_text, origin=origin)
        return validate_exchange_relationship(payload.get("relationship"), user_text, reply_text)

    def exchange_boundaries(self, source_id: str, user_text: str, reply_text: str, *, origin: str = "user") -> list[dict]:
        payload = self._exchange_payload(source_id, user_text, reply_text, origin=origin)
        return validate_boundary_changes(payload.get("boundaries"), reply_text)

    def _exchange_payload(self, source_id: str, user_text: str, reply_text: str, *, origin: str = "user") -> dict:
        with self._db() as db:
            row = db.execute("SELECT payload FROM life_moments WHERE source_id=? AND kind='exchange'", (source_id,)).fetchone()
        if row is None:
            return {}
        payload = json.loads(row[0])
        digest_value = [user_text, reply_text] if origin == "user" else [origin, user_text, reply_text]
        digest = hashlib.sha256(_json(digest_value).encode("utf-8")).hexdigest()
        if payload.get("digest") != digest:
            raise ValueError("DAILY_LIFE_SOURCE_CONFLICT")
        return payload

    def _projects_at(self, db, now: datetime) -> list[dict]:
        """Project the event journal at the requested time, including old data."""
        return self._project_changes_at(db, now, per_project=1)

    def _project_changes_at(self, db, now: datetime, *, per_project: int) -> list[dict]:
        """Last changes per identity, oldest first; ties follow journal insertion."""
        rows = db.execute("""
            SELECT item, position FROM (
                SELECT j.value AS item, ROW_NUMBER() OVER (
                    PARTITION BY json_extract(j.value, '$.id')
                    ORDER BY m.occurred_at DESC, m.rowid DESC) AS position
                FROM life_moments m, json_each(CASE m.kind
                    WHEN 'daily' THEN json_extract(m.payload, '$.progress')
                    WHEN 'phase_seed' THEN json_extract(m.payload, '$.updates')
                    WHEN 'exchange' THEN json_extract(m.payload, '$.updates') END) j
                WHERE m.occurred_at<=? AND m.kind IN ('daily','exchange','phase_seed')
            ) WHERE position<=? ORDER BY json_extract(item, '$.id'), position DESC
        """, (_time(now), per_project))
        from .phase_settings import project_at
        from .project_timing import project_at as project_time_at
        return [project_time_at(db, project_at(json.loads(row[0]), now), now) if row[1] == 1 else json.loads(row[0]) for row in rows]

    def exchange_state(self, query: str = "", *, related_text: str = "", now: datetime | None = None, include_history: bool = False) -> dict:
        """Open items and items this exchange mentions, with full evidence; finished
        items only as identities, and only while recent. Every exchange sent all items
        ever recorded (109 for one user) and each update slot listed them all again,
        so the request grew with every item the user ever had until it could not be sent."""
        value = {"projects": [], "shared": []}
        tokens = _query_tokens(query) | _query_tokens(related_text)
        now = now or datetime.now(timezone.utc)
        with self._db() as db:
            db.execute("BEGIN")
            changes = {}
            for item in self._project_changes_at(db, now, per_project=3 if include_history else 1):
                changes.setdefault(item['id'], []).append(item)
            for history in changes.values():
                item = history[-1]
                mentioned = bool(tokens & _query_tokens(item["title"] + " " + item.get("quote", item["detail"])))
                if item["status"] not in {"completed", "cancelled"} or mentioned:
                    disclosed = _project_evidence(item)
                elif not _recently_finished(item, now):
                    continue
                else:
                    # Keep every stable identity, even outside
                    # the UI window. Omitted evidence is never a blank quote.
                    fields = (("id", "title", "status") if include_history else
                              ("id", "title", "kind", "actor", "status", "source_id", "updated_at"))
                    disclosed = {key: item.get(key) for key in fields}
                    if include_history:
                        # This is an identity index, not an evidence excerpt.
                        # Full originals and timestamps remain in the journal.
                        disclosed['evidence_kind'] = _project_evidence(item)['evidence_kind']
                if include_history and item['kind'] == 'linli' and item['status'] not in {'completed', 'cancelled'}:
                    disclosed['history'] = [_project_evidence(change) for change in history if change['kind'] == 'linli']
                value["projects" if item["kind"] == "linli" else "shared"].append(disclosed)
        return value

    def reply_candidates(self, *, now: datetime) -> dict:
        """Authorized as-of facts, before any semantic ranking or reply budget."""
        with self._db() as db:
            db.execute('BEGIN')
            snapshot = self._snapshot(db, now)
            from .character_development import view as development_view
            development = development_view(db, now)
            projects = [_project_evidence(p) for p in _live_threads(self._projects_at(db, now), now)]
            observations = self._read_observations(db, now)
            last_reply = db.execute('SELECT MAX(replied_at) FROM life_rest_exchanges WHERE replied_at<=?', (_time(now),)).fetchone()[0]
            deliveries = [json.loads(r[0])['delivery'] for r in db.execute(
                "SELECT payload FROM life_moments WHERE kind='media' AND occurred_at<=? ORDER BY occurred_at DESC LIMIT 3", (_time(now),))]
            images = [json.loads(r[0])['image'] for r in db.execute(
                "SELECT payload FROM life_moments WHERE kind='image' AND occurred_at<=? ORDER BY occurred_at DESC LIMIT 3", (_time(now),))]
        world = snapshot['world']
        schedule = {k: world['schedule'].get(k) for k in ('date', 'phase', 'classes', 'current_class', 'next_class')}
        schedule.update(timezone='Asia/Shanghai', meaning='课表是计划，不证明出席。current_class为空不代表今天没课，今天课程看classes，下一节看next_class。')
        current = snapshot['current']
        stale = snapshot['stale'] or bool(current and last_reply and last_reply > current['occurred_at'])
        base = {'kind': 'character_life_reference', 'as_of': _time(now), 'stale': stale,
                'current': None, 'threads': [],
                'meaning': '仅为有时间和来源的角色视角。未选择的信息不是不存在。课表是计划，角色说法不等于已完成事实，不能混淆用户与角色。last_observation不是此刻活动；meals中stale只表示状态待更新，不证明仍在吃、已吃完或未吃。她的等待不等于用户承诺，计划不等于已发生。'}
        records = [{'field': 'schedule', 'value': schedule}, {'field': 'weather', 'value': world['weather']},
                   {'field': 'character_development', 'value': development}]
        if current:
            current = {**current, 'actor': 'linli', 'evidence_kind': 'character_statement'
                       if current['source_id'].startswith('reply:') else 'published_life'}
            records.append({'field': 'last_observation' if stale else 'current', 'value': current})
        for field, values in (('threads', projects), ('meals', world['meals']), ('recent_episodes', world.get('recent_episodes', [])),
                              ('previous_observations', observations), ('media_deliveries', grouped_delivery_evidence(deliveries)),
                              ('image_observations', images)):
            records.extend({'field': field, 'value': value, 'many': True} for value in values)
        return {'base': base, 'records': records, 'rhythm': snapshot['rhythm']}

    def reply_context(self, query: str, *, now: datetime, max_chars: int = 1800, related_text: str = "") -> str:
        """Disclose a small current view, then only relevant persistent threads."""
        # All views in this reply use one SQLite read transaction. A concurrent
        # delivery cannot mix new project state with an older observation.
        with self._db() as db:
            db.execute("BEGIN")
            snapshot = self._snapshot(db, now)
            from .character_development import view as development_view
            development = development_view(db, now)
            observations = self._read_observations(db, now)
            all_projects = self._projects_at(db, now)
            last_reply = db.execute(
                "SELECT MAX(replied_at) FROM life_rest_exchanges WHERE replied_at<=?", (_time(now),)
            ).fetchone()[0]
            media = [json.loads(row[0])['delivery'] for row in db.execute(
                "SELECT payload FROM life_moments WHERE kind='media' AND occurred_at<=? ORDER BY occurred_at DESC, source_id DESC LIMIT 3", (_time(now),))]
            images = [json.loads(row[0])['image'] for row in db.execute(
                "SELECT payload FROM life_moments WHERE kind='image' AND occurred_at<=? ORDER BY occurred_at DESC, source_id DESC LIMIT 3", (_time(now),))]
        # Timetable and independently observed weather remain usable before the
        # first published activity, or when the life-generation model is down.
        tokens = _query_tokens(query)
        related_tokens = _query_tokens(related_text)
        topic_chars = _query_topic_chars(query)
        related_topic_chars = _query_topic_chars(related_text)
        def relevance(p):
            text_tokens = _query_tokens(p["title"] + " " + p.get("quote", p["detail"]))
            title_chars = _query_topic_chars(p['title'])
            # Current question first; earlier letters may introduce an old
            # plan, so disclose that topic's current state in the same budget.
            direct_words = len(tokens & text_tokens)
            # One-character subjects may be separated from their verb in a
            # question. Match only the durable title, not unrelated support.
            direct = direct_words or 0.25 * len(topic_chars & title_chars)
            if (not direct and p["kind"] == "shared"
                    and p.get("actor") == "linli" and p["status"] == "awaiting_user"):
                # Recalling her own invitation must not keep promoting it as an
                # outstanding user obligation. Its source remains in the exchange log;
                # current-topic recall and exchange extraction retain access.
                return 0
            related = len(related_tokens & text_tokens) or 0.25 * len(related_topic_chars & title_chars)
            priority = 1000 if direct_words else 100 if direct else 0
            return priority + (direct + related) / max(1, len(text_tokens) ** 0.5)
        # UI limits must not hide old cancellations or finished threads from recall.
        projects = sorted((p for p in all_projects if relevance(p) > 0), key=lambda p: (relevance(p), p["updated_at"]), reverse=True)
        relevant_shared = next((p for p in projects if p["kind"] == "shared" and relevance(p) > 0), None)
        if relevant_shared:
            projects = [relevant_shared] + [p for p in projects if p["id"] != relevant_shared["id"]]
        current = snapshot["current"]
        # A moment records an observation, not a six-hour live activity. Later
        # correspondence makes it inactive until another observation arrives.
        # Keep the refresh TTL and stored journal unchanged.
        historical = bool(current and last_reply and last_reply > current["occurred_at"])
        value = {
            "kind": "character_life_reference",
            "meaning": "同一事件日志的时间截面。current仅来自已发布角色生活；last_observation不是此刻活动。meals中stale表示当前用餐状态待更新，保留的是当时记录，不能据此说仍在吃、已经吃完或没吃。character_statement只证明林离说过，user_statement只证明用户陈述，不能互换人物或自行升级为已发生。事项status是带来源的记录；取消须保留，约定不等于完成。不同来源矛盾时保持未定，不选最新说法当真，不编造过渡。官方人设和关系权限仍由各自来源约束。",
            "stale": snapshot["stale"] or historical,
            "current": {k: current[k] for k in ("location", "activity", "note", "occurred_at", "source_id", "place") if k in current} if current else None,
            "threads": [],
        }
        if value['current']:
            value['current'].update(actor='linli', evidence_kind=(
                'character_statement' if value['current']['source_id'].startswith('reply:') else 'published_life'))
        if value["stale"] and value["current"]:
            value["last_observation"] = value["current"]
            value["current"] = None
        if development['items']:
            # Keep relevant mature changes even when the whole catalog no
            # longer fits. Pack after the complete current/stale projection,
            # so required attribution cannot overflow the finished world view.
            # Each retained item is intact and shares this as_of.
            selected = {**development, 'items': [], 'omitted_count': len(development['items'])}
            ordered = sorted(development['items'], key=lambda item: (
                bool(tokens & _query_tokens(item['label'] + ' ' + item['key'])),
                item['stage'] == 'growing'), reverse=True)
            for item in ordered:
                proposed = {**selected, 'items': [*selected['items'], item],
                            'omitted_count': selected['omitted_count'] - 1}
                candidate = {**value, 'character_development': proposed}
                if len(_json(candidate)) <= max_chars:
                    selected, value = proposed, candidate
        world = snapshot['world']
        schedule = world['schedule']
        # Timetable facts must survive before optional narrative episodes.
        # No class at this instant does not mean there are no classes today.
        timetable = {k: schedule.get(k) for k in ('date', 'phase', 'classes', 'current_class', 'next_class')}
        timetable['timezone'] = 'Asia/Shanghai'
        timetable['meaning'] = '课表是当天计划，不是出席证据。current_class为空只表示此刻未在课程时段，不代表今天没课；今天是否有课看classes，下一节看next_class。休息不代表课程取消，不得据此指责用户记错日子。'
        candidate = {**value, 'schedule': timetable}
        if len(_json(candidate)) <= max_chars:
            value = candidate
        for episode in world.get('recent_episodes', []):
            candidate = {**value, 'recent_episodes': [*value.get('recent_episodes', []), episode]}
            if len(_json(candidate)) <= max_chars:
                value = candidate
            else:
                break  # Preserve whole process/meaning boundaries, never trim into a false cause.
        for name, item in (
            ('weather', world['weather']),
            ('meals', [m for m in world['meals'] if m['date'] == now.astimezone(LOCAL).date().isoformat()][:3]),
        ):
            candidate = {**value, name: item}
            if len(_json(candidate)) <= max_chars:
                value = candidate
        for observation in images:
            # Images describe an artifact, never certify a new location/activity.
            candidate = {**value, 'image_observations': [*value.get('image_observations', []), observation]}
            if len(_json(candidate)) <= max_chars:
                value = candidate
        for project in projects[:2]:
            disclosed = _project_evidence(project)
            if project.get("actor") == "linli" and project["status"] == "awaiting_user":
                # Her request or expectation is not evidence that the user
                # undertook an action; keep waiting attributed to its speaker.
                disclosed = {**disclosed, "status": "linli_waiting",
                             "commitment_evidence": "requires_user_statement",
                             "meaning": "她的等待不等于用户承诺；用户是否答应，以用户原文为准。"}
            candidate = {**value, "threads": [*value["threads"], disclosed]}
            if len(_json(candidate)) <= max_chars:
                value = candidate
        # Preserve earlier statements without displacing current cancellations
        # and project states. Different wording is not proof of a transition.
        for observation in observations:
            if current and observation['source_id'] == current['source_id']:
                continue
            candidate = {**value, 'previous_observations': [*value.get('previous_observations', []), observation],
                         'observation_boundary': '这些是带来源的旧说法，不是并行的当前活动。不同说法并列保留，不擅自选真、合并成先后事件或编造过渡。计划不等于已发生。'}
            if len(_json(candidate)) <= max_chars:
                value = candidate
        for group in grouped_delivery_evidence(media):
            candidate = {**value, 'media_deliveries': [*value.get('media_deliveries', []), group],
                'media_meaning': MEDIA_EVIDENCE_MEANING}
            if len(_json(candidate)) <= max_chars:
                value = candidate
        result = _json(value)
        return result if len(result) <= max_chars else ""

    def history(self, *, before: str | None = None) -> dict:
        """Eight immutable moments per page; new arrivals do not shift older pages."""
        params = ()
        condition = ""
        if before is not None:
            try:
                if not isinstance(before, str) or len(before) > 600:
                    raise ValueError
                stamp, source = json.loads(base64.urlsafe_b64decode(before).decode("utf-8"))
                stamp = _time(datetime.fromisoformat(stamp))
                source = _identifier(source)
            except (ValueError, TypeError, UnicodeError) as exc:
                raise ValueError("DAILY_LIFE_CURSOR_INVALID") from exc
            condition = " AND (occurred_at, source_id) < (?, ?)"
            params = (stamp, source)
        with self._db() as db:
            rows = db.execute(f"SELECT source_id, occurred_at, kind, payload FROM life_moments WHERE {_VISIBLE}{condition} ORDER BY occurred_at DESC, source_id DESC LIMIT 9", params).fetchall()
        cursor = None
        if len(rows) > 8:
            last = rows[7]
            cursor = base64.urlsafe_b64encode(_json([last["occurred_at"], last["source_id"]]).encode()).decode()
        return {"schema_version": "olivia.daily-life.history.v1", "status": "READY", "moments": self._moments(rows[:8]), "next_cursor": cursor}

    @staticmethod
    def _moments(rows) -> list:
        moments = []
        for row in rows:
            content = {k: v for k, v in json.loads(row["payload"]).items() if k not in {"digest", "relationship", "boundaries"}}
            if row["kind"] == "exchange" and isinstance(content.get("current"), dict):
                content["current"] = _current_evidence(content["current"])
            moments.append({"id": row["source_id"], "occurred_at": row["occurred_at"],
                            "kind": row["kind"], "content": content})
        return moments

    def recent_life(self, now: datetime) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                "SELECT source_id,occurred_at,payload FROM (SELECT source_id,occurred_at,payload,"
                "ROW_NUMBER() OVER (PARTITION BY date(occurred_at,'+8 hours') ORDER BY occurred_at DESC,source_id DESC) AS n "
                "FROM life_moments WHERE kind='daily' AND occurred_at<=? AND occurred_at>=?) "
                "WHERE n<=2 ORDER BY occurred_at DESC,source_id DESC LIMIT 14",
                (_time(now), _time(now-timedelta(days=7)))).fetchall()
        return [{'source_id': r[0], 'occurred_at': r[1], 'activity': json.loads(r[2])['activity'],
                 'note': json.loads(r[2])['note'], 'evidence_kind': 'published_life'} for r in rows]

    def _world(self, db, now):
        meals, weather = {}, None
        from .meal_lifecycle import records as meal_records, schedule as meal_schedule
        for meal in meal_records(db, now):
            meals.setdefault((meal['date'], meal['slot']), meal)
        observation = db.execute('SELECT payload FROM life_weather WHERE fetched_at<=? ORDER BY fetched_at DESC LIMIT 1', (_time(now),)).fetchone()
        if observation:
            weather = json.loads(observation[0])
        rows = db.execute("SELECT payload FROM life_moments WHERE kind='daily' AND occurred_at<=? AND occurred_at>=? "
                          "ORDER BY occurred_at DESC,source_id DESC LIMIT 80", (_time(now), _time(now-timedelta(days=7))))
        for row in rows:
            payload = json.loads(row[0])
            if weather is None and payload.get('weather'):
                weather = payload['weather']
            for meal in payload.get('meals', []):
                key = (meal['date'], meal['slot'])
                old = meals.get(key)
                if old is None or datetime.fromisoformat(meal.get('recorded_at') or meal['occurred_at']) > datetime.fromisoformat(old.get('recorded_at') or old['occurred_at']):
                    meals[key] = meal
        projected_meals = [_meal_observation(meal, now) for meal in list(meals.values())[:28]]
        day_start = now.astimezone(LOCAL).replace(hour=0, minute=0, second=0, microsecond=0)
        today_activities = []
        prior_daily = None
        for row in db.execute(
                "SELECT source_id,occurred_at,payload FROM life_moments WHERE kind='daily' "
                "AND occurred_at>=? AND occurred_at<=? ORDER BY occurred_at ASC,source_id ASC LIMIT 128",
                (_time(day_start), _time(now))):
            payload = json.loads(row[2])
            if payload.get('activity_kind') == 'meal' or (not payload.get('activity_kind') and payload.get('meals')):
                prior_daily = None
                continue
            item = {'source_id': row[0], 'occurred_at': row[1],
                'activity': payload.get('activity'), 'activity_kind': payload.get('activity_kind'),
                'note': payload.get('note')}
            if (prior_daily is not None and item['activity_kind'] == 'rest'
                    and all(prior_daily.get(key) == item.get(key) for key in ('activity_kind', 'activity', 'note'))
                    and datetime.fromisoformat(item['occurred_at']) - datetime.fromisoformat(prior_daily.get('last_recorded_at', prior_daily['occurred_at'])) <= timedelta(minutes=45)):
                prior_daily.setdefault('source_ids', [prior_daily['source_id']]).append(item['source_id'])
                prior_daily['last_recorded_at'] = item['occurred_at']
                prior_daily['record_count'] = len(prior_daily['source_ids'])
            else:
                today_activities.append(item)
                prior_daily = item
        from .life_episode import recent as recent_episodes
        return {'schedule': student_schedule(now), 'weather': weather_view(weather, now),
                'meals': projected_meals, 'meal_schedule': meal_schedule(db, now, projected_meals),
                'recent_episodes': recent_episodes(db, now), 'today_activities': today_activities}

    def _recent_observations(self, now: datetime) -> list[dict]:
        with self._db() as db:
            return self._read_observations(db, now)

    def _read_observations(self, db, now: datetime) -> list[dict]:
        rows = db.execute("SELECT kind,payload FROM life_moments WHERE occurred_at<=? "
            "AND occurred_at>=? AND (kind='daily' OR (kind='exchange' AND json_type(payload,'$.current')='object')) "
            "ORDER BY occurred_at DESC,source_id DESC LIMIT 4", (_time(now), _time(now - RECENT_OBSERVATION_WINDOW))).fetchall()
        result = []
        for kind, raw in rows:
            payload = json.loads(raw)
            observation = _current_evidence(payload if kind == 'daily' else payload.get('current'))
            if observation and _current_time_consistent(observation.get('note'), observation.get('occurred_at')):
                result.append({**{key: observation[key] for key in ('note', 'source_id', 'occurred_at')},
                               'actor': 'linli', 'evidence_kind': 'character_statement' if kind == 'exchange' else 'published_life',
                               'completion': 'not_established' if _explicitly_unfinished(observation['note']) else 'not_verified'})
        return result

    def pending_exchange_actions(self, now: datetime, after: datetime | None = None) -> list[dict]:
        """Jev-extracted intentions invite a new decision, never certify an event."""
        since = max(now - timedelta(hours=6), after) if after else now - timedelta(hours=6)
        with self._db() as db:
            if after is None:
                latest = db.execute("SELECT MAX(occurred_at) FROM life_moments WHERE kind='daily' AND occurred_at<=?", (_time(now),)).fetchone()[0]
                if latest:
                    since = max(since, datetime.fromisoformat(latest))
            rows = db.execute("SELECT source_id,occurred_at,payload FROM life_moments "
                "WHERE kind='exchange' AND occurred_at>? AND occurred_at<=? "
                "AND source_id IN (SELECT source_id FROM life_exchange_world_gate WHERE decision='reconsider') "
                "ORDER BY occurred_at DESC,source_id DESC LIMIT 4", (_time(since), _time(now))).fetchall()
            texts = dict(db.execute("SELECT source_id,reply_text FROM life_exchange_world_gate WHERE source_id IN "
                "(SELECT source_id FROM life_moments WHERE occurred_at>? AND occurred_at<=?)", (_time(since), _time(now))))
            said = dict(db.execute("SELECT source_id,user_text FROM life_exchange_user_text WHERE source_id IN "
                "(SELECT source_id FROM life_moments WHERE occurred_at>? AND occurred_at<=?)", (_time(since), _time(now))))
        actions = []
        for source, stamp, raw in rows:
            payload = json.loads(raw)
            actions.append({'source_id': source, 'occurred_at': stamp,
                'evidence_kind': 'character_statement', 'current': payload.get('current'),
                'reply_text': texts[source], **({'user_text': said[source]} if said.get(source) else {}),
                'updates': [u for u in payload.get('updates', []) if u.get('kind') == 'linli'
                            and u.get('status') in {'planned', 'ongoing', 'paused'}]})
        return actions

    def snapshot(self, now: datetime) -> dict:
        with self._db() as db:
            db.execute("BEGIN")
            return self._snapshot(db, now)

    def _snapshot(self, db, now: datetime) -> dict:
        _time(now)
        current_row = db.execute("SELECT payload FROM life_moments WHERE kind='daily' AND occurred_at<=? ORDER BY occurred_at DESC, source_id DESC LIMIT 1", (_time(now),)).fetchone()
        rows = db.execute(f"SELECT source_id, occurred_at, kind, payload FROM life_moments WHERE {_VISIBLE} AND occurred_at<=? ORDER BY occurred_at DESC, source_id DESC LIMIT 12", (_time(now),)).fetchall()
        projects = self._projects_at(db, now)
        exchanges = [(datetime.fromisoformat(r[0]), datetime.fromisoformat(r[1])) for r in db.execute(
            "SELECT received_at, replied_at FROM life_rest_exchanges WHERE replied_at>=? AND received_at<=? ORDER BY received_at",
            (_time(now - timedelta(days=14)), _time(now)))]
        shifts = dict(db.execute('SELECT day, shift_minutes FROM life_routine_days'))
        current = _current_evidence(json.loads(current_row[0]) if current_row else None)
        if current and _explicitly_unfinished(current['note']):
            current = None
        projects.sort(key=lambda p: (p["status"] in {"completed", "cancelled"} or bool(p.get("deadline_expired")) or p.get('time_scope') == 'transient', -datetime.fromisoformat(p["updated_at"]).timestamp(), p["id"]))
        # An activity observed before a class boundary must not remain "current"
        # for another 3–5 hours. A timetable still does not prove attendance.
        class_changed = False
        if current:
            observed = datetime.fromisoformat(current['occurred_at'])
            class_changed = any(observed < datetime.fromisoformat(item[edge]) <= now
                for point in (observed, now) for item in student_schedule(point)['classes']
                for edge in ('start', 'end'))
        # A long rest/practice observation must not suppress a new mealtime
        # decision. This expires the observation, never invents a meal record.
        meal_boundary = False
        if current:
            observed_local = datetime.fromisoformat(current['occurred_at']).astimezone(LOCAL)
            local_now = now.astimezone(LOCAL)
            meal_boundary = any(
                observed_local < local_now.replace(hour=hour, minute=0, second=0, microsecond=0) <= local_now
                for hour in (8, 12, 18)
            )
        world = self._world(db, now)
        meal_finished = bool(current and current['source_id'].startswith('meal:') and any(
            m['status'] == 'eaten' and m.get('started_at') == current['occurred_at'] for m in world['meals']))
        from .life_rhythm import with_recovery
        body = rhythm(now, exchanges, shifts)
        recovery_episodes = [json.loads(row[0]) for row in db.execute(
            """SELECT payload FROM life_episodes WHERE occurred_at>=? AND occurred_at<=?
               AND (json_type(payload,'$.effects.body_recovery')='object'
                    OR json_type(payload,'$.effects.sleep_plan')='object'
                    OR json_type(payload,'$.effects.sleep_resolution')='object')
               ORDER BY occurred_at DESC,source_id DESC LIMIT 128""",
            (_time(now - timedelta(days=14)), _time(now)))]
        body = with_recovery(body, recovery_episodes, now, exchanges=exchanges, shifts=shifts)
        bath = db.execute('SELECT source_id,started_at FROM life_baths WHERE started_at<=? '
                          'AND (completed_at IS NULL OR completed_at>?) ORDER BY started_at DESC LIMIT 1',
                          (_time(now), _time(now))).fetchone()
        if bath:
            body['authored_bath'] = dict(bath)
            body.update(phase='bathing', phase_basis='published_bath_start', activity='林离洗澡中', availability='rest')
        latest_activity = world['today_activities'][-1] if world['today_activities'] else None
        if latest_activity and latest_activity.get('activity_kind') == 'rest':
            body['rest_observations'] = {
                'first_observed_at': latest_activity['occurred_at'],
                'last_observed_at': latest_activity.get('last_recorded_at', latest_activity['occurred_at']),
                'record_count': latest_activity.get('record_count', 1),
                'meaning': '这些时刻已记录休息，不证明之间持续睡眠；本次恢复仍由新过程判断。'}
        return {
            "schema_version": "olivia.daily-life.v1", "status": "READY",
            "current": current,
            "world": world,
            "rhythm": body,
            "stale": (body.get('authored_sleep') or {}).get('status') == 'due' or current is None or class_changed or meal_boundary or meal_finished or now - datetime.fromisoformat(current["occurred_at"]) >= _activity_refresh_delay(
                current, repeats=_same_activity_repeats(db, current, now),
                idle=_user_idle(exchanges, now)),
            "projects": [p for p in projects if p["kind"] == "linli"][:6],
            "shared": [p for p in projects if p["kind"] == "shared"][:6],
            "moments": self._moments(rows),
        }
