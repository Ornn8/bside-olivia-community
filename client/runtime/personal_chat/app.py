"""The phone app as a chat channel (`app`): one more transport behind the same reply pipeline.

A message the user writes on the phone reaches this computer through the desk line of the call service
(runtime/call/desk.py hands the frames to the hub below). It is saved like a QQ message, acknowledged
to the cloud, and then answered by the unchanged personal-chat service: the same decisions, her voice,
photos, stickers, billing and memory writes. Only the way things are sent differs: every item she
sends is stored here first (a small outbox, so a closed line loses nothing), its media is uploaded to
the cloud, and the item itself goes out as an `app_out` frame.

The wire format is in the phone app protocol (items, kinds, media, frames); the pieces here:
  Hub     the desk line side: inbound queue, outbox flush, earlier messages, profile, typing
  Sender  the object the service sends through (the same call surface as the QQ transport)
  Store   the app chat log and the outbox on disk, plus the media cache
  run_app the job the personal-chat backend runs for this channel
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import tempfile
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

from .channels import MEDIA_HINT
from .events import PersonalMessage, combine, mergeable_by_sent_time

log = logging.getLogger(__name__)

CHANNEL = 'app'
ACCOUNT, OWNER = 'linli', 'owner'
NUDGE_TEXT = '[他戳了戳你]'
LOG_ITEMS = 1500
HISTORY_LIMIT = 60
HISTORY_MEDIA = 30
MEDIA_MAX_BYTES = 25 * 1024 * 1024
CACHE_BYTES = 300 * 1024 * 1024
CACHE_DAYS = 14
ACK_TIMEOUT = 20
TYPING_REFRESH = 8
MERGE_SECONDS = 2
MIMES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif',
         '.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.ogg': 'audio/ogg', '.m4a': 'audio/mp4', '.opus': 'audio/ogg',
         '.mp4': 'video/mp4', '.webm': 'video/webm'}


def cloud():
    """(base url, Olivia key) of the call service, or (None, None) without an account."""
    from runtime.call.http import _cloud
    return _cloud()


def enabled() -> bool:
    return cloud()[0] is not None


FLAG = 'app-enabled.json'


def active(server) -> bool:
    """The app channel runs once a phone has connected to this computer (the flag stays after a restart)."""
    root = server._state_root()
    return root is not None and enabled() and (Path(root) / 'personal-chat' / FLAG).is_file()


def mark_active(server) -> bool:
    """Remember that a phone is in use. True the first time."""
    root = server._state_root()
    if root is None or active(server):
        return False
    _write(Path(root) / 'personal-chat' / FLAG, {'since': time.time()})
    return True


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read(path: Path, default):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


class Store:
    """The app chat log (both directions, with the ids the phone knows), the outbox and the media cache."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.media_dir = self.root / 'media'
        self.log = _read(self.root / 'chat-log.json', [])
        self.outbox = _read(self.root / 'outbox.json', [])
        self.meta = _read(self.root / 'media-meta.json', {})
        self.ids = {item.get('id') for item in self.log}

    # -- chat log
    def add(self, item: dict) -> bool:
        if item['id'] in self.ids:
            return False
        self.ids.add(item['id'])
        self.log.append(item)
        if len(self.log) > LOG_ITEMS:
            for gone in self.log[:len(self.log) - LOG_ITEMS]:
                self.ids.discard(gone.get('id'))
            del self.log[:len(self.log) - LOG_ITEMS]
        _write(self.root / 'chat-log.json', self.log)
        return True

    def recent(self, limit: int, before=None) -> list:
        items = [i for i in self.log if before is None or i.get('ts', 0) < before]
        return items[-limit:]

    # -- outbox
    def queue(self, item: dict):
        self.outbox.append(item['id'])
        _write(self.root / 'outbox.json', self.outbox)

    def done(self, item_id: str):
        if item_id in self.outbox:
            self.outbox.remove(item_id)
            _write(self.root / 'outbox.json', self.outbox)

    def waiting(self) -> list:
        by_id = {item['id']: item for item in self.log if item.get('id') in self.outbox}
        return [by_id[i] for i in self.outbox if i in by_id]

    # -- media cache (content addressed)
    def media_path(self, media_id: str) -> Path:
        return self.media_dir / media_id

    def keep_media(self, data: bytes, meta: dict) -> str:
        media_id = hashlib.sha256(data).hexdigest()[:32]
        path = self.media_path(media_id)
        if not path.is_file():
            self.media_dir.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.part')
            temporary.write_bytes(data)
            temporary.replace(path)
        self.meta[media_id] = meta
        _write(self.root / 'media-meta.json', self.meta)
        return media_id

    def prune(self, now=None):
        """Old media goes first, then the oldest until the folder fits; logged items keep working without it."""
        now = now or time.time()
        if not self.media_dir.is_dir():
            return
        files = sorted(((p.stat().st_mtime, p.stat().st_size, p) for p in self.media_dir.iterdir() if p.is_file()),
                       key=lambda row: row[0])
        total = sum(size for _, size, _ in files)
        keep = set(self.outbox)
        for item in self.log:
            if item.get('id') in keep and isinstance(item.get('media'), dict):
                keep.add(item['media'].get('id'))
        for mtime, size, path in files:
            if path.name in keep:
                continue
            if now - mtime > CACHE_DAYS * 86400 or total > CACHE_BYTES:
                try:
                    path.unlink()
                    total -= size
                    self.meta.pop(path.name, None)
                except OSError:
                    pass


