"""The cover ordered from a QQ chat: order it, send the result as an mp3 file, add it to the music library.

States of ``row['cover_status']``: PENDING (asked for, checks not run) -> MAKING (checked; ordered or about to be) ->
SENDING (the file is being handed to QQ) -> DELIVERED, or FAILED / UNKNOWN. A restart never orders again: the order's
receipt (runtime/remote_pipeline) brings back the task already made, and a finished result file is used as it is.
SENDING and UNKNOWN are never sent again. After DELIVERED the short closing line, the library entry and the memory
record are finished once each (``cover_finished``).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os
from pathlib import Path

from runtime.ui_i18n_backend import tr
from . import cover_chat
from .cover_chat import CoverFailure

log = logging.getLogger(__name__)


def work_folder(root, row) -> Path:
    return Path(root) / 'media' / 'chat-covers' / row['letter_id']


def to_mp3(wav: Path, mp3: Path) -> None:
    """The finished cover as an mp3 (the only kind of file QQ accepts here), written whole or not at all."""
    from runtime.media.latentsync_reply import resolve_ffmpeg_executable
    from runtime.media.managed_subprocess import run_managed_process
    staging = mp3.with_name(mp3.stem + '.part.mp3')
    try:
        result = run_managed_process([str(resolve_ffmpeg_executable(os.environ)), '-nostdin', '-v', 'error', '-y',
                                      '-i', str(wav), '-codec:a', 'libmp3lame', '-b:a', '192k', str(staging)],
                                     timeout_seconds=600)
        if result.returncode or not staging.is_file() or staging.stat().st_size < 1024:
            raise CoverFailure('COVER_ENCODE_FAILED')
        staging.replace(mp3)
    except OSError:
        raise CoverFailure('COVER_ENCODE_FAILED') from None
    finally:
        staging.unlink(missing_ok=True)


def _order(source: Path, output: Path, style: str = '') -> None:
    """The cover pipeline with her own singing voice; waits for the account's lane instead of failing at once."""
    from runtime import remote_pipeline
    from runtime.media import cover_options
    from runtime.remote_generation import LANE_WAIT, LaneWait
    options = {'engine': 'yue2'}
    if style:
        options['arrangement'] = style
    token = LANE_WAIT.set(LaneWait(cover_chat.LANE_WAIT_SECONDS))
    try:
        remote_pipeline.generate('cover', {'lyrics': '', 'language': 'unknown',
                                           'cover_options': cover_options.validate(options)},
                                 output, environment=os.environ, assets={'source_asset': source})
    finally:
        LANE_WAIT.reset(token)


async def deliver(server, row, send):
    from .backend import persist_chat
    from .speech import filename
    state = row.get('cover_status') or 'PENDING'
    if state in cover_chat.FINAL_STATUSES:
        return
    if state == 'DELIVERED':
        await finish(server, row, send)
        return
    from runtime import remote_pipeline
    from runtime.media.cover_upload import source_path
    from runtime.media.song_sources import SongSources
    root = server._local_data_root()
    request = row.get('cover_request')
    if isinstance(request, dict) and request.get('task_id'):
        await _deliver_task(server, row, send, root, request)
        return
    if root is None or not isinstance(request, dict) or not isinstance(request.get('source'), str):
        raise CoverFailure('COVER_SOURCE_REQUIRED')
    store = SongSources(root)
    song = store.get(request['source'])
    if song is None:
        raise CoverFailure('COVER_SOURCE_REQUIRED')
    title = (request.get('title') or song.get('title') or tr('发来的歌')).strip()[:80]
    if state == 'PENDING':
        if not remote_pipeline.enabled(os.environ):
            raise CoverFailure('COVER_RUNTIME_UNAVAILABLE', tr('这台电脑现在没有连上 Olivia 云服务'))
        facts = await cover_chat.preflight_facts(os.environ)
        active = getattr(server, '_active_undelivered_letter', None)
        ok, code, words = cover_chat.cover_preflight(
            **facts, letter_active=bool(active() if callable(active) else False),
            cover_active=cover_chat.other_cover_active(server, row))
        if not ok:
            raise CoverFailure(code, words)
        row.update(cover_status='MAKING', cover_title=title)
        await persist_chat(server)
    folder = work_folder(root, row)
    folder.mkdir(parents=True, exist_ok=True)
    wav, mp3 = folder / 'cover.wav', folder / 'cover.mp3'
    if not mp3.is_file():
        if not wav.is_file():       # a result already fetched before a restart is used, never ordered again
            try:
                source = source_path(root, song['id'])
            except ValueError:
                raise CoverFailure('COVER_SOURCE_REQUIRED') from None
            style = request.get('style')
            style = style.strip() if isinstance(style, str) else ''
            await asyncio.to_thread(*((_order, source, wav, style) if style else (_order, source, wav)))
        await asyncio.to_thread(to_mp3, wav, mp3)
    await _send(server, row, send, mp3, title)


