"""Explicit remote routing for existing synchronous generation stages."""
import asyncio
import json
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
PROGRESS_CALLBACK = ContextVar('remote_generation_progress', default=None)


def enabled(environment=None):
    return (environment if environment is not None else os.environ).get('OLIVIA_GPU_ROUTE') == 'remote'


def run_sync(execute):
    try: asyncio.get_running_loop()
    except RuntimeError: return execute()
    with ThreadPoolExecutor(max_workers=1) as pool: return pool.submit(execute).result()


def capabilities(environment):
    from runtime.remote_generation import RemoteGeneration
    api = RemoteGeneration(environment.get('OLIVIA_GPU_API_URL', ''), environment.get('OLIVIA_GPU_API_KEY', ''))
    return run_sync(lambda: asyncio.run(api.request('capabilities', {})))


def generate(kind, data, output, *, environment=None, assets=None):
    from runtime.remote_generation import RemoteGeneration
    from runtime.media.music_reply import _media_duration_seconds
    from runtime.cloud_service import CloudError
    env = environment if environment is not None else os.environ
    if kind == 'tts' and env.get('OLIVIA_MEDIA_CHANNEL') == 'qq':
        data = {**data, 'channel': 'qq'}
    api = RemoteGeneration(env.get('OLIVIA_GPU_API_URL', ''), env.get('OLIVIA_GPU_API_KEY', ''))
    caps = run_sync(lambda: asyncio.run(api.request('capabilities', {})))
    if caps.get('server_media_planning') is True and kind in ('tts','video'):
        # Speech content and selected output remain facts; rendering controls live on the server.
        data={key:value for key,value in data.items() if key not in ('voice_plan','adaptive_delivery','enforce_content_gate')}
    progress = PROGRESS_CALLBACK.get()
    night = set((caps.get('night_batch') or {}).get('kinds') or ())
    # While the order waits for the night batch, a marker beside the output tells the letter when it starts.
    marker = Path(output).with_name(Path(output).stem + '-night.json') if kind in night else None

    def report(phase, task):
        hold = task.get('night_hold') if isinstance(task, dict) else None
        try:
            if isinstance(hold, dict) and type(hold.get('starts_at')) is int:
                if not marker.exists():
                    marker.write_text(json.dumps({'starts_at': hold['starts_at']}), encoding='utf-8')
            else:
                marker.unlink(missing_ok=True)
        except OSError:
            pass
        if progress is not None:
            progress(phase, task)
    if marker is not None:
        api.progress = report
    elif progress is not None:
        api.progress = progress
    ffmpeg = env.get('OLIVIA_FFMPEG_EXE')
    metadata = {}
    def validate(path):
        duration = _media_duration_seconds(path, required_streams=('0:a:0', '0:v:0') if kind in ('video', 'lipsync') else ('0:a:0',), ffmpeg_path=Path(ffmpeg) if ffmpeg else None)
        if duration is None or duration <= 0: raise CloudError('GPU_OUTPUT_INVALID', 502)
        metadata['duration_seconds'] = duration
    # A night-batch order can wait for hours: its receipt lets a restart resume it instead of ordering again.
    receipt = (Path(output).with_name(Path(output).stem + '-remote-order.private.json')
               if kind in ('video', 'lipsync') or kind in night else None)
    try:
        task = run_sync(lambda: asyncio.run(api.generate(kind, data, output, assets=assets,
            validate=validate, **({'receipt_path': receipt} if receipt is not None else {}))))
    finally:
        if marker is not None:
            marker.unlink(missing_ok=True)
    return {**metadata, 'remote_task_id': task['task_id'],
            'audio_provider': 'remote', 'visual_provider': 'remote' if kind in ('video','lipsync') else ''}