def _mime(path: Path) -> str:
    return MIMES.get(path.suffix.lower(), 'application/octet-stream')


def _describe(data: bytes, mime: str, path: Path) -> dict:
    """seconds for audio and video, w and h for pictures; best effort, a number that cannot be read is left out."""
    meta = {'mime': mime, 'name': path.name}
    try:
        if mime == 'audio/wav':
            with wave.open(path.open('rb')) as wav:
                meta['seconds'] = round(wav.getnframes() / float(wav.getframerate()), 2)
        elif mime.startswith(('audio/', 'video/')):
            try:
                import mutagen
                found = mutagen.File(str(path))
                if found is not None and found.info.length:
                    meta['seconds'] = round(float(found.info.length), 2)
            except Exception:
                if mime == 'audio/mpeg':
                    meta['seconds'] = round(len(data) * 8 / 128000, 2)   # a rough figure at 128 kbit/s
        elif mime.startswith('image/'):
            import io
            from PIL import Image
            with Image.open(io.BytesIO(data)) as picture:
                meta['w'], meta['h'] = picture.size
    except Exception:
        pass
    return meta


def _face_label(text: str):
    from .qq_faces import FACE_IDS
    stripped = text.strip()
    if stripped.startswith('[') and stripped.endswith(']') and len(stripped) <= 34 and '[' not in stripped[1:-1]:
        face_id = FACE_IDS.get(stripped[1:-1].casefold())
        if face_id is not None:
            return face_id, stripped[1:-1]
    return None


def _inline_faces(text: str):
    import re
    from .qq_faces import FACE_IDS
    found = []
    for match in re.finditer(r'\[([^\[\]\r\n]{1,32})\]', text):
        face_id = FACE_IDS.get(match.group(1).casefold())
        if face_id is not None:
            found.append({'id': face_id, 'name': match.group(1)})
    return found


