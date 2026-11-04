"""Songs that arrive in a QQ turn become cover sources: a shared card, link or B站 video is fetched through the relay
(``runtime/media/song_resolver``), an audio file sent in the chat is received from QQ. Either way the audio is
normalised by ``cover_upload.validate_upload`` into ``cover-inputs/<id>/source.wav`` and indexed in
``runtime/media/song_sources``. What happened is written on the turn's row for the writer (``cover_chat.context``):
``song_arrivals`` (kept songs) and ``song_failures`` (what could not be fetched, with the reason and its code).
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import hashlib
import logging
import os
from pathlib import Path
import shutil

from runtime.media import song_resolver
from runtime.media.song_resolver import SongError
from runtime.media.song_sources import SongSources
from runtime.ui_i18n_backend import tr
from . import song_shares

log = logging.getLogger(__name__)
AUDIO_SUFFIXES = frozenset({'.mp3', '.wav', '.flac', '.m4a', '.aac', '.ogg'})
MAX_FILE_BYTES = 40 * 1024 * 1024
LONGEST_SECONDS = 600
# The text sender of the turn being answered (service._generate): her one short line before a slow fetch.
QUICK_SEND = ContextVar('personal_chat_quick_send', default=None)


def _store(server):
    root = getattr(server, '_local_data_root', lambda: None)()
    return SongSources(root) if root is not None else None


def _persist(server):
    try:
        server._persist_store_state()
    except Exception:  # noqa: BLE001 - the row is saved again with the reply
        pass


def _failure(row, what, code):
    row.setdefault('song_failures', []).append({'what': what[:80], 'code': code, 'reason': song_resolver.reason(code)})


async def _keep(store, folder, *, key, title, singer, origin, identity, now=None):
    """The audio at ``folder/upload.bin`` normalised into the cover source and indexed. Raises SongError."""
    from runtime.media.cover_upload import validate_upload
    raw, audio = folder / 'upload.bin', folder / 'source.wav'
    try:
        seconds = await asyncio.to_thread(validate_upload, raw, audio, os.environ)
    except ValueError as exc:
        raise SongError('SONG_FFMPEG_UNAVAILABLE' if str(exc) == 'COVER_FFMPEG_UNAVAILABLE' else 'SONG_AUDIO_INVALID') from None
    if seconds > LONGEST_SECONDS:
        raise SongError('SONG_TOO_LONG')
    return store.add(folder.name, key=key, title=title, singer=singer, origin=origin, identity=identity,
                     seconds=seconds, now=now)


def _arrival(song) -> dict:
    return {'source': song['id'], 'title': song.get('title', ''), 'singer': song.get('singer', ''),
            'seconds': int(song.get('seconds') or 0)}


async def say_first(row) -> None:
    """One short line sent before a slow fetch, so the user is not left in silence (once per turn)."""
    send = QUICK_SEND.get()
    if not callable(send) or row.get('song_listening_line') is not None:
        return
    row['song_listening_line'] = ''
    lines = (tr('我听一下'), tr('我先找找这首歌'), tr('等我听听看'))
    line = lines[int(hashlib.sha256(str(row.get('letter_id', '')).encode()).hexdigest(), 16) % len(lines)]
    try:
        await send(line)
        row['song_listening_line'] = line
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the reply follows as usual
        log.info('SONG_FIRST_LINE_FAILED')


async def _short_bvid(share) -> str:
    import aiohttp
    from . import bilibili
    async with aiohttp.ClientSession(headers={'User-Agent': bilibili.UA}, trust_env=False,
                                     timeout=aiohttp.ClientTimeout(total=bilibili.TIMEOUT_SECONDS)) as http:
        return await bilibili._bvid(http, share.short_link)


async def receive_shares(server, event, row) -> None:
    """The song this message shares, kept as a cover source (once per turn)."""
    if row.get('song_share_read'):
        return
    row['song_share_read'] = True
    store = _store(server)
    share = song_shares.find(getattr(event, 'text', ''))
    url, token = os.environ.get('OLIVIA_GPU_API_URL', ''), os.environ.get('OLIVIA_GPU_API_KEY', '')
    if share is None or store is None or not url or not token:
        return
    what = (share.title or share.app or '分享的歌')
    try:
        store.cleanup()
        if share.short_link:
            try:
                share = song_shares.with_bvid(share, await _short_bvid(share))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                raise SongError('SONG_LINK_UNREADABLE') from None
        kept = store.find_key(share.key)
        if kept is None:
            await say_first(row)
            folder = store.folder(store.new_id())
            folder.mkdir(parents=True, exist_ok=False)
            try:
                details = await song_resolver.resolve(share.request(), folder / 'upload.bin', url, token)
                kept = await _keep(store, folder, key=share.key, title=details['title'] or share.title,
                                   singer=details['singer'] or share.singer, origin=details['source'],
                                   identity=details['identity'])
            except BaseException:
                shutil.rmtree(folder, ignore_errors=True)
                raise
        row.setdefault('song_arrivals', []).append(_arrival(kept))
    except asyncio.CancelledError:
        raise
    except SongError as exc:
        _failure(row, what, exc.code)
        log.info('SONG_NOT_KEPT %s', exc.code)
    except Exception as exc:  # noqa: BLE001 - the turn goes on without the song
        _failure(row, what, 'SONG_FETCH_FAILED')
        log.info('SONG_NOT_KEPT %s', type(exc).__name__)
    _persist(server)


async def receive_audio_file(server, event, row, identifier, reference, name) -> bool:
    """An audio file sent in the chat, kept as a cover source. False: not an audio file (or covers are off) and the
    ordinary media path decides."""
    from . import cover_chat
    if event.channel != 'qq' or Path(str(name)).suffix.lower() not in AUDIO_SUFFIXES:
        return False
    if not await cover_chat.available(os.environ):
        return False
    store = _store(server)
    if store is None:
        return False
    seen = row.setdefault('song_files', [])
    identity = hashlib.sha256(f'{identifier}:{reference}'.encode()).hexdigest()
    if identity in seen:
        return True
    seen.append(identity)
    title = Path(str(name).replace('\\', '/')).stem.strip()[:60] or tr('发来的歌')
    from runtime.incoming_media import acquire, MEDIA_RESOLVER
    folder = None
    try:
        store.cleanup()
        resolver = MEDIA_RESOLVER.get()
        if not callable(resolver):
            raise SongError('SONG_FILE_UNAVAILABLE')
        folder = store.folder(store.new_id())
        folder.mkdir(parents=True, exist_ok=False)
        try:
            await acquire(await resolver('file', reference), folder / 'upload.bin', max_bytes=MAX_FILE_BYTES)
        except ValueError as exc:
            raise SongError('SONG_FILE_TOO_LARGE' if str(exc) == 'MEDIA_INPUT_TOO_LARGE' else 'SONG_FILE_UNAVAILABLE') from None
        except (RuntimeError, OSError):
            raise SongError('SONG_FILE_UNAVAILABLE') from None
        digest = hashlib.sha256((folder / 'upload.bin').read_bytes()).hexdigest()
        kept = store.find_key('file:' + digest)
        if kept is not None:
            shutil.rmtree(folder, ignore_errors=True)
        else:
            kept = await _keep(store, folder, key='file:' + digest, title=title, singer='', origin='file', identity=digest)
        row.setdefault('song_arrivals', []).append(_arrival(kept))
    except asyncio.CancelledError:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        raise
    except SongError as exc:
        _failure(row, title, exc.code)
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        _failure(row, title, 'SONG_FILE_UNAVAILABLE')
        log.info('SONG_FILE_NOT_KEPT %s', type(exc).__name__)
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
    _persist(server)
    return True
