import asyncio
import json

from runtime.cloud_service import CloudError


def test_failed_cleanup_keeps_output_and_retries_without_regeneration(tmp_path):
    from runtime.gpu_cleanup import acknowledge_result, retry_pending
    from runtime.remote_generation import RemoteGeneration
    output=tmp_path/'media'/'final.mp4';output.parent.mkdir();output.write_bytes(b'completed video')
    api=RemoteGeneration('https://gpu.example','synthetic-key')
    calls=[]
    async def fail(action,data):
        calls.append(action)
        raise CloudError('GPU_CONNECT_FAILED')
    api.request=fail
    asyncio.run(acknowledge_result(api,'task-1234',output))
    marker=output.with_suffix(output.suffix+'.gpu-cleanup.json')
    assert marker.exists() and output.read_bytes()==b'completed video'
    assert 'synthetic-key' not in marker.read_text()
    async def success(action,data):
        calls.append(action)
        return {'task_id':data['task_id'],'status':'acknowledged'}
    api.request=success
    asyncio.run(retry_pending(tmp_path,api))
    assert not marker.exists() and output.exists()
    assert calls==['ack','ack']


def test_cleanup_never_acks_different_connection(tmp_path):
    from runtime.gpu_cleanup import acknowledge_result,retry_pending
    from runtime.remote_generation import RemoteGeneration
    output=tmp_path/'final.mp4';output.write_bytes(b'completed')
    api=RemoteGeneration('https://gpu.example','old-key')
    async def fail(*args):raise CloudError('GPU_CONNECT_FAILED')
    api.request=fail
    asyncio.run(acknowledge_result(api,'task-1234',output))
    other=RemoteGeneration('https://gpu.example','new-key')
    async def unexpected(*args):raise AssertionError('must not acknowledge another owner')
    other.request=unexpected
    asyncio.run(retry_pending(tmp_path,other))
    assert output.with_suffix('.mp4.gpu-cleanup.json').exists()