class Sender:
    """What the personal-chat service sends through. The same call surface as the QQ transport object."""

    def __init__(self, hub, event=None):
        self.hub, self.event = hub, event
        self.source = 'reply'
        self.reply_to = None
        if event is not None:
            quiet = not event.text.strip()
            self.source = 'proactive' if quiet else 'reply'
            if not quiet and event.text != NUDGE_TEXT:
                self.reply_to = event.message_id

    # -- texts, faces and pictures
    async def __call__(self, text, *, quote=True):
        if not isinstance(text, str) or not text.strip() or len(text) > 10000:
            raise ValueError('APP_REPLY_INVALID')
        face = _face_label(text)
        if face is not None:
            return await self._emit('face', text.strip(), face=face[0], face_name=face[1], quote=quote)
        inline = _inline_faces(text)
        return await self._emit('text', text, quote=quote, **({'faces': inline} if inline else {}))

    async def plain(self, text):
        return await self(text, quote=False)

    async def image(self, path):
        hint = MEDIA_HINT.get() or {}
        if hint.get('kind') == 'sticker':
            return await self._emit('sticker', '', path=path, quote=False, sticker=hint.get('text') or Path(path).stem)
        return await self._emit('image', hint.get('text') or '', path=path)

    async def audio(self, path):
        hint = MEDIA_HINT.get() or {}
        return await self._emit('voice', hint.get('text') or '', path=path, quote=False)

    async def video(self, path, reply_to=None, *, eligible=None, caption=''):
        if callable(eligible) and not eligible():
            from .qq import QQVideoDeferred
            raise QQVideoDeferred('QQ_VIDEO_DEFERRED')
        if not isinstance(caption, str) or len(caption) > 256:
            raise ValueError('APP_VIDEO_CAPTION_INVALID')
        return await self._emit('video', caption, path=path)

    async def file(self, path, name):
        """A finished audio file: a long speech (story, ASMR) is a voice item, a song or piano clip a song item."""
        path = Path(path)
        if not path.is_file() or path.suffix.lower() not in {'.mp3', '.wav', '.m4a', '.ogg'} or not isinstance(name, str) or not name:
            raise ValueError('APP_FILE_INVALID')
        hint = MEDIA_HINT.get() or {}
        if hint.get('kind') == 'voice':
            return await self._emit('voice', hint.get('text') or Path(name).stem, path=path, quote=False)
        title = Path(name).stem
        return await self._emit('song', title, path=path, quote=False, title=title, **({'artist': self.hub.name()} if self.hub.name() else {}))

    # -- gestures
    async def poke(self):
        return await self._emit('nudge', '拍了拍你', quote=False)

    async def like(self, times):
        return None   # a QQ-only gesture

    async def status(self, face_id, wording):
        if not isinstance(wording, str) or not 1 <= len(wording) <= 16:
            raise ValueError('APP_STATUS_INVALID')
        await self.hub.set_status({'text': wording, 'face': str(face_id), 'until': round(time.time() + 600, 1)})

    async def signature(self, text):
        if not isinstance(text, str) or not 1 <= len(text) <= 60 or '\n' in text:
            raise ValueError('APP_SIGNATURE_INVALID')
        await self.hub.set_signature(text)

    async def qzone_post(self, content, images=()):
        if not isinstance(content, str) or not 1 <= len(content) <= 1000 or len(images) > 3:
            raise ValueError('APP_MOMENT_INVALID')
        paths = [Path(p) for p in images]
        moment_id = uuid.uuid4().hex
        await self._emit('moment', content, path=paths[0] if paths else None, quote=False,
                         more_media=paths[1:], moment_id=moment_id)
        return moment_id

    async def cookies(self, domain):
        raise RuntimeError('APP_COOKIES_UNAVAILABLE')

    async def game(self, kind):
        if kind not in {'dice', 'rps'}:
            raise ValueError('APP_REPLY_INVALID')
        result = str(random.randint(1, 6)) if kind == 'dice' else str(random.randint(1, 3))
        text = f'🎲 {result}' if kind == 'dice' else {'1': '✋ 布', '2': '✌️ 剪刀', '3': '✊ 石头'}[result]
        await self._emit('game', text, quote=False, game=kind, result=result)
        return result

    async def resolve_media(self, kind, reference):
        """A file the phone sent, handed to the media reader as base64 (the cache holds what was fetched)."""
        import base64
        path = self.hub.store().media_path(str(reference)) if self.hub.server is not None else None
        if path is None or not path.is_file():
            raise ValueError('APP_MEDIA_UNAVAILABLE')
        return {'base64': base64.b64encode(await asyncio.to_thread(path.read_bytes)).decode()}

    # -- state
    def is_available(self):
        return self.hub.bound

    def start_typing(self):
        return self.hub.start_typing(self.event)

    def for_exchange(self, event):
        if (event.channel, event.account_id, event.owner_id) != (CHANNEL, ACCOUNT, OWNER):
            raise ValueError('APP_REPLY_OWNER_MISMATCH')
        return Sender(self.hub, event)

    async def _emit(self, kind, text, *, path=None, quote=True, more_media=(), **fields):
        item = {'id': uuid.uuid4().hex, 'from': 'her', 'ts': round(time.time(), 3), 'kind': kind, 'text': text,
                'media': None, 'source': self.source}
        if quote and self.reply_to:
            item['reply_to'] = self.reply_to
        item.update(fields)
        if path is not None:
            item['media'] = await self.hub.keep(Path(path))
        extra = [await self.hub.keep(Path(p)) for p in more_media]
        if extra:
            item['media_more'] = extra
        return await self.hub.send_item(item)


