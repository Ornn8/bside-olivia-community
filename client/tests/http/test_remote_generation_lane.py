"""A background order waits for its lane, a busy status read never abandons a paid task, and a cover keeps a receipt
so a restart fetches the task it already ordered instead of ordering another."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from runtime import remote_generation, remote_pipeline
from runtime.cloud_service import CloudError


class Restart(BaseException):
    """The app stopping in the middle of an order, not a failure of the order."""


class Service:
    """The media service as RemoteGeneration.request sees it: a scripted answer per submit and per status read."""

    def __init__(self, submits=(), statuses=()):
        self.submits, self.statuses, self.calls = list(submits), list(statuses), []

    def install(self, monkeypatch):
        service = self

        async def request(self, action, data):
            service.calls.append((action, data))
            if action == 'capabilities':
                return {'kinds': ['cover', 'tts'], 'shared_assets': []}
            if action == 'submit':
                answer = service.submits.pop(0)
            elif action == 'status':
                answer = service.statuses.pop(0)
            elif action == 'cancel':
                return {'task_id': data['task_id'], 'status': 'cancelled'}
            else:
                raise AssertionError(action)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        async def upload(self, path):
            service.calls.append(('upload', path.name))
            return 'asset-1'

        async def download(self, task, path, *, validate=None, attempts=3):
            service.calls.append(('download', task['task_id']))
            path.write_bytes(b'RIFF cover ' + task['task_id'].encode())
            if validate is not None:
                validate(path)
            return task
        monkeypatch.setattr(remote_generation.RemoteGeneration, 'request', request)
        monkeypatch.setattr(remote_generation.RemoteGeneration, 'upload', upload)
        monkeypatch.setattr(remote_generation.RemoteGeneration, '_download', download)
        return self

    def of(self, action):
        return [data for kind, data in self.calls if kind == action]


def task(task_id, status, stage='gpu_running'):
    return {'task_id': task_id, 'status': status, 'stage': stage,
            'outputs': [{'url': 'https://gpu.example/r/' + task_id}] if status == 'succeeded' else []}


def busy():
    return CloudError('GPU_QUEUE_FULL', 429)


@pytest.fixture
def slept(monkeypatch):
    seen = []

    async def sleep(seconds, result=None):
        seen.append(seconds)
    monkeypatch.setattr(remote_generation.asyncio, 'sleep', sleep)
    return seen


@pytest.fixture
def api():
    return remote_generation.RemoteGeneration('https://gpu.example', 'synthetic-key')


def cover_environment(tmp_path, monkeypatch):
    monkeypatch.setattr('runtime.media.music_reply._media_duration_seconds', lambda *args, **kwargs: 200.0)
    source = tmp_path / 'source.wav'
    source.write_bytes(b'RIFF source')
    return {'OLIVIA_GPU_API_URL': 'https://gpu.example', 'OLIVIA_GPU_API_KEY': 'synthetic-key'}, source


def submit_under(api, wait):
    token = remote_generation.LANE_WAIT.set(wait)
    try:
        return asyncio.run(api._submit({'request_id': 'r-12345678', 'kind': 'cover', 'input': {}}))
    finally:
        remote_generation.LANE_WAIT.reset(token)


def test_a_busy_lane_waits_with_the_same_order_and_key(monkeypatch, api, slept):
    service = Service(submits=[busy(), busy(), busy(), task('t-2', 'queued', 'queued')]).install(monkeypatch)
    got = submit_under(api, remote_generation.LaneWait(600))
    assert got['task_id'] == 't-2'
    submitted = service.of('submit')
    assert len(submitted) == 4 and len({item['request_id'] for item in submitted}) == 1
    assert slept == [2, 4, 8]


def test_a_lane_that_stays_busy_ends_with_GPU_LANE_BUSY(monkeypatch, api, slept):
    clock = [1000.0]
    monkeypatch.setattr(remote_generation, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))

    async def sleep(seconds, result=None):
        slept.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(remote_generation.asyncio, 'sleep', sleep)
    Service(submits=[busy()] * 100).install(monkeypatch)
    with pytest.raises(CloudError) as raised:
        submit_under(api, remote_generation.LaneWait(120))
    assert raised.value.code == 'GPU_LANE_BUSY'
    assert sum(slept) == pytest.approx(120) and max(slept) == 30


def test_an_order_taken_back_during_the_wait_is_never_sent(monkeypatch, api):
    service = Service(submits=[busy(), task('t-3', 'queued', 'queued')]).install(monkeypatch)
    flag = []

    async def sleep(seconds, result=None):
        flag.append(True)
    monkeypatch.setattr(remote_generation.asyncio, 'sleep', sleep)
    with pytest.raises(CloudError) as raised:
        submit_under(api, remote_generation.LaneWait(600, lambda: bool(flag)))
    assert raised.value.code == 'GPU_ORDER_CANCELLED' and len(service.of('submit')) == 1


def test_a_task_accepted_just_as_it_is_taken_back_is_cancelled(monkeypatch, api, slept):
    service = Service(submits=[task('t-4', 'queued', 'queued')]).install(monkeypatch)
    with pytest.raises(CloudError) as raised:
        submit_under(api, remote_generation.LaneWait(600, lambda: True))
    assert raised.value.code == 'GPU_ORDER_CANCELLED' and service.of('cancel') == [{'task_id': 't-4'}]


def test_chat_voices_and_pictures_still_fail_at_once_on_a_busy_lane(api, slept):
    calls = []

    async def request(action, data):
        calls.append(action)
        raise busy()
    api.request = request
    with pytest.raises(CloudError) as raised:
        asyncio.run(api._submit({'request_id': 'r-12345678', 'kind': 'tts', 'input': {}}))
    assert raised.value.code == 'GPU_QUEUE_FULL' and calls == ['submit'] and slept == []


def test_status_reads_back_off_and_give_up_only_after_a_long_busy_spell(monkeypatch, api, slept):
    answers = [busy()] * 7 + [CloudError('GPU_CONNECT_FAILED')] + [task('t-1', 'running')]

    async def request(action, data):
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer
    api.request = request
    assert asyncio.run(api._status('t-1'))['status'] == 'running'
    assert slept == [2, 4, 8, 16, 30, 30, 30, 2]   # the 429s did not use up the reads kept for a service switch
    monkeypatch.setattr(remote_generation, 'STATUS_BUSY_SECONDS', 0)
    answers[:] = [busy()]
    with pytest.raises(CloudError) as raised:
        asyncio.run(api._status('t-1'))
    assert raised.value.code == 'GPU_QUEUE_FULL'


def test_a_failed_tasks_own_code_reaches_the_error_detail(tmp_path, api):
    failed = {'task_id': 't-1', 'status': 'failed', 'outputs': [], 'error_code': 'YUE2_TEMPO_OUT_OF_RANGE'}
    with pytest.raises(CloudError) as raised:
        asyncio.run(api._download_once(failed, tmp_path / 'x.wav'))
    assert raised.value.code == 'GPU_TASK_FAILED' and raised.value.detail == 'YUE2_TEMPO_OUT_OF_RANGE'
    plain = {'task_id': 't-1', 'status': 'failed', 'outputs': []}
    with pytest.raises(CloudError) as raised:
        asyncio.run(api._download_once(plain, tmp_path / 'x.wav'))
    assert raised.value.detail is None


def test_a_cover_cut_off_by_a_restart_fetches_its_own_task_and_never_orders_another(tmp_path, monkeypatch, slept):
    environment, source = cover_environment(tmp_path, monkeypatch)
    output = tmp_path / 'made' / 'song-x.wav'
    output.parent.mkdir()
    first = Service(submits=[task('task-1', 'queued', 'queued')],
                    statuses=[task('task-1', 'running'), Restart()]).install(monkeypatch)
    with pytest.raises(Restart):
        remote_pipeline.generate('cover', {'lyrics': 'la'}, output, environment=environment,
                                 assets={'source_asset': source})
    receipt = output.with_name('song-x-remote-order.private.json')
    saved = json.loads(receipt.read_text(encoding='utf-8'))
    assert saved['task_id'] == 'task-1' and saved['submission']['kind'] == 'cover' and len(first.of('submit')) == 1
    second = Service(statuses=[task('task-1', 'running'), task('task-1', 'succeeded', 'completed')]).install(monkeypatch)
    result = remote_pipeline.generate('cover', {'lyrics': 'la'}, output, environment=environment,
                                      assets={'source_asset': source})
    assert second.of('submit') == [] and second.of('upload') == []
    assert second.of('download') == ['task-1'] and result['remote_task_id'] == 'task-1'
    assert not receipt.exists()   # the order is over: its receipt is gone


def test_a_cover_with_other_inputs_never_reuses_the_receipt(tmp_path, monkeypatch, slept):
    environment, source = cover_environment(tmp_path, monkeypatch)
    output = tmp_path / 'made' / 'song-x.wav'
    output.parent.mkdir()
    Service(submits=[task('task-1', 'queued', 'queued')], statuses=[Restart()]).install(monkeypatch)
    with pytest.raises(Restart):
        remote_pipeline.generate('cover', {'lyrics': 'la'}, output, environment=environment,
                                 assets={'source_asset': source})
    receipt = output.with_name('song-x-remote-order.private.json')
    old = json.loads(receipt.read_text(encoding='utf-8'))
    second = Service(submits=[task('task-2', 'queued', 'queued')],
                     statuses=[task('task-2', 'succeeded', 'completed')]).install(monkeypatch)
    result = remote_pipeline.generate('cover', {'lyrics': 'other words'}, output, environment=environment,
                                      assets={'source_asset': source})
    [submitted] = second.of('submit')
    assert result['remote_task_id'] == 'task-2' and submitted['request_id'] != old['submission']['request_id']
    assert {'task_id': 'task-1'} not in second.of('status')


def test_the_receipt_goes_when_the_task_fails_and_the_tasks_code_is_kept(tmp_path, monkeypatch, slept):
    environment, source = cover_environment(tmp_path, monkeypatch)
    output = tmp_path / 'made' / 'song-x.wav'
    output.parent.mkdir()
    Service(submits=[task('task-1', 'queued', 'queued')],
            statuses=[{**task('task-1', 'failed', 'failed'), 'error_code': 'YUE2_TEMPO_OUT_OF_RANGE'}]).install(monkeypatch)

    async def download(self, task, path, *, validate=None, attempts=3):
        return await remote_generation.RemoteGeneration._download_once(self, task, path, validate=validate)
    monkeypatch.setattr(remote_generation.RemoteGeneration, '_download', download)
    with pytest.raises(CloudError) as raised:
        remote_pipeline.generate('cover', {'lyrics': 'la'}, output, environment=environment,
                                 assets={'source_asset': source})
    assert raised.value.detail == 'YUE2_TEMPO_OUT_OF_RANGE'
    assert not output.with_name('song-x-remote-order.private.json').exists()


def test_a_context_set_by_the_caller_reaches_an_order_run_from_a_running_loop():
    seen = []
    token = remote_generation.LANE_WAIT.set(remote_generation.LaneWait(77))

    async def inside():
        remote_pipeline.run_sync(lambda: seen.append(remote_generation.LANE_WAIT.get().seconds))
    try:
        asyncio.run(inside())
    finally:
        remote_generation.LANE_WAIT.reset(token)
    assert seen == [77]
