"""Explicit remote routing for existing synchronous generation stages."""
import asyncio
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
    api = RemoteGeneration(env.get('OLIVIA_GPU_API_URL', ''), env.get('OLIVIA_GPU_API_KEY', ''))
    progress = PROGRESS_CALLBACK.get()
    if progress is not None:
        api.progress = progress
    ffmpeg = env.get('OLIVIA_FFMPEG_EXE')
    metadata = {}
    def validate(path):
        duration = _media_duration_seconds(path, required_streams=('0:a:0', '0:v:0') if kind in ('video', 'lipsync') else ('0:a:0',), ffmpeg_path=Path(ffmpeg) if ffmpeg else None)
        if duration is None or duration <= 0: raise CloudError('GPU_OUTPUT_INVALID', 502)
        metadata['duration_seconds'] = duration
    task = run_sync(lambda: asyncio.run(api.generate(kind, data, output, assets=assets, validate=validate)))
    return {**metadata, 'remote_task_id': task['task_id'],
            'audio_provider': 'remote', 'visual_provider': 'remote' if kind in ('video','lipsync') else ''}
