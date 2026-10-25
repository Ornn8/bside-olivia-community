"""Receive server media artifacts, retaining compatibility with legacy bundles."""
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import zipfile

from runtime.cloud_service import CloudError


def extract_materials(bundle, destination, *, include_spoken, server_assembly=False):
    expected = {'song.wav', 'song.mp4'} | ({'speech.mp4'} if include_spoken else set())
    if server_assembly:
        expected = {'song.wav','final.mp4'}
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        if len(names) != len(expected) + 1 or set(names) != expected | {'manifest.json'}:
            raise CloudError('GPU_MATERIALS_INVALID', 502)
        if archive.getinfo('manifest.json').file_size > 8192:
            raise CloudError('GPU_MATERIALS_INVALID', 502)
        manifest = json.loads(archive.read('manifest.json'))
        if (manifest.get('version') != (2 if server_assembly else 1)
                or manifest.get('assembly') != ('server' if server_assembly else 'client') or set(manifest.get('files', {})) != expected):
            raise CloudError('GPU_MATERIALS_INVALID', 502)
        total = sum(item.file_size for item in archive.infolist())
        if total > 2147483648:
            raise CloudError('GPU_MATERIALS_TOO_LARGE', 502)
        for name in sorted(expected):
            info = archive.getinfo(name)
            metadata = manifest['files'][name]
            if info.file_size <= 0 or info.file_size != metadata['bytes']:
                raise CloudError('GPU_MATERIALS_INVALID', 502)
            target = Path(destination) / name
            with archive.open(name) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output)
            with target.open('rb') as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            if digest != metadata['sha256']:
                raise CloudError('GPU_MATERIALS_INVALID', 502)
    return expected


async def fetch_task_video(api, task_id, output, *, ffmpeg=None):
    """A video the service already finished for this account, fetched by its task without ordering anything:
    a song video bundle assembled by the service (its final.mp4) or a single MP4. Checked before it is kept.
    Returns 'song' for a song video and 'spoken' for a single MP4 (her speaking)."""
    from runtime.media.music_reply import _media_duration_seconds
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='olivia-task-video-', dir=output.parent) as temporary:
        work = Path(temporary)
        download = work / 'result.bin'
        await api.download_task(task_id, download)
        song = zipfile.is_zipfile(download)
        if song:
            try:
                extract_materials(download, work, include_spoken=False, server_assembly=True)
            except (ValueError, KeyError, TypeError, zipfile.BadZipFile):
                raise CloudError('GPU_MATERIALS_INVALID', 502) from None
            video = work / 'final.mp4'
        else:
            video = work / 'result.mp4'
            download.replace(video)
        if not _media_duration_seconds(video, required_streams=('0:a:0', '0:v:0'), ffmpeg_path=ffmpeg):
            raise CloudError('GPU_OUTPUT_INVALID', 502)
        video.replace(output)
    return 'song' if song else 'spoken'


def render_music_materials(kind, data, output, *, environment, include_spoken, reply_text,
                           performance_video_path, official_reply_reference_path,
                           spoken_action_base_path=None, voice_performance_plan=None,
                           source_audio=None, **unused):
    from runtime.remote_generation import RemoteGeneration
    from runtime.remote_pipeline import run_sync
    from runtime.media.music_reply import _run, concat_videos, _media_duration_seconds
    from runtime.media.latentsync_reply import resolve_ffmpeg_executable
    ffmpeg = resolve_ffmpeg_executable(environment)
    api = RemoteGeneration(environment.get('OLIVIA_GPU_API_URL', ''), environment.get('OLIVIA_GPU_API_KEY', ''))
    caps = run_sync(lambda: asyncio.run(api.request('capabilities',{})))
    server_assembly = caps.get('server_music_assembly') is True
    inputs = {**data, 'include_spoken': include_spoken,
              'scene_asset': 'official-performance-lipsync-safe-2950f-v1'}
    assets = {}
    if source_audio is not None:
        assets['source_asset'] = source_audio
    if include_spoken:
        inputs['text'] = reply_text
        if voice_performance_plan is not None:
            inputs['voice_plan'] = voice_performance_plan.to_dict()
        inputs['spoken_scene_asset'] = 'official-reply-action-base-v1'
    if server_assembly:
        inputs['server_assembly'] = True
        inputs.pop('voice_plan',None)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='olivia-materials-', dir=output.parent) as temporary:
        work = Path(temporary)
        bundle = work / 'materials.zip'
        task = run_sync(lambda: asyncio.run(api.generate(kind, inputs, bundle, assets=assets,
            receipt_path=output.with_name(output.stem + '-remote-order.private.json'))))
        try:
            names = extract_materials(bundle, work, include_spoken=include_spoken, server_assembly=server_assembly)
        except (ValueError, KeyError, TypeError, zipfile.BadZipFile):
            raise CloudError('GPU_MATERIALS_INVALID', 502) from None
        for name in names:
            streams = ('0:a:0', '0:v:0') if name.endswith('.mp4') else ('0:a:0',)
            if not _media_duration_seconds(work / name, required_streams=streams, ffmpeg_path=ffmpeg):
                raise CloudError('GPU_OUTPUT_INVALID', 502)
        if server_assembly:
            (work/'final.mp4').replace(output)
            from runtime.gpu_cleanup import acknowledge_result
            run_sync(lambda: asyncio.run(acknowledge_result(api,task['task_id'],output)))
            return {'remote_task_id':task['task_id'],'assembly':'server',
                    'reply_structure':'normal_video_then_official_transition_then_song_video' if include_spoken else 'singing_only'}
        song = work / 'assembled-song.mp4'
        _run([str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-y', '-i', str(work / 'song.mp4'),
              '-i', str(work / 'song.wav'), '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy',
              '-c:a', 'aac', '-b:a', '192k', '-shortest', '-movflags', '+faststart', str(song)],
             'MUSIC_REPLY_AUDIO_MUX_FAILED', timeout=900, cleanup_path=song)
        final = work / 'final.mp4'
        if include_spoken:
            transition = Path(official_reply_reference_path) if official_reply_reference_path else None
            concat_videos(work / 'speech.mp4', song, final,
                          transition_video_path=transition if transition and transition.is_file() else None,
                          ffmpeg_path=ffmpeg)
        else:
            shutil.copyfile(song, final)
        final.replace(output)
    from runtime.gpu_cleanup import acknowledge_result
    run_sync(lambda: asyncio.run(acknowledge_result(api, task['task_id'], output)))
    return {'remote_task_id': task['task_id'], 'assembly': 'local',
            'reply_structure': 'normal_video_then_official_transition_then_song_video' if include_spoken else 'singing_only'}
