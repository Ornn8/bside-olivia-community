"""Managed local performance imports. Import/catalog approach credited to 芙桃."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import subprocess
import time
import shutil
import wave

from runtime.media.latentsync_reply import resolve_ffmpeg_executable
from runtime.media.managed_subprocess import run_managed_process

_LOCK = threading.RLock()
_IMPORT_LOCK = threading.Lock()
_ID = re.compile(r'[0-9a-f]{64}')
_VIDEO = {'.mp4', '.mov', '.mkv', '.webm', '.m4v', '.avi', '.flv', '.wmv'}


class LocalSongError(RuntimeError):
    pass


class LocalSongLibrary:
    def __init__(self, data_root: Path, environment):
        self.root = Path(data_root).resolve() / 'local-songs'
        self.environment = dict(environment)

    def _read(self):
        path = self.root / 'catalog.json'
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            rows = data['songs']
            if data['schema_version'] != 1 or not isinstance(rows, list):
                raise ValueError()
            if any(not isinstance(x, dict) or not _ID.fullmatch(str(x.get('id', '')))
                   or not isinstance(x.get('name'), str) for x in rows):
                raise ValueError()
            return rows
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LocalSongError('LOCAL_SONG_CATALOG_INVALID') from exc

    def _write(self, rows):
        self.root.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.root, suffix='.json.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'schema_version': 1, 'songs': rows}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.root / 'catalog.json')
        finally:
            Path(name).unlink(missing_ok=True)

    def songs(self):
        with _LOCK:
            rows = self._read()
            changed = False
            for row in rows:
                if not row.get('native_id'):
                    row['native_id'] = self._native_id(row['id'], rows)
                    changed = True
            if changed:
                self._write(rows)
            return rows

    def media_path(self, song_id):
        if not isinstance(song_id, str) or not _ID.fullmatch(song_id):
            raise LocalSongError('LOCAL_SONG_NOT_FOUND')
        row = next((x for x in self._read() if x['id'] == song_id), {})
        path = self.root / (song_id + ('.wav' if row.get('media_type') == 'audio' else '.mp4'))
        if path.is_symlink() or path.resolve().parent != self.root or not path.is_file():
            raise LocalSongError('LOCAL_SONG_NOT_FOUND')
        return path

    def rename(self, song_id, name):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            raise LocalSongError('LOCAL_SONG_NAME_INVALID')
        with _LOCK:
            rows = self._read()
            row = next((x for x in rows if x['id'] == song_id), None)
            if row is None:
                raise LocalSongError('LOCAL_SONG_NOT_FOUND')
            row['name'] = name.strip()
            self._write(rows)

    def reveal(self, song_id):
        with _LOCK:
            if not any(row['id'] == song_id for row in self._read()):
                raise LocalSongError('LOCAL_SONG_NOT_FOUND')
            path = self.media_path(song_id)
        if os.name != 'nt':
            raise LocalSongError('LOCAL_SONG_REVEAL_UNSUPPORTED')
        try:
            explorer = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'explorer.exe'
            subprocess.Popen([str(explorer), '/select,', str(path)], shell=False)
        except OSError:
            raise LocalSongError('LOCAL_SONG_REVEAL_FAILED') from None

    def delete(self, song_id):
        with _LOCK:
            rows = self._read()
            if not any(x['id'] == song_id for x in rows):
                raise LocalSongError('LOCAL_SONG_NOT_FOUND')
            # Resolve the owned file before changing the catalog; never delete sources.
            path = self.media_path(song_id)
            retired = next((x.get('native_id') for x in rows if x['id'] == song_id), None)
            if retired:
                self._retire(song_id, retired)
            self._write([x for x in rows if x['id'] != song_id])
            try:
                path.unlink()
            except OSError:
                self._write(rows)
                raise LocalSongError('LOCAL_SONG_FILE_IN_USE') from None

    def _retired(self):
        """Return content-id -> native-id pairs whose song was deleted."""
        path = self.root / 'native-ids.json'
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            rows = data['retired']
            if data['schema_version'] != 1 or not isinstance(rows, dict):
                raise ValueError()
            if any(not _ID.fullmatch(key) or not isinstance(value, str) or not value.isascii()
                   or not value.isdecimal() or not NATIVE_SONG_ID_BASE <= int(value) < NATIVE_SONG_ID_BASE + NATIVE_SONG_ID_SPAN
                   for key, value in rows.items()) or len(set(rows.values())) != len(rows):
                raise ValueError()
            return rows
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LocalSongError('LOCAL_SONG_ID_LEDGER_INVALID') from exc

    def _native_id(self, song_id, rows):
        retired = self._retired()
        taken = {row['native_id'] for row in rows if row.get('native_id') and row['id'] != song_id}
        owned = retired.get(song_id)
        if owned is not None:
            if owned in taken:
                raise LocalSongError('LOCAL_SONG_NATIVE_ID_CONFLICT')
            return owned
        return _assign_native_id(song_id, taken | set(retired.values()))

    def _retire(self, song_id, native_id):
        """Keep a deleted song's native id owned by its content for good."""
        retired = self._retired()
        if song_id in retired and retired[song_id] != native_id:
            raise LocalSongError('LOCAL_SONG_NATIVE_ID_CONFLICT')
        retired[str(song_id)] = str(native_id)
        fd, name = tempfile.mkstemp(dir=self.root, suffix='.json.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'schema_version': 1, 'retired': retired}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.root / 'native-ids.json')
        finally:
            Path(name).unlink(missing_ok=True)

    def _prepare(self, source, output):
        try:
            ffmpeg = resolve_ffmpeg_executable(self.environment)
        except RuntimeError as exc:
            raise LocalSongError('LOCAL_SONG_FFMPEG_UNAVAILABLE') from exc
        probe = ffmpeg.with_name('ffprobe.exe' if ffmpeg.suffix.lower() == '.exe' else 'ffprobe')
        if not probe.is_file():
            # The base application's imageio-ffmpeg wheel ships FFmpeg only.
            # Convert to a known playable format and use its output timeline.
            result = run_managed_process(
                [str(ffmpeg), '-v', 'info', '-nostdin', '-y', '-i', str(source),
                 '-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'libx264',
                 '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
                 '-c:a', 'aac', '-movflags', '+faststart', '-progress', 'pipe:1',
                 '-nostats', str(output)], timeout_seconds=1800)
            times = re.findall(rb'^out_time_us=(\d+)\s*$', result.stdout, re.MULTILINE)
            duration = max((int(value) for value in times), default=0) / 1_000_000
            header = re.search(rb'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', result.stderr)
            if header:
                hours, minutes, seconds = map(float, header.groups())
                duration = hours * 3600 + minutes * 60 + seconds
            if result.returncode or duration <= 0 or not output.is_file() or not output.stat().st_size:
                raise LocalSongError('LOCAL_SONG_VIDEO_INVALID')
            return duration
        result = run_managed_process(
            [str(probe), '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(source)],
            timeout_seconds=30)
        try:
            info = json.loads(result.stdout)
            streams = info['streams']
            videos = [x for x in streams if x.get('codec_type') == 'video']
            duration = float(info['format']['duration'])
            if result.returncode or not videos or not 0 < duration < float('inf'):
                raise ValueError()
        except (ValueError, KeyError, TypeError) as exc:
            raise LocalSongError('LOCAL_SONG_VIDEO_INVALID') from exc
        compatible = videos[0].get('codec_name') == 'h264' and all(
            x.get('codec_name') == 'aac' for x in streams if x.get('codec_type') == 'audio')
        codecs = ['-c', 'copy'] if compatible else ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p', '-c:a', 'aac']
        result = run_managed_process(
            [str(ffmpeg), '-v', 'error', '-nostdin', '-y', '-i', str(source),
             '-map', '0:v:0', '-map', '0:a:0?', *codecs, '-movflags', '+faststart', str(output)],
            timeout_seconds=1800)
        if result.returncode or not output.is_file() or output.stat().st_size == 0:
            raise LocalSongError('LOCAL_SONG_VIDEO_INVALID')
        return duration

    @staticmethod
    def _titles(source):
        root = source if source.is_dir() and not source.name.startswith('midi_') else source.parent
        titles = {}
        for path in (root / 'user_midi_manifest.json', root / '__olivia_data__/__olivia_titles__.json'):
            if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
                continue
            try:
                data = json.loads(path.read_text(encoding='utf-8-sig'))
                if isinstance(data.get('titles'), dict):
                    titles.update(data['titles'])
                for row in data.get('songs', []):
                    if isinstance(row, dict):
                        titles.setdefault(row.get('mediaId'), row.get('name'))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return titles

    def import_audio(self, source: Path, title: str):
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
            raise LocalSongError('LOCAL_SONG_NAME_INVALID')
        with wave.open(str(source), 'rb') as audio:
            duration = audio.getnframes() / audio.getframerate()
            if duration <= 0:
                raise LocalSongError('LOCAL_SONG_AUDIO_INVALID')
        with source.open('rb') as stream:
            song_id = hashlib.file_digest(stream, 'sha256').hexdigest()
        with _LOCK:
            rows = self._read()
            if any(x['id'] == song_id for x in rows):
                return {'id': song_id, 'added': False}
            self.root.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=self.root, suffix='.wav.tmp')
            os.close(fd)
            destination = self.root / (song_id + '.wav')
            try:
                shutil.copyfile(source, temporary)
                os.replace(temporary, destination)
                self._write([*rows, {'id': song_id, 'name': title.strip(), 'duration': duration,
                                     'media_type': 'audio', 'native_id': self._native_id(song_id, rows)}])
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            finally:
                Path(temporary).unlink(missing_ok=True)
        return {'id': song_id, 'added': True}

    def import_path(self, value):
        if not _IMPORT_LOCK.acquire(blocking=False):
            raise LocalSongError('LOCAL_SONG_IMPORT_BUSY')
        try:
            return self._import_path(value)
        finally:
            _IMPORT_LOCK.release()

    def _import_path(self, value):
        if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
            raise LocalSongError('LOCAL_SONG_PATH_INVALID')
        if value.startswith(('\\\\', '//')):
            raise LocalSongError('LOCAL_SONG_PATH_INVALID')
        source = Path(value).resolve()
        if not source.exists() or source == self.root or self.root in source.parents:
            raise LocalSongError('LOCAL_SONG_PATH_INVALID')
        if source.is_file():
            if source.suffix.lower() not in _VIDEO:
                raise LocalSongError('LOCAL_SONG_FORMAT_UNSUPPORTED')
            files = [source]
        else:
            files = []
            for directory, dirs, names in os.walk(source, followlinks=False):
                dirs[:] = [x for x in dirs if not (Path(directory) / x).is_symlink()
                           and not (Path(directory) / x).is_junction()]
                if Path(directory).name.startswith('midi_'):
                    # One imported performance can contain several camera/time variants.
                    # Restore its primary video as one song, as in 芙桃's auto-list builder.
                    candidates = [Path(directory) / name for name in sorted(names)
                                  if Path(name).suffix.lower() in _VIDEO
                                  and not (Path(directory) / name).is_symlink()]
                    def size(path):
                        try:
                            return path.stat().st_size
                        except OSError:
                            return -1  # unreadable here; reported per file when imported
                    if candidates:
                        files.append(max(candidates, key=size))
                    dirs[:] = []
                    if len(files) > 5000:
                        raise LocalSongError('LOCAL_SONG_TOO_MANY_FILES')
                    continue
                for name in sorted(names):
                    path = Path(directory) / name
                    if path.suffix.lower() in _VIDEO and not path.is_symlink() and self.root not in path.resolve().parents:
                        files.append(path)
                    if len(files) > 5000:
                        raise LocalSongError('LOCAL_SONG_TOO_MANY_FILES')
        titles = self._titles(source)
        report = {'added': 0, 'skipped': 0, 'failed': 0, 'errors': []}
        self.root.mkdir(parents=True, exist_ok=True)
        for path in files:
            temporary = None
            try:
                with path.open('rb') as stream:
                    song_id = hashlib.file_digest(stream, 'sha256').hexdigest()
                with _LOCK:
                    rows = self._read()
                    if any(x['id'] == song_id for x in rows):
                        report['skipped'] += 1
                        continue
                fd, name = tempfile.mkstemp(dir=self.root, suffix='.mp4')
                os.close(fd)
                temporary = Path(name)
                duration = self._prepare(path, temporary)
                title = titles.get(path.parent.name)
                title = title if isinstance(title, str) and title.strip() else path.stem
                with _LOCK:
                    rows = self._read()
                    if any(x['id'] == song_id for x in rows):
                        report['skipped'] += 1
                        continue
                    target = self.root / (song_id + '.mp4')
                    temporary.replace(target)
                    try:
                        self._write([*rows, {'id': song_id, 'name': title[:120], 'duration': duration,
                                             'native_id': self._native_id(song_id, rows)}])
                    except Exception:
                        target.unlink(missing_ok=True)
                        raise
                report['added'] += 1
            except Exception as exc:
                if isinstance(exc, LocalSongError) and str(exc) == 'LOCAL_SONG_CATALOG_INVALID':
                    raise
                report['failed'] += 1
                code = 'LOCAL_SONG_IMPORT_FAILED'
                if isinstance(exc, LocalSongError):
                    code = str(exc)
                elif isinstance(exc, PermissionError):
                    code = 'LOCAL_SONG_PERMISSION_DENIED'
                elif isinstance(exc, FileNotFoundError):
                    code = 'LOCAL_SONG_FILE_MISSING'
                elif isinstance(exc, (subprocess.TimeoutExpired, TimeoutError)):
                    code = 'LOCAL_SONG_PROCESS_TIMEOUT'
                elif isinstance(exc, OSError) and (exc.errno == 28 or getattr(exc, 'winerror', None) == 112):
                    code = 'LOCAL_SONG_DISK_FULL'
                report['errors'].append({'name': path.name, 'code': code})
                try:
                    log = self.root.parent / 'logs' / 'media-provider.jsonl'
                    log.parent.mkdir(parents=True, exist_ok=True)
                    with log.open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps({'timestamp': int(time.time()), 'provider': 'ffmpeg',
                                                 'error_code': code}) + '\n')
                except OSError:
                    pass
                if code == 'LOCAL_SONG_FFMPEG_UNAVAILABLE':
                    # A missing shared tool cannot be repaired by trying every video.
                    raise
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return report

