"""Loopback-only real GPU acceptance. One TTS task; no cloud accounts or payments."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import sys
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from runtime.cloud_service import CloudService
from original_client_cloud_api import mount_cloud_api
from original_client_setup_api import LLMSetupService, mount_original_client_setup_api
from tts.breeze_adapter import adapter_metadata


async def main(args):
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    config = json.loads(Path(args.config).read_text('utf-8'))['settings']
    token = secrets.token_urlsafe(32)
    task = {'task_id':'acceptance-tts', 'status':'queued', 'outputs':[]}
    processes, futures, submitted = [], [], []
    wav = root/'gpu-task.wav'
    started = time.monotonic()
    async def infer():
        task['status'] = 'running'
        request = {k:config[k] for k in ('model_dir', 'runtime_root', 'reference_audio', 'reference_text')}
        request.update(config['provider_options'])
        request['adapter'] = adapter_metadata(request['adapter_dir'])
        request.update(text=submitted[0]['input']['text'], instruction='自然温柔地说话。', max_new_tokens=300, audio_only_unbounded=False)
        request_path = root/'request.json'
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
        temp = root/'tmp'; temp.mkdir(exist_ok=True)
        env = dict(os.environ, TEMP=str(temp), TMP=str(temp), HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', OMP_NUM_THREADS='4')
        with (root/'worker.log').open('wb') as log:
            proc = await asyncio.create_subprocess_exec(request['external_python'], '-I', '-X', 'utf8',
                str(Path(__file__).resolve().parents[1]/'tts/external_breeze_worker.py'),
                '--request', str(request_path), '--output', str(wav), '--status', str(root/'worker-status.json'),
                env=env, stdout=log, stderr=log, creationflags=0x08000000 if os.name=='nt' else 0)
            processes.append(proc)
            code = await proc.wait()
        if code or not wav.exists():
            task['status'] = 'failed'
        else:
            task.update(status='succeeded', outputs=[{'url':str(gpu.make_url('/artifact.wav'))}])
    async def handler(request):
        if request.headers.get('Authorization') != 'Bearer '+token:
            return web.json_response({}, status=401)
        if request.path == '/v1/tasks' and request.method == 'POST':
            body = await request.json()
            assert request.headers['Idempotency-Key'] == body['request_id']
            if submitted and submitted[0] != body:
                return web.json_response({}, status=409)
            if not submitted:
                assert body['kind'] == 'tts'
                submitted.append(body)
                futures.append(asyncio.create_task(infer()))
            return web.json_response(task, status=202)
        if request.path == '/v1/tasks/acceptance-tts': return web.json_response(task)
        return web.json_response({}, status=404)
    gateway = web.Application()
    gateway.router.add_route('*','/v1/{tail:.*}',handler)
    gateway.router.add_get('/artifact.wav', lambda r:web.FileResponse(wav))
    try:
        async with TestServer(gateway) as gpu:
            os.environ['OLIVIA_GPU_API_URL'] = str(gpu.make_url('/'))
            os.environ['OLIVIA_GPU_API_KEY'] = token
            app = web.Application()
            setup = LLMSetupService(root/'client')
            mount_original_client_setup_api(app,setup,trusted_origins=('https://client.example',))
            mount_cloud_api(app,CloudService(root/'client'),setup)
            async with TestClient(TestServer(app)) as client:
                origin = {'Origin':'https://client.example'}
                state = await (await client.get('/toy/setup/status',headers=origin)).json()
                headers = dict(origin, **{'X-Olivia-Setup-Action':'confirmed','X-Olivia-Setup-Session':state['session_token']})
                async def call(body):
                    response = await client.post('/toy/generation/action',json=body,headers=headers)
                    result = await response.json()
                    assert response.status == 200, result
                    return result
                payload = {'action':'submit','request_id':'local-acceptance-001','kind':'tts','input':{'text':'今天的任务已经完成了，记得休息一下。'}}
                await call(payload)
                await call(payload)
                assert len(submitted)==1
                async with asyncio.timeout(600):
                    while True:
                        result = await call({'action':'status','task_id':'acceptance-tts'})
                        if result['status'] in ('succeeded','failed'): break
                        await asyncio.sleep(2)
                assert result['status']=='succeeded', 'GPU worker failed; inspect local worker.log'
                response = await client.session.get(result['outputs'][0]['url'])
                assert response.status==200
                downloaded = root/'downloaded.wav'; downloaded.write_bytes(await response.read())
                with wave.open(str(downloaded)) as audio:
                    duration = audio.getnframes()/audio.getframerate()
                    assert duration > 1
                report = dict(passed=True, duration_seconds=duration, wall_seconds=time.monotonic()-started,
                              submitted_jobs=len(submitted), result=result, audio=str(downloaded))
                (root/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps(report,ensure_ascii=False))
    finally:
        for proc in processes:
            if proc.returncode is None:
                proc.terminate(); await proc.wait()
        for future in futures:
            if not future.done(): future.cancel()
        await asyncio.gather(*futures, return_exceptions=True)
        os.environ.pop('OLIVIA_GPU_API_KEY',None)
        os.environ.pop('OLIVIA_GPU_API_URL',None)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    parser.add_argument('--output',required=True)
    asyncio.run(main(parser.parse_args()))
