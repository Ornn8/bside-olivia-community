"""Songs that arrive in a QQ turn become cover sources: a shared card, link or B站 video is fetched through the relay
(``runtime/media/song_resolver``), an audio file sent in the chat is received from QQ. Either way the audio is
normalised by ``cover_upload.validate_upload`` into ``cover-inputs/<id>/source.wav`` and indexed in
``runtime/media/song_sources``. What happened is written on the turn's row for the writer (``cover_chat.context``):
``song_arrivals`` (kept songs) and ``song_failures`` (what could not be fetched, with the reason and its code).

A voice message the user sends (himself singing or humming) is kept the same way by ``keep_voice``, in its own index
(``song_sources.VoiceSources``), and noted on the row as ``voice_arrivals``; she may sing it back.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import time
import wave

from runtime.media import song_resolver
from runtime.media.song_resolver import SongError
from runtime.media.song_sources import SongSources, VoiceSources
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


PENDING_FILE = 'pending-shares.json'
PENDING_LIMIT = 3
PENDING_HOURS = 24


def _pending_path(server):
    root = getattr(server, '_local_data_root', lambda: None)()
    return None if root is None else Path(root) / 'cover-inputs' / PENDING_FILE


def _pending_read(server, now=None) -> list:
    path = _pending_path(server)
    try:
        items = json.loads(path.read_text(encoding='utf-8')) if path is not None and path.exists() else []
    except (OSError, ValueError):
        return []
    now = time.time() if now is None else now
    return [item for item in items if isinstance(item, dict) and isinstance(item.get('text'), str)
            and isinstance(item.get('key'), str) and now - float(item.get('at') or 0) < PENDING_HOURS * 3600][-PENDING_LIMIT:]


def song_waiting(server, event) -> bool:
    """This message shares a song, or one shared earlier is still waiting to be fetched."""
    return song_shares.find(getattr(event, 'text', '')) is not None or bool(_pending_read(server))


def _pending_write(server, items) -> None:
    path = _pending_path(server)
    if path is None:
        return
    try:
        if items:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(items[-PENDING_LIMIT:], ensure_ascii=False), encoding='utf-8')
        elif path.exists():
            path.unlink()
    except OSError:
        log.info('SONG_PENDING_WRITE_FAILED')


def defer_share(server, event, row, reason) -> None:
    """Covers cannot be offered this turn (``reason`` is a cover_chat.SKIP_WORDS code). A song this message shares is
    kept to be fetched on the next turn that can, and the writer is told plainly why it was not (with the code)."""
    row['cover_skip'] = reason
    share = song_shares.find(getattr(event, 'text', ''))
    if share is None:
        return
    log.info('COVER_OFFER_SKIPPED %s', reason)
    from . import cover_chat
    what = (share.title or share.app or '分享的歌')[:80]
    if not any(item.get('what') == what and item.get('code') == reason for item in row.get('song_failures') or []):
        words = cover_chat.SKIP_WORDS.get(reason, '翻唱现在用不了')
        row.setdefault('song_failures', []).append({
            'what': what, 'code': reason, 'reason': f'{words}（错误码 {reason}）；这首歌先记着，等能用了再唱'})
    items = [item for item in _pending_read(server) if item['key'] != share.key]
    items.append({'key': share.key, 'text': str(event.text)[:1200], 'at': time.time()})
    _pending_write(server, items)
    _persist(server)


async def _take(server, row, text, *, arrive, first_line):
    """Keep the song ``text`` shares as a cover source. Raises SongError; True when a song was kept."""
    store = _store(server)
    share = song_shares.find(text)
    url, token = os.environ.get('OLIVIA_GPU_API_URL', ''), os.environ.get('OLIVIA_GPU_API_KEY', '')
    if share is None or store is None or not url or not token:
        return False
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
        if first_line:
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
    if arrive:
        row.setdefault('song_arrivals', []).append(_arrival(kept))
    return True


async def receive_shares(server, event, row) -> None:
    """The song this message shares, kept as a cover source (once per turn). Songs shared earlier while covers were
    unavailable (``defer_share``) are fetched too; they are offered as earlier songs."""
    if row.get('song_share_read'):
        return
    row['song_share_read'] = True
    text = getattr(event, 'text', '')
    share = song_shares.find(text)
    for item in _pending_read(server):
        if share is not None and item['key'] == share.key:
            continue  # this very song is taken below
        try:
            await _take(server, row, item['text'], arrive=False, first_line=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one late song never blocks this turn
            log.info('SONG_PENDING_NOT_KEPT %s', getattr(exc, 'code', type(exc).__name__))
    _pending_write(server, [])
    if share is None or _store(server) is None:
        return
    what = (share.title or share.app or '分享的歌')
    try:
        await _take(server, row, text, arrive=True, first_line=True)
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


def _wav_seconds(path) -> float:
    with wave.open(str(path), 'rb') as audio:
        return audio.getnframes() / audio.getframerate()


async def keep_voice(server, event, row, recording, identity, heard) -> None:
    """A voice message the user sent in the chat, kept (the audio and what was heard in it) so that she may choose to
    sing it back. ``recording`` is the WAV QQ handed over, ``identity`` the digest of that message (one message is kept
    once), ``heard`` the text of its observation. Never raises: a voice that cannot be kept is simply not offered.
    The turn's row gets ``voice_arrivals`` (the kept voices of this message), like ``song_arrivals`` for songs."""
    from . import cover_chat
    if (event.channel != 'qq' or not cover_chat.ENABLED or row.get('origin') == 'proactive'
            or row.get('contact_id') is not None):
        return
    folder = None
    try:
        root = getattr(server, '_local_data_root', lambda: None)()
        if root is None:
            return
        store = VoiceSources(root)
        store.cleanup()
        kept = store.find_key(identity)
        if kept is None:
            seconds = await asyncio.to_thread(_wav_seconds, recording)
            if not 0 < seconds <= cover_chat.VOICE_MAX_SECONDS:
                return
            folder = store.folder(store.new_id())
            folder.mkdir(parents=True, exist_ok=False)
            await asyncio.to_thread(shutil.copyfile, recording, folder / 'source.part')
            os.replace(folder / 'source.part', folder / 'source.wav')
            text = str(heard or '').strip()
            kept = store.add(folder.name, key=identity, title='', singer='', origin='voice', identity=identity,
                             seconds=seconds, heard=text[:cover_chat.HEARD_LIMIT], words=cover_chat.has_words(text))
            folder = None
            store.cleanup()   # keeps the newest few: the one just added is never the one that goes
        arrivals = row.setdefault('voice_arrivals', [])
        if not any(item.get('source') == kept['id'] for item in arrivals):
            arrivals.append({'source': kept['id'], 'seconds': float(kept.get('seconds') or 0)})
            _persist(server)
    except asyncio.CancelledError:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001 - the turn goes on; this voice is just not offered
        log.info('VOICE_NOT_KEPT %s', type(exc).__name__)
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
