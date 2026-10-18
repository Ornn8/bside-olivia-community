"""Explicit daily-video jobs; generation never owns the conversation lock.

The queue contains transport work, not world events or conversation memory.
Only an acknowledged QQ send enters the existing canonical media journal.
"""
import asyncio
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from aiohttp import web

from runtime.ui_i18n_backend import tr

LOCAL = timezone(timedelta(hours=8))
EVENT_KINDS = {'housework', 'walk', 'bath_finished', 'shopping', 'meal', 'wake_up', 'moment'}
PATH = '/toy/companion/daily-video'
RECOVER_PATH = PATH + '/recover'
_FIELDS = {'version', 'event_id', 'target_date', 'event_at', 'event_kind',
           'scene_id', 'certainty', 'spoken_text'}


def validate_input(value):
    try:
        if (not isinstance(value, dict) or not _FIELDS <= set(value) <= _FIELDS | {'staging', 'share_text', 'place'}
                or type(value['version']) is not int or value['version'] != 1
                or not isinstance(value['event_id'], str)
                or not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', value['event_id'])
                or value['event_kind'] not in EVENT_KINDS
                or value['certainty'] not in {'planned', 'live'}
                or not isinstance(value['scene_id'], str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value['scene_id'])
                or not isinstance(value['spoken_text'], str)
                or not 1 <= len(value['spoken_text']) <= 500 or not value['spoken_text'].strip()
                or not isinstance(value['target_date'], str)
                or date.fromisoformat(value['target_date']).isoformat() != value['target_date']
                or not isinstance(value['event_at'], str)):
            raise ValueError()
        when = datetime.fromisoformat(value['event_at'])
        if when.utcoffset() is None or when.astimezone(LOCAL).date().isoformat() != value['target_date']:
            raise ValueError()
        if 'place' in value:
            from runtime.private_world.place_detail import validate
            if validate(value['place'])['stage'] != 'arrived':
                raise ValueError()
        if value['scene_id'] == 'daily_place' and not value.get('place'):
            raise ValueError()
        if 'staging' in value:
            staging = value['staging']
            if (not isinstance(staging, dict) or set(staging) != {'image_direction', 'video_direction'}
                    or any(not isinstance(staging[key], str) or not 1 <= len(staging[key]) <= limit
                           or not staging[key].strip() or any(ord(c) < 32 for c in staging[key])
                           for key, limit in (('image_direction', 400), ('video_direction', 800)))):
                raise ValueError()
        if 'share_text' in value and (not isinstance(value['share_text'], str)
                or not 1 <= len(value['share_text']) <= 200 or not value['share_text'].strip()
                or any(ord(c) < 32 for c in value['share_text'])):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise ValueError('DAILY_VIDEO_INPUT_INVALID') from None
    return dict(value) | ({'staging': dict(value['staging'])} if 'staging' in value else {})


