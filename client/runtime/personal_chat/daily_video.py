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

LOCAL = timezone(timedelta(hours=8))
EVENT_KINDS = {'housework', 'walk', 'bath_finished', 'shopping', 'meal', 'wake_up'}
PATH = '/toy/companion/daily-video'
RECOVER_PATH = PATH + '/recover'
_FIELDS = {'version', 'event_id', 'target_date', 'event_at', 'event_kind',
           'scene_id', 'certainty', 'spoken_text'}


def validate_input(value):
    try:
        if (not isinstance(value, dict) or not _FIELDS <= set(value) <= _FIELDS | {'staging', 'share_text'}
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
    when = ('今天 ' if captured.date() == local_now.date() else captured.strftime('%Y-%m-%d '))
    stamp = ('这是' + when + captured.strftime('%H:%M')
             + ('那次洗澡后的样子。' if value['event_kind'] == 'bath_finished' else '拍的。'))
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
                              {'spoken_text': value['spoken_text'].strip()})
    for key in ('staging', 'share_text'):
        if key not in value:
            continue
        try:
            selected = validate_input(selected | {key: value[key]})
        except ValueError:
            pass  # Invalid optional copy never repeats the writer or discards normal chat.
    return selected


class DailyVideoWorker:
    def __init__(self, directory, service, binding, api_factory, event_store,
                 *, validator=validate_video, quiet_seconds=2, clock=lambda: datetime.now(timezone.utc), author_candidate=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / 'queue.sqlite3'
        self.service, self.binding = service, list(binding)
        if service.bindings.get('qq') != tuple(binding):
            raise ValueError('DAILY_VIDEO_BINDING_CONFLICT')
        self.api_factory, self.event_store = api_factory, event_store
        self.author_candidate = author_candidate
        self.validator, self.quiet_seconds = validator, quiet_seconds
        self.clock = clock
        self.tasks, self.send = {}, None
        self.closed = False
        self.user_revision = service.user_revision
        self.last_input = time.monotonic()
        self.scenes, self.capabilities_at = [], 0
        with sqlite3.connect(self.database) as db:
            db.execute('CREATE TABLE IF NOT EXISTS daily_video_jobs (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS daily_video_preparations (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            for event_id, raw in db.execute('SELECT event_id,payload FROM daily_video_preparations').fetchall():
                row = json.loads(raw)
                if row.get('status') == 'AUTHORING':
                    row['status'] = 'UNKNOWN'  # The paid writer may have completed before a crash.
                    db.execute('UPDATE daily_video_preparations SET payload=? WHERE event_id=?', (_json(row), event_id))
            # A process can die after platform acceptance but before saving ACK.
            for event_id, raw in db.execute('SELECT event_id,payload FROM daily_video_jobs').fetchall():
                row = json.loads(raw)
                if row.get('delivery_status') == 'SENDING':
                    row['delivery_status'] = 'UNKNOWN'
                    db.execute('UPDATE daily_video_jobs SET payload=? WHERE event_id=?', (_json(row), event_id))

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
            row = dict(event_id=value['event_id'], input=value, binding=self.binding,
                       status='PENDING', delivery_status='PENDING', world_status='PENDING')
            self._save(row)  # Durable before any network request is scheduled.
        self._schedule(row)
        return self.get(value['event_id'])

    async def refresh_capabilities(self):
        """Refresh outside the chat lock; failures disable new automatic spending."""
        self.capabilities_at = time.monotonic()
        self.scenes = []
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
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def candidates(self, *, now=None):
        """No model call: scene aliases are approved and owned by the server."""
        now = now or self.clock()
        if not self.scenes or time.monotonic() - self.capabilities_at > 120:
            return []
        store = self.event_store() if callable(self.event_store) else self.event_store
        if store is None or not callable(getattr(store, 'daily_video_sources', None)):
            return []
        result = []
        for source in store.daily_video_sources(now=now):
            if self.get(source['event_id']) is not None or self.preparation(source['event_id']) is not None:
                continue
            scene = next((scene for scene in self.scenes if source['event_kind'] in scene['event_kinds']
                and source['location'] in [scene['scene_id'], *scene['locations']]), None)
            if scene is not None:
                result.append(dict(version=1, certainty='live', scene_id=scene['scene_id'],
                    target_date=datetime.fromisoformat(source['event_at']).astimezone(LOCAL).date().isoformat(),
                    **{key: source[key] for key in ('event_id', 'event_at', 'event_kind', 'detail', 'location')},
                    **({'event_status': 'preparing'} if source.get('event_status') == 'preparing' else {})))
        return result

    async def intake_preparations(self):
        """Author real bath starts independently of new messages or the chat lock."""
        if self.closed or self.author_candidate is None:
            return
        for candidate in self.candidates():
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
                            and value['event_kind'] in scene['event_kinds']
                            and frozen.get('location') in [scene['scene_id'], *scene['locations']]
                            for scene in self.scenes):
                        raise ValueError('DAILY_VIDEO_SCENE_UNAVAILABLE')
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
                or row['delivery_status'] == 'DELIVERED' and row['world_status'] == 'COMMITTED'):
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
                row.update(delivery_status='DELIVERED', message_id=str(message_id),
                           delivered_at=self.clock().isoformat())
                self._save(row)
            self._commit(row)
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
        return (store is not None and callable(getattr(self.send, 'video', None))
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