class Hub:
    """The desk-line side of the channel. One per process (`hub`); tests make their own."""

    def __init__(self):
        self.server = None
        self.line = None
        self.handler = None
        self.bound = False
        self.inq: asyncio.Queue = asyncio.Queue()
        self.acks: dict = {}
        self.flushing = asyncio.Lock()
        self.changed = asyncio.Event()
        self.status_value = None
        self.signature_value = None
        self._store = None
        self.sender = Sender(self)
        self.profile_sent = None
        self.generation = 0

    # -- plumbing
    def store(self) -> Store:
        root = self.server._state_root() if self.server is not None else None
        if root is None:
            raise RuntimeError('APP_STATE_UNAVAILABLE')
        if self._store is None:
            self._store = Store(Path(root) / 'personal-chat' / 'app')
            self._store.prune()
        return self._store

    def connected(self) -> bool:
        return self.line is not None and not self.line.ws.closed

    def name(self):
        return self.profile().get('name')

    def attach(self, line):
        self.line = line
        self.generation += 1
        self.profile_sent = None
        self.changed.set()
        server = getattr(line, 'server', None)
        wake = getattr(server, '_personal_chat_wake', None)
        if server is not None and callable(wake) and not self.bound:
            mark_active(server)
            line.spawn(wake())   # the chat service starts (or takes the new channel) when the first phone connects

    def detach(self, line):
        if self.line is line:
            self.line = None
            self.changed.set()

    async def on_connect(self):
        await self.push_profile()
        await self.flush()

    def frame(self, line, m):
        """A frame from the desk line. Inbound items are queued and handled one at a time, in order."""
        kind = m.get('type')
        if kind == 'app_in':
            self.inq.put_nowait(m)
        elif kind == 'app_out_ack':
            for item_id in m.get('ids') or ():
                future = self.acks.pop(item_id, None)
                if future is not None and not future.done():
                    future.set_result(True)
        elif kind == 'app_history':
            line.spawn(self.history(m))

    async def say(self, value):
        line = self.line
        if line is None or line.ws.closed:
            return False
        try:
            await line.send(value)
            return True
        except Exception:
            return False

    # -- inbound
    def _event(self, item):
        item_id, kind = item['id'], item.get('kind') or 'text'
        text = (item.get('text') or '').strip()
        images, input_kind = (), 'text'
        if kind == 'nudge':
            text = NUDGE_TEXT
        elif kind == 'face':
            from .qq_faces import FACE_NAMES
            name = item.get('face_name') or FACE_NAMES.get(str(item.get('face')))
            text = f'[表情:{name}]' if name else '[QQ表情]'
        elif kind == 'game':
            from .qq_games import RPS
            result = str(item.get('result', ''))
            text = (f'[骰子:{result}点]' if item.get('game') == 'dice' and result in '123456' and len(result) == 1 else
                    f'[猜拳:{RPS[result]}]' if item.get('game') == 'rps' and result in RPS else
                    '[骰子]' if item.get('game') == 'dice' else '[猜拳]')
        elif kind == 'voice':
            input_kind = 'voice'
            text = text or '[语音]'
        if item.get('media_id') and kind in ('image', 'text'):
            images = ((item_id, 'app-media:' + str(item['media_id'])),)
            input_kind = 'image'
            text = text or '[图片]'
        if not text:
            return None
        ts = item.get('ts')
        sent_at = None
        if type(ts) in (int, float) and ts > 0:
            try:
                sent_at = datetime.fromtimestamp(ts, timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError):
                pass
        return PersonalMessage(CHANNEL, ACCOUNT, OWNER, item_id, text[:10000], input_kind=input_kind,
                               sent_at=sent_at, images=images)

    async def receive(self, m):
        """One app_in: save it, answer app_in_ack, and only then let the reply start."""
        item, seq = m.get('item'), m.get('seq')
        if (not isinstance(item, dict) or type(seq) is not int or not isinstance(item.get('id'), str)
                or not 1 <= len(item['id']) <= 64):
            return None
        event = self._event(item)
        if event is not None:
            try:
                await self.handler.ingest(event)
            except ValueError:
                pass   # the same id with other words: the first copy stays, this one is only acknowledged
            record = {'id': item['id'], 'from': 'user', 'ts': item.get('ts') or time.time(),
                      'kind': item.get('kind') or 'text', 'text': (item.get('text') or '')[:4000], 'media': None}
            if item.get('media_id'):
                record['media'] = {'id': item['media_id'], **(self.store().meta.get(item['media_id']) or {})}
            if item.get('reply_to'):
                record['reply_to'] = item['reply_to']
            self.store().add(record)
        await self.say({'type': 'app_in_ack', 'seq': seq})
        if event is not None and event.images:
            await self.fetch_photo(event, item['media_id'])
        return event

    async def fetch_photo(self, event, media_id):
        """The phone's picture, placed where the picture reader looks for a QQ picture."""
        base, key = cloud()
        if base is None or self.server is None:
            return
        identity = hashlib.sha256((event.images[0][0] + '\0' + event.images[0][1]).encode()).hexdigest()
        target = Path(self.server._state_root()) / 'media' / ('qq-incoming-' + identity + '.img')
        if target.is_file():
            return
        for attempt in range(3):
            try:
                data = await self._http('GET', f'/v1/call/app/inbox-media/{media_id}', key=key, base=base)
                if data:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    mime = 'image/jpeg' if data[:3] == b'\xff\xd8\xff' else 'image/png' if data[:4] == b'\x89PNG' else 'image/webp'
                    self.store().keep_media(data, {'mime': mime})
                    return
            except Exception:
                log.warning('APP_PHOTO_FETCH_FAILED')
            await asyncio.sleep(1 + attempt)

    async def _http(self, method, path, *, key, base, data=None, mime=None):
        from aiohttp import ClientSession, ClientTimeout, TCPConnector
        from runtime.tls import client_tls_context
        headers = {'Authorization': 'Bearer ' + key}
        if mime:
            headers['Content-Type'] = mime
        async with ClientSession(timeout=ClientTimeout(total=120), trust_env=False,
                                 connector=TCPConnector(ssl=client_tls_context())) as session:
            async with session.request(method, base + path, headers=headers, data=data) as response:
                if response.status not in (200, 201, 204):   # 201: stored now, 200: the cloud had it already
                    raise RuntimeError(f'APP_HTTP_{response.status}')
                return await response.read() if method == 'GET' else None

    # -- outbound
    async def keep(self, path: Path) -> dict:
        def read():
            data = path.read_bytes()
            if not data or len(data) > MEDIA_MAX_BYTES:
                raise ValueError('APP_MEDIA_SIZE_INVALID')
            mime = _mime(path)
            return data, _describe(data, mime, path)
        data, meta = await asyncio.to_thread(read)
        media_id = await asyncio.to_thread(self.store().keep_media, data, meta)
        return {'id': media_id, **meta}

    async def send_item(self, item: dict) -> str:
        """Store the item (log and outbox), then try to hand it to the cloud at once. Returns its id."""
        store = self.store()
        store.add(item)
        store.queue(item)
        await self.flush()
        return item['id']

    async def flush(self):
        async with self.flushing:
            for item in self.store().waiting():
                if not self.connected():
                    return
                try:
                    media = [item['media']] if isinstance(item.get('media'), dict) else []
                    media += [m for m in item.get('media_more') or () if isinstance(m, dict)]
                    for entry in media:
                        await self.upload(entry)
                    future = asyncio.get_running_loop().create_future()
                    self.acks[item['id']] = future
                    wire = {k: v for k, v in item.items() if k != 'media_more'}
                    if item.get('media_more'):
                        wire['media_more'] = item['media_more']
                    if not await self.say({'type': 'app_out', 'items': [wire]}):
                        self.acks.pop(item['id'], None)
                        return
                    await asyncio.wait_for(future, ACK_TIMEOUT)
                    self.store().done(item['id'])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.acks.pop(item['id'], None)
                    log.warning('APP_OUT_UNCONFIRMED')
                    return
            self.store().prune()

    async def upload(self, entry: dict):
        base, key = cloud()
        path = self.store().media_path(entry['id'])
        if base is None or not path.is_file():
            raise RuntimeError('APP_MEDIA_UNAVAILABLE')
        await self._http('PUT', f'/v1/call/app/media/{entry["id"]}', key=key, base=base,
                         data=await asyncio.to_thread(path.read_bytes), mime=entry.get('mime') or 'application/octet-stream')

    # -- typing and read state
    def start_typing(self, event):
        if os.environ.get('OLIVIA_APP_TYPING_ENABLED', '1') == '0':
            return lambda: None
        task = asyncio.get_running_loop().create_task(self._typing(event))

        def stop():
            task.cancel()
        return stop

    async def _typing(self, event):
        try:
            if event is not None:
                for source_id, _ in event.sources:
                    await self.say({'type': 'app_delivered', 'id': source_id, 'state': 'read'})
            while True:
                await self.say({'type': 'app_typing', 'on': True})
                await asyncio.sleep(TYPING_REFRESH)
        finally:
            if self.line is not None and not self.line.ws.closed:
                try:
                    await asyncio.shield(self.say({'type': 'app_typing', 'on': False}))
                except Exception:
                    pass

    # -- profile and status
    def profile(self) -> dict:
        from runtime import ui_language
        value = {'name': None, 'persona_line': None, 'avatar_media': None, 'language': 'en' if ui_language.is_english() else 'zh',
                 'timezone': 'Asia/Shanghai'}
        try:
            from persona_loader import load_persona
            snapshot = load_persona(self.server.letters_adapter.persona_v2_path).snapshot
            if snapshot.profile is not None:
                value['name'] = snapshot.profile.display_name
                value['persona_line'] = (self.signature_value or snapshot.profile.summary or '')[:80] or None
        except Exception:
            value['persona_line'] = self.signature_value
        if self.signature_value:
            value['persona_line'] = self.signature_value
        value['status'] = self.status_value
        return value

    async def push_profile(self, force=False):
        value = await asyncio.to_thread(self.profile)
        fingerprint = json.dumps(value, sort_keys=True, ensure_ascii=False)
        if not force and fingerprint == self.profile_sent:
            return
        if await self.say({'type': 'app_profile', 'profile': value}):
            self.profile_sent = fingerprint

    async def set_status(self, status):
        self.status_value = status
        await self.push_profile()

    async def set_signature(self, text):
        self.signature_value = text
        await self.push_profile()

    # -- earlier messages for the phone
    def history_items(self, limit, before) -> list:
        store = self.store()
        items = store.recent(limit, before)
        if items:
            return items
        return rows_history(getattr(self.server.store, 'personal_chats', []), limit, before)

    async def history(self, m):
        try:
            limit = max(1, min(int(m.get('limit') or HISTORY_LIMIT), HISTORY_LIMIT))
        except (TypeError, ValueError):
            limit = HISTORY_LIMIT
        before = m.get('before') if type(m.get('before')) in (int, float) else None
        items = [dict(i) for i in self.history_items(limit, before)]
        uploaded = 0
        for item in items:
            media = item.get('media')
            if not isinstance(media, dict):
                continue
            if uploaded >= HISTORY_MEDIA or not self.store().media_path(media['id']).is_file():
                item['media'] = None   # kept only as its words
                continue
            try:
                await self.upload(media)
                uploaded += 1
            except Exception:
                item['media'] = None
        await self.say({'type': 'app_history', 'req': m.get('req'), 'items': items})