def validate_video(path):
    """Validate the MP4 container and decode both expected media streams."""
    from runtime.media.music_reply import _media_duration_seconds
    path = Path(path)
    if (path.suffix.lower() != '.mp4' or not path.is_file()
            or not 16 <= path.stat().st_size <= 2147483648):
        raise ValueError('DAILY_VIDEO_MP4_INVALID')
    with path.open('rb') as stream:
        if stream.read(12)[4:8] != b'ftyp':
            raise ValueError('DAILY_VIDEO_MP4_INVALID')
    duration = _media_duration_seconds(path, required_streams=('0:v:0', '0:a:0'))
    if duration is None or not 5 <= duration <= 15.1:
        raise ValueError('DAILY_VIDEO_MP4_INVALID')


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _hash(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def capture_caption(value, now):
    """A delayed clip keeps its frozen capture time, including across dates."""
    captured = datetime.fromisoformat(value['event_at']).astimezone(LOCAL)
    local_now = now.astimezone(LOCAL)
    share = value.get('share_text', '').strip()
    if (local_now - captured).total_seconds() < 60:
        return share
    when = (tr('今天 ') if captured.date() == local_now.date() else tr('{date} ', date=captured.strftime('%Y-%m-%d')))
    stamp = tr('这是{when}{time}那次洗澡后的样子。' if value['event_kind'] == 'bath_finished' else '这是{when}{time}拍的。',
               when=when, time=captured.strftime('%H:%M'))
    return (share + '\n' if share else '') + stamp


def select_candidate(value, candidates):
    """Forward same-call staging; event facts remain bound to the supplied source."""
    if (not isinstance(value, dict) or not {'event_id', 'spoken_text'} <= set(value) <= {'event_id', 'spoken_text', 'staging', 'share_text'}
            or not isinstance(value['spoken_text'], str) or not 1 <= len(value['spoken_text'].strip()) <= 100):
        raise ValueError('DAILY_VIDEO_SELECTION_INVALID')
    candidate = next((item for item in candidates or [] if item['event_id'] == value['event_id']), None)
    if candidate is None:
        raise ValueError('DAILY_VIDEO_SOURCE_UNAVAILABLE')
    selected = validate_input({key: candidate[key] for key in _FIELDS - {'spoken_text'}} |
                              {'spoken_text': value['spoken_text'].strip()} |
                              ({'place': candidate['place']} if candidate.get('place') else {}))
    for key in ('staging', 'share_text'):
        if key not in value:
            continue
        try:
            selected = validate_input(selected | {key: value[key]})
        except ValueError:
            pass  # Invalid optional copy never repeats the writer or discards normal chat.
    return selected


def tier_allows_video(settings):
    """Daily videos are the 文字＋声音＋视频 tier's media; text and audio users never receive one."""
    try:
        return settings.tier_snapshot() == 'video'
    except Exception:
        return False  # An unreadable setting is never consent.


def is_current_activity(candidate, observed):
    """Her moment is the record the world view takes her place from: ids decide, never wording.

    Both sides must carry a structured place, or the video is not offered."""
    place, there = candidate.get('place'), (observed or {}).get('place')
    return bool(observed and observed.get('live') is True
                and candidate.get('source_id', candidate.get('event_id')) == observed.get('source_id')
                and isinstance(place, dict) and isinstance(there, dict)
                and place.get('place_id') and place.get('place_id') == there.get('place_id')
                and place.get('name') == there.get('name'))


def scene_matches(scene, source):
    if source['event_kind'] == 'moment':
        # Announced by its own capability; public scene kinds stay legacy-only.
        return scene['scene_id'] == 'daily_place' and bool(source.get('place') and source['place']['stage'] == 'arrived')
    if source['event_kind'] not in scene['event_kinds']:
        return False
    if scene['scene_id'] == 'daily_place':
        return bool(source.get('place') and source['place']['stage'] == 'arrived')
    return source['location'] in [scene['scene_id'], *scene['locations']]


class DailyVideoWorker:
    def __init__(self, directory, service, binding, api_factory, event_store,
                 *, validator=validate_video, quiet_seconds=2, clock=lambda: datetime.now(timezone.utc), author_candidate=None,
                 consent=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / 'queue.sqlite3'
        self.service, self.binding = service, list(binding)
        if service.bindings.get('qq') != tuple(binding):
            raise ValueError('DAILY_VIDEO_BINDING_CONFLICT')
        self.api_factory, self.event_store = api_factory, event_store
        self.author_candidate = author_candidate
        # The user's video tier. None means no settings are attached (tests, manual use).
        self.consent = consent
        self.validator, self.quiet_seconds = validator, quiet_seconds
        self.clock = clock
        self.tasks, self.send = {}, None
        self.share_lock = asyncio.Lock()
        self.closed = False
        self.user_revision = service.user_revision
        self.last_input = time.monotonic()
        self.scenes, self.capabilities_at = [], 0
        self.moments = False
        interrupted = []
        with sqlite3.connect(self.database) as db:
            db.execute('CREATE TABLE IF NOT EXISTS daily_video_jobs (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS daily_video_preparations (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            for event_id, raw in db.execute('SELECT event_id,payload FROM daily_video_preparations').fetchall():
                row = json.loads(raw)
                if row.get('status') == 'AUTHORING':
                    row['status'] = 'UNKNOWN'  # The paid writer may have completed before a crash.
                    db.execute('UPDATE daily_video_preparations SET payload=? WHERE event_id=?', (_json(row), event_id))
                if row.get('status') == 'UNKNOWN' and row.get('candidate', {}).get('event_kind') == 'moment':
                    if not db.execute('SELECT 1 FROM daily_video_jobs WHERE event_id=?', (event_id,)).fetchone():
                        interrupted.append(event_id)
            # A process can die after platform acceptance but before saving ACK.
            for event_id, raw in db.execute('SELECT event_id,payload FROM daily_video_jobs').fetchall():
                row = json.loads(raw)
                if row.get('delivery_status') == 'SENDING':
                    row['delivery_status'] = 'UNKNOWN'
                    db.execute('UPDATE daily_video_jobs SET payload=? WHERE event_id=?', (_json(row), event_id))
        if interrupted:
            store = self.event_store() if callable(self.event_store) else self.event_store
            if store is not None:
                for event_id in interrupted:
                    store.fail_daily_video_share(event_id, now=self.clock())

    def _consented(self):
        check = getattr(self, 'consent', None)
        if check is None:
            return True
        try:
            return bool(check())
        except Exception:
            return False

    def _is_current(self, candidate, store, now):
        reader = getattr(store, 'observed_place', None)
        if not callable(reader):
            return False
        try:
            if candidate.get('event_kind') == 'moment':
                candidate = dict(candidate, source_id=store.daily_video_source_id(candidate['event_id']))
            return is_current_activity(candidate, reader(now=now))
        except Exception:
            return False

    def _save(self, row):
        with sqlite3.connect(self.database) as db:
            db.execute('INSERT OR REPLACE INTO daily_video_jobs VALUES (?,?)', (row['event_id'], _json(row)))

    def get(self, event_id):
        with sqlite3.connect(self.database) as db:
            result = db.execute('SELECT payload FROM daily_video_jobs WHERE event_id=?', (event_id,)).fetchone()
        return json.loads(result[0]) if result else None

    def preparation(self, event_id):
        with sqlite3.connect(self.database) as db:
            row = db.execute('SELECT payload FROM daily_video_preparations WHERE event_id=?', (event_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save_preparation(self, row):
        with sqlite3.connect(self.database) as db:
            db.execute('INSERT OR REPLACE INTO daily_video_preparations VALUES (?,?)', (row['event_id'], _json(row)))

    async def enqueue(self, value):
        value = validate_input(value)
        row = self.get(value['event_id'])
        if row:
            if row['input'] != value:
                raise ValueError('DAILY_VIDEO_EVENT_CONFLICT')
            if row['binding'] != self.binding:
                raise ValueError('DAILY_VIDEO_BINDING_CONFLICT')
        else:
            if not self._consented():
                raise ValueError('DAILY_VIDEO_CONSENT_REQUIRED')
            if value['event_kind'] == 'moment':
                store = self.event_store() if callable(self.event_store) else self.event_store
                if store is None:
                    raise ValueError('DAILY_VIDEO_SOURCE_UNAVAILABLE')
                claimed = store.reserve_daily_video_share(value, now=self.clock())
            row = dict(event_id=value['event_id'], input=value, binding=self.binding,
                       status='PENDING', delivery_status='PENDING', world_status='PENDING')
            try:
                self._save(row)  # Durable before any network request is scheduled.
            except BaseException:
                if value['event_kind'] == 'moment' and claimed and self.get(value['event_id']) is None:
                    store.fail_daily_video_share(value['event_id'], now=self.clock())
                raise
        self._schedule(row)
        return self.get(value['event_id'])

    async def refresh_capabilities(self):
        """Refresh outside the chat lock; failures disable new automatic spending."""
        self.capabilities_at = time.monotonic()
        self.scenes, self.moments = [], False
        try:
            data = await asyncio.wait_for(self.api_factory().request('capabilities', {}), 5)
            if data.get('daily_video_enabled') is not True or 'daily_video' not in data.get('kinds', []):
                return
            scenes = data.get('daily_video_scenes', [])
            if not isinstance(scenes, list) or len(scenes) > 100:
                return
            for scene in scenes:
                if (not isinstance(scene, dict) or set(scene) != {'scene_id', 'event_kinds', 'locations'}
                        or not isinstance(scene['scene_id'], str)
                        or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', scene['scene_id'])
                        or not isinstance(scene['event_kinds'], list)
                        or any(kind not in EVENT_KINDS for kind in scene['event_kinds'])
                        or not isinstance(scene['locations'], list)
                        or any(not isinstance(place, str) or not 1 <= len(place) <= 80 for place in scene['locations'])):
                    return
            self.scenes = scenes
            self.moments = data.get('daily_video_moment') is True
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def candidates(self, *, now=None, current_only=True):
        """No model call: scene aliases are approved and owned by the server.

        Nothing is offered without the user's video tier. A chat offer is her current
        moment, so by default only the event her world view puts her in qualifies; a
        background clip prepared ahead of an event (current_only=False) is not presented as now."""
        now = now or self.clock()
        if not self._consented() or not self.scenes or time.monotonic() - self.capabilities_at > 120:
            return []
        store = self.event_store() if callable(self.event_store) else self.event_store
        if store is None or not callable(getattr(store, 'daily_video_sources', None)):
            return []
        result = []
        observed = None
        if current_only:
            reader = getattr(store, 'observed_place', None)
            if not callable(reader):
                return []
            try:
                observed = reader(now=now)
            except Exception:
                return []
        for source in store.daily_video_sources(now=now, moment=self.moments):
            if self.get(source['event_id']) is not None or self.preparation(source['event_id']) is not None:
                continue
            if current_only and not is_current_activity(source, observed):
                continue
            ordered = sorted(self.scenes, key=lambda s: s['scene_id']=='daily_place')
            scene = next((scene for scene in ordered if scene_matches(scene, source)), None)
            if scene is not None:
                result.append(dict(version=1, certainty='live', scene_id=scene['scene_id'],
                    target_date=datetime.fromisoformat(source['event_at']).astimezone(LOCAL).date().isoformat(),
                    **{key: source[key] for key in ('event_id', 'event_at', 'event_kind', 'detail', 'location')},
                    **({'place': source['place']} if source.get('place') else {}),
                    **({'source_id': source['source_id']} if source.get('source_id') else {}),
                    **({'event_status': 'preparing'} if source.get('event_status') == 'preparing' else {})))
        return result

    async def share(self, candidate, author):
        """A short video of her current moment chosen in place of a photo: author once, then queue."""
        async with self.share_lock:
            return await self._share(candidate, author)

    async def _share(self, candidate, author):
        existing = self.get(candidate['event_id'])
        if existing is not None:
            return existing
        if not self._consented():
            raise ValueError('DAILY_VIDEO_CONSENT_REQUIRED')
        store = self.event_store() if callable(self.event_store) else self.event_store
        if not self._is_current(candidate, store, self.clock()):
            raise ValueError('DAILY_VIDEO_SOURCE_UNAVAILABLE')  # She has moved on: it is no longer her moment.
        if not self.scenes or time.monotonic() - self.capabilities_at > 120:
            await self.refresh_capabilities()
        if not self.scenes or candidate['event_kind'] == 'moment' and not self.moments:
            raise ValueError('DAILY_VIDEO_CAPABILITY_UNAVAILABLE')
        if not any(scene['scene_id'] == candidate['scene_id'] and scene_matches(scene, candidate)
                   for scene in self.scenes):
            raise ValueError('DAILY_VIDEO_SCENE_UNAVAILABLE')
        if self.preparation(candidate['event_id']) is not None:
            raise ValueError('DAILY_VIDEO_AUTHORING_UNKNOWN')
        if candidate['event_kind'] == 'moment':
            store.reserve_daily_video_share(candidate, now=self.clock())
        preparation = dict(event_id=candidate['event_id'], candidate=candidate, status='AUTHORING')
        with sqlite3.connect(self.database) as db:
            if not db.execute('INSERT OR IGNORE INTO daily_video_preparations VALUES (?,?)',
                              (candidate['event_id'], _json(preparation))).rowcount:
                raise ValueError('DAILY_VIDEO_AUTHORING_UNKNOWN')
        try:
            value = validate_input(await author(dict(candidate)))
            if any(value[key] != candidate[key] for key in _FIELDS - {'spoken_text'}):
                raise ValueError('DAILY_VIDEO_SOURCE_UNAVAILABLE')
            if store is None or not store.daily_video_can_prepare(value, now=self.clock()):
                raise ValueError('DAILY_VIDEO_SOURCE_UNAVAILABLE')
            if not self.scenes or candidate['event_kind'] == 'moment' and not self.moments:
                raise ValueError('DAILY_VIDEO_CAPABILITY_UNAVAILABLE')
            if not any(scene['scene_id'] == candidate['scene_id'] and scene_matches(scene, candidate)
                       for scene in self.scenes):
                raise ValueError('DAILY_VIDEO_SCENE_UNAVAILABLE')
            return await self.enqueue(value)
        except BaseException:
            if candidate['event_kind'] == 'moment' and self.get(candidate['event_id']) is None:
                store.fail_daily_video_share(candidate['event_id'], now=self.clock())
            preparation['status'] = 'UNKNOWN'
            self._save_preparation(preparation)
            raise

    async def intake_preparations(self):
        """Author real bath starts independently of new messages or the chat lock."""
        if self.closed or self.author_candidate is None:
            return
        for candidate in self.candidates(current_only=False):
            if candidate.get('event_status') != 'preparing':
                continue
            row = dict(event_id=candidate['event_id'], candidate=candidate, status='AUTHORING')
            self._save_preparation(row)  # Reserve before the detached writer can incur cost.
            key = 'author:' + candidate['event_id']
            task = asyncio.create_task(self._author_preparation(row))
            self.tasks[key] = task
            def finished(done, key=key):
                self.tasks.pop(key, None)
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(finished)

    async def _author_preparation(self, row):
        try:
            value = validate_input(await self.author_candidate(dict(row['candidate'])))
            if any(value[key] != row['candidate'][key] for key in _FIELDS - {'spoken_text'}):
                raise ValueError('DAILY_VIDEO_SOURCE_UNAVAILABLE')
            store = self.event_store() if callable(self.event_store) else self.event_store
            if store is None or not store.daily_video_can_prepare(value, now=self.clock()):
                raise ValueError('DAILY_VIDEO_SOURCE_UNAVAILABLE')
            if self.get(value['event_id']) is None:
                await self.enqueue(value)
            row.update(status='AUTHORED', input=self.get(value['event_id'])['input'])
            self._save_preparation(row)
        except BaseException:
            # No automatic re-author after transport/protocol ambiguity. The
            # existing paid call may have completed; ordinary chat can continue.
            row['status'] = 'UNKNOWN'
            self._save_preparation(row)
            raise

    async def intake_replies(self):
        """The same-call author's selected metadata becomes a job after text ACK."""
        from .events import PersonalMessage
        from .service import persist_state
        owner = PersonalMessage('qq', *self.binding, '', '').binding_id
        for reply in self.service.rows:
            if (reply.get('channel') != 'qq' or reply.get('binding_id') != owner
                    or reply.get('delivery_status') != 'DELIVERED' or not reply.get('daily_video_request')
                    or reply.get('daily_video_status') in {'QUEUED', 'REJECTED'}):
                continue
            try:
                value = validate_input(reply['daily_video_request'])
                if self.get(value['event_id']) is None:
                    if not self.scenes or time.monotonic() - self.capabilities_at > 120:
                        continue
                    frozen = next((item for item in reply.get('daily_video_candidates', [])
                        if item.get('event_id') == value['event_id']), None)
                    if frozen is None or not any(scene['scene_id'] == value['scene_id']
                            and scene_matches(scene, frozen)
                            for scene in self.scenes):
                        raise ValueError('DAILY_VIDEO_SCENE_UNAVAILABLE')
                    store = self.event_store() if callable(self.event_store) else self.event_store
                    if not self._is_current(frozen, store, self.clock()):
                        raise ValueError('DAILY_VIDEO_SOURCE_UNAVAILABLE')
                store = self.event_store() if callable(self.event_store) else self.event_store
                if store is None or not store.daily_video_can_prepare(value, now=self.clock()):
                    raise ValueError('DAILY_VIDEO_SOURCE_UNAVAILABLE')
                if self.get(value['event_id']) is None:
                    await self.enqueue(value)
                reply['daily_video_status'] = 'QUEUED'
                reply.pop('daily_video_error_code', None)
            except ValueError as exc:
                reply.update(daily_video_status='REJECTED', daily_video_error_code=str(exc))
            await persist_state(self.service.persist)

    async def recover(self, event_id):
        """Explicit recovery reuses the original receipt/order; never another key."""
        row = self.get(event_id)
        if row is None:
            raise ValueError('DAILY_VIDEO_NOT_FOUND')
        if row['binding'] != self.binding:
            raise ValueError('DAILY_VIDEO_BINDING_CONFLICT')
        if row['delivery_status'] in {'SENDING', 'UNKNOWN'}:
            raise ValueError('DAILY_VIDEO_DELIVERY_UNKNOWN')
        if row['status'] == 'FAILED':
            row['status'] = 'PENDING'
            self._save(row)
        self._schedule(row)
        return self.get(event_id)

    def bind(self, channel, send):
        if channel == 'qq':
            self.send = send
            self.resume()

    def resume(self):
        if self.closed:
            return
        with sqlite3.connect(self.database) as db:
            rows = [json.loads(raw) for (raw,) in db.execute('SELECT payload FROM daily_video_jobs')]
        for row in sorted(rows, key=lambda row: datetime.fromisoformat(row['input']['event_at'])):
            self._schedule(row)

    def _schedule(self, row):
        key = row['event_id']
        if (self.closed or key in self.tasks or row['binding'] != self.binding
                or row['delivery_status'] in {'SENDING', 'UNKNOWN'}
                or row['status'] == 'FAILED'
                or row['delivery_status'] == 'DELIVERED' and row['world_status'] == 'COMMITTED'
                and row.get('memory_status') != 'PENDING'):
            return
        if row['delivery_status'] != 'DELIVERED' and not self._consented():
            # The tier no longer allows video: neither generate nor send, and never revive it silently.
            row.update(status='FAILED', error_code='DAILY_VIDEO_CONSENT_REQUIRED')
            self._save(row)
            return
        if row['status'] == 'READY' and row['delivery_status'] != 'DELIVERED' and not self._can_send(row):
            return
        task = asyncio.create_task(self._run(row))
        self.tasks[key] = task
        def finished(done):
            self.tasks.pop(key, None)
            if not done.cancelled():
                done.exception()  # The sanitized failure remains durable.
        task.add_done_callback(finished)

    async def _run(self, row):
        try:
            identity = hashlib.sha256(row['event_id'].encode()).hexdigest()
            output = self.directory / (identity + '.mp4')
            if row['delivery_status'] == 'DELIVERED':
                self._commit(row)
                await self._remember(row)
                from runtime.gpu_cleanup import acknowledge_result
                await acknowledge_result(self.api_factory(), row['task_id'], output)
                return
            receipt = self.directory / (identity + '.task.json')
            valid = output.is_file() and row.get('output_sha256') == _hash(output)
            if not valid:
                if (row.get('receipt_required') or row.get('task_id')) and not receipt.is_file():
                    raise RuntimeError('GPU_RECOVERY_REQUIRED')
                if row.get('task_id'):
                    try:
                        saved = json.loads(receipt.read_text(encoding='utf-8'))
                        if saved.get('task_id') != row['task_id']:
                            raise ValueError()
                    except (ValueError, OSError, AttributeError):
                        raise RuntimeError('GPU_RECOVERY_REQUIRED') from None
                row['status'] = 'GENERATING'
                self._save(row)
                api = self.api_factory()
                def progress(phase, task):
                    row['receipt_required'] = receipt.is_file()
                    if task.get('task_id'):
                        row['task_id'] = task['task_id']
                    self._save(row)
                api.progress = progress
                result = await api.generate('daily_video', row['input'], output,
                    receipt_path=receipt, timeout=7200)
                row['task_id'] = result['task_id']
            if not valid:
                await asyncio.to_thread(self.validator, output)
            row.update(status='READY', output_sha256=_hash(output))
            row.pop('error_code', None)
            self._save(row)
            # A ready artifact never certifies an event, moves actual world, or
            # enters memory. Keep it pending until the author and QQ are ready.
            if not self._can_send(row):
                return
            async with self.service.lock:
                if not self._can_send(row):
                    return
                store = self.event_store() if callable(self.event_store) else self.event_store
                store.reserve_daily_video_delivery(row['input'], task_id=row['task_id'], now=self.clock())
                row['delivery_status'] = 'SENDING'
                self._save(row)  # Reserve before the transport can accept it.
                try:
                    caption = capture_caption(row['input'], self.clock())
                    options = {'caption': caption} if caption else {}
                    message_id = await self.send.video(output, eligible=lambda: self._can_send(row), **options)
                    if (isinstance(message_id, bool) or not isinstance(message_id, (str, int))
                            or not str(message_id)):
                        raise RuntimeError('QQ_SEND_UNCONFIRMED')
                except BaseException as exc:
                    from .qq import QQVideoDeferred
                    if isinstance(exc, QQVideoDeferred):
                        row['delivery_status'] = 'PENDING'
                        self._save(row)
                        return
                    row['delivery_status'] = 'UNKNOWN'
                    self._save(row)
                    raise
                # Only an acknowledged send owes the conversation a memory record.
                row.update(delivery_status='DELIVERED', message_id=str(message_id),
                           delivered_at=self.clock().isoformat(), memory_status='PENDING')
                self._save(row)
            self._commit(row)
            await self._remember(row)
            # Acknowledging remote cleanup is safe only after the QQ receipt.
            from runtime.gpu_cleanup import acknowledge_result
            await acknowledge_result(self.api_factory(), row['task_id'], output)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if row['delivery_status'] not in {'UNKNOWN', 'DELIVERED'}:
                row['status'] = 'FAILED'
            code = getattr(exc, 'code', str(exc))
            row['error_code'] = code if isinstance(code, str) and re.fullmatch(r'(?:GPU|QQ|DAILY_VIDEO)_[A-Z_]{1,80}', code) else 'DAILY_VIDEO_UNAVAILABLE'
            self._save(row)

    def _can_send(self, row):
        if self.service.user_revision != self.user_revision:
            self.user_revision = self.service.user_revision
            self.last_input = time.monotonic()
        store = self.event_store() if callable(self.event_store) else self.event_store
        return (self._consented() and store is not None and callable(getattr(self.send, 'video', None))
                and getattr(self.send, 'is_available', lambda: True)()
                and not self.service.pending('qq')
                and time.monotonic() - self.last_input >= self.quiet_seconds
                and store.daily_video_event_matches(row['input'], now=self.clock()))

    def _commit(self, row):
        from runtime.reply.media_delivery import make_daily_delivery
        event = make_daily_delivery(row['input'], task_id=row['task_id'], message_id=row['message_id'],
                                    occurred_at=datetime.fromisoformat(row['delivered_at']))
        store = self.event_store() if callable(self.event_store) else self.event_store
        if store is None:
            raise RuntimeError('DAILY_VIDEO_WORLD_UNAVAILABLE')
        store.record_media_delivery(event)
        row['world_status'] = 'COMMITTED'
        self._save(row)

    async def _remember(self, row):
        """One memory record per delivered video, from facts already known and without any model call.

        Videos delivered before this existed carry no memory_status and are left alone."""
        if row.get('memory_status') != 'PENDING':
            return
        from . import daily_video_memory as memory
        from .events import PersonalMessage
        from .service import persist_state
        try:
            store = self.event_store() if callable(self.event_store) else self.event_store
            reader = getattr(store, 'daily_video_event_facts', None)
            try:
                facts = reader(row['input']) if callable(reader) else None
            except Exception:
                facts = None
            delivered_at = datetime.fromisoformat(row['delivered_at'])
            record = memory.build(row['input'], facts=facts, caption=capture_caption(row['input'], delivered_at),
                                  delivered_at=delivered_at)
            owner = PersonalMessage('qq', *self.binding, '', '').binding_id
            outcome = memory.attach(self.service.rows, owner, record)
            if outcome != 'NO_CONVERSATION':
                await persist_state(self.service.persist)
            row['memory_status'] = outcome if outcome == 'NO_CONVERSATION' else 'COMMITTED'
        except asyncio.CancelledError:
            raise
        except Exception:
            row['memory_attempts'] = row.get('memory_attempts', 0) + 1
            row['memory_status'] = 'FAILED' if row['memory_attempts'] >= 5 else 'PENDING'
        self._save(row)

    async def wait_idle(self):
        while self.tasks:
            await asyncio.gather(*tuple(self.tasks.values()))
            await asyncio.sleep(0)

    async def monitor(self):
        self.resume()
        while not self.closed:
            if time.monotonic() - self.capabilities_at >= 60:
                await self.refresh_capabilities()
            await self.intake_replies()
            await self.intake_preparations()
            self.resume()
            await asyncio.sleep(2)

    async def close(self):
        self.closed = True
        tasks = tuple(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def install_routes(app, server, runtime_key):
    """Manual, origin-checked local API; callers cannot choose a QQ binding."""
    from .setup import _cors_headers, CONFIRM_HEADER, CONFIRM_VALUE
    @web.middleware
    async def boundary(request, handler):
        if request.path not in {PATH, RECOVER_PATH}:
            return await handler(request)
        headers = _cors_headers(request, server, preflight=request.method == 'OPTIONS')
        if headers is None:
            return web.json_response({'error': 'PERSONAL_CHAT_ORIGIN_FORBIDDEN'}, status=403)
        from original_client_setup_api import _SERVICE_KEY, SESSION_HEADER, LLMSetupError
        if request.method == 'OPTIONS':
            headers['Access-Control-Allow-Headers'] += ', ' + SESSION_HEADER
            return web.Response(status=204, headers=headers)
        if request.method not in ({'POST'} if request.path == RECOVER_PATH else {'POST', 'GET'}):
            return web.json_response({'error': 'METHOD_NOT_ALLOWED'}, status=405, headers=headers)
        if request.headers.get(CONFIRM_HEADER) != CONFIRM_VALUE:
            return web.json_response({'error': 'PERSONAL_CHAT_CONFIRM_REQUIRED'}, status=403, headers=headers)
        setup = request.app.get(_SERVICE_KEY)
        if setup is not None:
            try:
                setup.require_session(request.headers.get(SESSION_HEADER, ''))
            except LLMSetupError as exc:
                return web.json_response({'error': exc.code}, status=exc.status, headers=headers)
        runtime = request.app.get(runtime_key, {})
        worker = runtime.get('daily_video')
        from .backend import selected_channels
        if worker is None or 'qq' not in selected_channels(server):
            return web.json_response({'error': 'DAILY_VIDEO_QQ_NOT_CONFIGURED'}, status=503, headers=headers)
        try:
            if request.method == 'POST':
                body = await request.json()
                if request.path == RECOVER_PATH:
                    if not isinstance(body, dict) or set(body) != {'event_id'} or not isinstance(body['event_id'], str):
                        raise ValueError('DAILY_VIDEO_INPUT_INVALID')
                    row = await worker.recover(body['event_id'])
                else:
                    row = await worker.enqueue(body)
            else:
                row = worker.get(request.query.get('event_id', ''))
                if row is None:
                    return web.json_response({'error': 'DAILY_VIDEO_NOT_FOUND'}, status=404, headers=headers)
                if row['binding'] != worker.binding:
                    raise ValueError('DAILY_VIDEO_BINDING_CONFLICT')
            # No spoken text, credentials, filesystem paths or binding IDs.
            result = {key: row[key] for key in ('event_id', 'status', 'delivery_status', 'world_status', 'task_id', 'error_code') if key in row}
            return web.json_response(result, status=202 if request.method == 'POST' else 200, headers=headers)
        except (ValueError, TypeError) as exc:
            code = str(exc)
            if not re.fullmatch(r'DAILY_VIDEO_[A-Z_]{1,80}', code):
                code = 'DAILY_VIDEO_INPUT_INVALID'
            return web.json_response({'error': code}, status=409 if code.endswith(('CONFLICT', 'UNKNOWN')) else 400, headers=headers)
        except Exception:
            return web.json_response({'error': 'DAILY_VIDEO_UNAVAILABLE'}, status=503, headers=headers)
    app.middlewares.append(boundary)