# The native player's eventId is a bounded integer: scaling a SHA256 prefix to
# 1e15 overflows it. Derive a stable numeric identifier instead, and keep the
# SHA256 as the content identifier for media URLs and de-duplication.
#
# native_id contract:
#   type     decimal string. The bridge hands numbers to id/itemId/eventId, and a
#            string keeps full precision on the wire and in the catalog.
#   range    [NATIVE_SONG_ID_BASE, NATIVE_SONG_ID_BASE + NATIVE_SONG_ID_SPAN),
#            i.e. inside the signed 32-bit range and clear of the low ids the
#            bundled songs occupy.
#   migrate  catalog.json stores one native_id per song. A catalog written before
#            this change is backfilled by songs() on first read, then written back.
#   retire   deleting a song moves its id into native-ids.json so the number stays
#            owned by that content; probes never hand it to different content.
NATIVE_SONG_ID_BASE = 1000000000
NATIVE_SONG_ID_SPAN = 1000000000


def native_song_id(song_id):
    """Return the deterministic candidate native id for a song content id."""
    if not isinstance(song_id, str) or not _ID.fullmatch(song_id):
        raise LocalSongError('LOCAL_SONG_ID_INVALID')
    return str(_native_id_candidate(song_id))


def _native_id_candidate(song_id):
    return NATIVE_SONG_ID_BASE + (int(song_id[:16], 16) % NATIVE_SONG_ID_SPAN)


def _assign_native_id(song_id, taken):
    """Return a unique native id for song_id, avoiding the given taken ids.

    Starts from the deterministic candidate and probes forward, wrapping within
    [NATIVE_SONG_ID_BASE, NATIVE_SONG_ID_BASE + NATIVE_SONG_ID_SPAN). ``taken``
    is a set of already-assigned native ids (strings).
    """
    candidate = _native_id_candidate(song_id)
    for _ in range(NATIVE_SONG_ID_SPAN):
        if str(candidate) not in taken:
            return str(candidate)
        candidate += 1
        if candidate >= NATIVE_SONG_ID_BASE + NATIVE_SONG_ID_SPAN:
            candidate = NATIVE_SONG_ID_BASE
    raise LocalSongError('LOCAL_SONG_ID_SPACE_EXHAUSTED')