def rows_history(rows, limit, before) -> list:
    """The chat as the personal-chat rows hold it (used when the app log is empty): his messages and her delivered replies."""
    items = []
    for row in rows:
        if row.get('channel') != CHANNEL or row.get('superseded_by'):
            continue
        base = float(row.get('created_at') or 0)
        for index, (key, text) in enumerate((row.get('source_messages') or {}).items()):
            items.append({'id': str(key), 'from': 'user', 'ts': base + index * 0.001, 'kind': 'text', 'text': text, 'media': None})
        if row.get('delivery_status') == 'DELIVERED' and row.get('reply_text'):
            when = row.get('private_world_occurred_at')
            try:
                ts = datetime.fromisoformat(when).timestamp() if when else base + 1
            except ValueError:
                ts = base + 1
            kind = 'voice' if row.get('delivered_format') == 'audio' else 'text'
            items.append({'id': 'r-' + str(row.get('letter_id')), 'from': 'her', 'ts': ts, 'kind': kind,
                          'text': row['reply_text'], 'media': None, 'source': 'proactive' if row.get('origin') == 'proactive' else 'reply'})
    items.sort(key=lambda i: i['ts'])
    return [i for i in items if before is None or i['ts'] < before][-limit:]


hub = Hub()


async def run_app(handle_message, stop_event, state_callback, *, server, hub=None):
    """Run until stopped: the job `install_personal_chat` starts for the channel.

    handle_message is the exchange runner; its `ready` is told when the line is up so she may start contact,
    exactly like a QQ connection that has just come up."""
    hub = hub or globals()['hub']
    hub.server, hub.handler, hub.bound = server, handle_message, True
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    scheduled = set()

    def keys(event):
        return {(event.binding_id, key) for key, _ in event.sources}

    def schedule(event):
        found = keys(event)
        if found & scheduled or queue.full():
            return
        queue.put_nowait(event)
        scheduled.update(found)

    def refill():
        pending = getattr(handle_message, 'pending', None)
        for event in (pending(CHANNEL) if callable(pending) else ()):
            schedule(event)

    async def intake():
        while True:
            m = await hub.inq.get()
            try:
                await hub.receive(m)
                refill()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning('APP_IN_FAILED')   # not acknowledged: the cloud hands it over again on the next connect

    async def worker():
        carry = None
        while True:
            event = carry or await queue.get()
            carry = None
            events = [event]
            try:
                deadline = asyncio.get_running_loop().time() + 8
                while len(events) < 16:
                    wait = min(MERGE_SECONDS, deadline - asyncio.get_running_loop().time())
                    if wait <= 0:
                        break
                    try:
                        following = await asyncio.wait_for(queue.get(), wait)
                    except asyncio.TimeoutError:
                        break
                    if (sum(len(e.text) + 1 for e in events) + len(following.text) > 10000
                            or sum(len(e.images) for e in events) + len(following.images) > 4
                            or not mergeable_by_sent_time(events[-1], following, MERGE_SECONDS)):
                        carry = following
                        break
                    events.append(following)
                await handle_message(combine(events), hub.sender)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning('APP_MESSAGE_HANDLER_FAILED')   # the exchange records its own failure on the stored row
            finally:
                for _ in events:
                    queue.task_done()
                for done in events:
                    scheduled.difference_update(keys(done))
                refill()

    tasks = [asyncio.create_task(intake()), asyncio.create_task(worker())]
    announced = None
    try:
        hub.changed.set()
        while not stop_event.is_set():
            up = (hub.connected(), hub.generation)
            if up != announced:
                announced = up
                state_callback('CONNECTED' if up[0] else 'RECONNECTING')
                if up[0]:
                    ready = getattr(handle_message, 'ready', None)
                    if callable(ready):
                        ready(CHANNEL, hub.sender)
                    hub.line.spawn(hub.on_connect())
                    refill()
            hub.changed.clear()
            waiter = asyncio.ensure_future(hub.changed.wait())
            stopper = asyncio.ensure_future(stop_event.wait())
            done, rest = await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED, timeout=30)
            for task in rest:
                task.cancel()
    finally:
        hub.bound = False
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
