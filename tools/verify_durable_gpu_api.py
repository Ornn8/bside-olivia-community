"""One real TTS task through the durable API, using an existing local profile."""
import argparse
import asyncio
import hashlib
import json
import secrets
import subprocess
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp.test_utils import TestServer
from gpu_service.server import build_app
from runtime.remote_generation import RemoteGeneration


async def main(args):
    root = args.output.resolve(); root.mkdir(parents=True, exist_ok=True)
    settings = json.loads(args.config.read_text('utf-8'))['settings']
    reference = Path(settings['reference_audio'])
    with reference.open('rb') as source: digest = hashlib.file_digest(source, 'sha256').hexdigest()
    config = {'public_url': f'http://127.0.0.1:{args.port}', 'tokens': {'acceptance': secrets.token_urlsafe(32)},
        'signing_key': secrets.token_urlsafe(32), 'task_timeout_seconds': 300,
        'profiles': {'tts': {'tts_config_path': str(args.config.resolve())}},
        'shared_assets': {'reference-voice-v1': {'path': str(reference), 'sha256': digest}}}
    samples = []
    async def sample():
        while True:
            process = await asyncio.create_subprocess_exec('nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            out, _ = await process.communicate()
            if process.returncode == 0:
                samples.append(int(out.decode().splitlines()[0]))
            await asyncio.sleep(1)
    sampler = asyncio.create_task(sample()); start = time.monotonic()
    try:
        async with TestServer(build_app(config, root / 'state'), port=args.port) as server:
            api = RemoteGeneration(str(server.make_url('/')), config['tokens']['acceptance'])
            task = await api.generate('tts', {'text': '今天的任务已经完成了，记得休息一下。'}, root / 'output.wav', timeout=330)
            with wave.open(str(root / 'output.wav')) as audio: duration = audio.getnframes() / audio.getframerate()
            report = {'passed': duration > 1, 'task_id': task['task_id'], 'wall_seconds': round(time.monotonic()-start, 2),
                'audio_seconds': duration, 'sampled_total_vram_peak_mib': max(samples) if samples else None,
                'vram_note': 'Whole GPU usage, includes other processes; sampled once per second.'}
            (root / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            print(json.dumps(report))
    finally:
        sampler.cancel(); await asyncio.gather(sampler, return_exceptions=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18882)
    asyncio.run(main(parser.parse_args()))