async def _deliver_task(server, row, send, root, request):
    """A finished song the cloud named in a contact (its task, made for this account): fetched, never ordered again,
    then sent and filed exactly like a cover she sang in the chat. Her own words already told him, so no closing line."""
    from .backend import persist_chat
    if root is None:
        raise CoverFailure('COVER_SOURCE_REQUIRED')
    title = (request.get('title') or tr('发来的歌')).strip()[:80]
    url, key = os.environ.get('OLIVIA_GPU_API_URL', ''), os.environ.get('OLIVIA_GPU_API_KEY', '')
    if not url or not key:
        raise CoverFailure('COVER_RUNTIME_UNAVAILABLE', tr('这台电脑现在没有连上 Olivia 云服务'))
    if row.get('cover_status') in (None, 'PENDING'):
        row.update(cover_status='MAKING', cover_title=title)
        await persist_chat(server)
    folder = work_folder(root, row)
    folder.mkdir(parents=True, exist_ok=True)
    wav, mp3 = folder / 'cover.wav', folder / 'cover.mp3'
    if not mp3.is_file():
        if not wav.is_file():       # a result already fetched before a restart is used as it is
            from runtime.cloud_service import CloudError
            from runtime.remote_generation import RemoteGeneration
            try:
                await RemoteGeneration(url, key).download_task(request['task_id'], wav)
            except (CloudError, OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                raise CoverFailure(cover_chat.error_code(exc)) from None
        await asyncio.to_thread(to_mp3, wav, mp3)
    row['cover_task_only'] = True
    await _send(server, row, send, mp3, title)


async def _send(server, row, send, mp3, title):
    from .backend import persist_chat
    from .speech import filename
    row.update(cover_file=str(mp3), cover_status='SENDING')
    await persist_chat(server)
    try:
        receipt = await send.file(mp3, filename(tr('{title}（翻唱）', title=title)))
        if not receipt:
            raise ValueError('QQ_FILE_ACK_INVALID')
    except asyncio.CancelledError:
        row['cover_status'] = 'UNKNOWN'
        await persist_chat(server)
        raise
    except Exception as exc:
        from .qq import QQFileRejected
        rejected = isinstance(exc, QQFileRejected)
        row['cover_status'] = 'FAILED' if rejected else 'UNKNOWN'
        await persist_chat(server)
        raise CoverFailure('QQ_FILE_REJECTED' if rejected else 'QQ_FILE_UNCONFIRMED') from None
    row.update(cover_status='DELIVERED', cover_delivery_receipt=str(receipt),
               cover_completed_at=datetime.now(timezone.utc).isoformat())
    await persist_chat(server)
    await finish(server, row, send)


async def finish(server, row, send):
    """After the file went out: her closing line once, the library entry, the source marked used, the work files gone."""
    from .backend import persist_chat
    from .service import delivery_notice
    root = server._local_data_root()
    title = row.get('cover_title') or tr('发来的歌')
    if not row.get('cover_task_only'):
        await delivery_notice(row, send, lambda: persist_chat(server), 'cover_notice_status',
                              cover_chat.completion_text(title))
    folder = work_folder(root, row)
    wav = folder / 'cover.wav'
    if not row.get('cover_library') and wav.is_file():
        row['cover_library'] = 'DONE'
        try:
            from runtime.media.local_song_library import LocalSongLibrary
            await asyncio.to_thread(LocalSongLibrary(root, os.environ).import_audio, wav,
                                    tr('{title}（翻唱）', title=title)[:120])
        except Exception as exc:  # noqa: BLE001 - the file is sent; the library entry is a convenience
            row['cover_library'] = 'FAILED'
            log.info('COVER_LIBRARY_IMPORT_FAILED %s', type(exc).__name__)
        await persist_chat(server)
    if not row.get('cover_finished'):
        from runtime.media.song_sources import SongSources
        request = row.get('cover_request') or {}
        if not row.get('cover_task_only'):
            SongSources(root).mark_used(str(request.get('source', '')))
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
        row['cover_finished'] = True
        await persist_chat(server)
