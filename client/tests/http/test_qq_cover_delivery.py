"""A cover asked for in a QQ chat from start to end: the shared song, the writer's checked request, the order, the mp3
file sent back, the library entry, the memory record; every failure told once with its reason and code; a restart that
neither orders nor sends twice; and the whole path inert while the switch is off."""
import asyncio
from copy import deepcopy
import json
import time
from types import SimpleNamespace

import pytest
from aiohttp import web

from runtime import remote_generation
from runtime.cloud_service import CloudError
from runtime.media import song_resolver
from runtime.media.song_sources import SongSources
from runtime.personal_chat import cover_chat, cover_delivery, decision, song_intake
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.qq import QQFileRejected
from runtime.personal_chat.service import PersonalChatService
from tests.http.test_qq_cover_intake import IDENTITY, NETEASE_CARD, card, envelope, text_of
from tests.http.test_remote_generation_lane import Restart, Service, busy, task

REAL_ORDER = cover_delivery._order
FACTS = {'capabilities': {'kinds': ['cover', 'tts']}, 'sings_covers': True,
         'account': {'mode': 'money', 'balance_cents': 900}}


class Send:
    """The QQ sender of one chat: texts, and files that are acknowledged unless told to fail."""

    def __init__(self, file_failure=None):
        self.texts, self.files, self.file_failure = [], [], file_failure

    async def __call__(self, text):
        self.texts.append(text)
        return 'text-ack'

    async def file(self, path, name):
        self.files.append((name, path.read_bytes()))
        if self.file_failure:
            raise self.file_failure
        return 'file-ack'


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv('OLIVIA_GPU_API_URL', 'https://gpu.example')
    monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'synthetic-key')
    monkeypatch.setenv('OLIVIA_GPU_ROUTE', 'remote')
    monkeypatch.setattr(cover_chat, 'ENABLED', True)
    state = SimpleNamespace(facts=dict(FACTS), library=[], orders=[], letter=None, tmp=tmp_path, slept=[])

    async def ready(environment):
        return True

    async def facts(environment):
        return dict(state.facts)

    async def caps(environment):
        return {'kinds': ['cover'], 'shared_assets': []}
    monkeypatch.setattr(cover_chat, 'relay_ready', ready)
    monkeypatch.setattr(cover_chat, 'preflight_facts', facts)
    monkeypatch.setattr('runtime.media_capabilities.current', caps)

    def validate(raw, destination, environment):
        destination.write_bytes(b'RIFF' + raw.read_bytes())
        raw.unlink()
        return 200.0
    monkeypatch.setattr('runtime.media.cover_upload.validate_upload', validate)

    async def fetch(request, target, url, token):
        target.write_bytes(b'audio')
        return {'title': request.get('title') or '晴天', 'singer': '周杰伦', 'source': 'netease', 'seconds': 200.0,
                'identity': IDENTITY, 'bytes': 5}
    monkeypatch.setattr(song_resolver, 'resolve', fetch)

    def order(source, output):
        state.orders.append(source.read_bytes())
        output.write_bytes(b'RIFFcover')
    monkeypatch.setattr(cover_delivery, '_order', order)
    monkeypatch.setattr(cover_delivery, 'to_mp3', lambda wav, mp3: mp3.write_bytes(b'ID3' + wav.read_bytes()))

    class Library:
        def __init__(self, root, environment):
            pass

        def import_audio(self, source, title):
            state.library.append((title, source.read_bytes()))
            return {'id': 'x', 'added': True}
    monkeypatch.setattr('runtime.media.local_song_library.LocalSongLibrary', Library)

    async def sleep(seconds, result=None):
        state.slept.append(seconds)
    monkeypatch.setattr(remote_generation.asyncio, 'sleep', sleep)
    state.persisted = []
    state.rows = []
    state.server = SimpleNamespace(
        _local_data_root=lambda: tmp_path, _persist_store_state=lambda: state.persisted.append(1),
        store=SimpleNamespace(personal_chats=state.rows), _active_undelivered_letter=lambda: state.letter)
    return state


def asked_row(world, letter_id='turn-1', *, share=NETEASE_CARD, words='你翻唱这首歌吧'):
    """A turn in which he shared a song and asked for a cover: the writer's answer decoded as the backend does."""
    row = {'letter_id': letter_id, 'channel': 'qq', 'delivery_status': 'DELIVERED', 'letter_status': 'COMPLETED',
           'binding_id': PersonalMessage('qq', 'b', 'u', '', '').binding_id}
    text = text_of(card(share)) + words
    event = PersonalMessage('qq', 'b', 'u', letter_id, text)

    async def prepare():
        await song_intake.receive_shares(world.server, event, row)
        return await cover_chat.offered(world.server, row, {})
    offered = asyncio.run(prepare())
    written = decision.decode(envelope(text='好呀，我唱给你听。', cover={'source': offered[0]['id'], 'evidence': words}),
                              user=text, now=time.time(), cover_sources=offered)
    row.update(cover_request=written['cover_request'], cover_status=cover_chat.PENDING)
    world.rows.append(row)
    return row


def run_service(world, row, send, *, rounds=1):
    """The service's own restart path: every row of the chat is looked at again, ``rounds`` times."""
    async def scenario():
        async def generate(event, r):
            return 'x'

        async def commit(r):
            pass
        service = PersonalChatService(world.rows, lambda: None, generate, commit, {'qq': ('b', 'u')},
                                      cover=lambda r, s: cover_delivery.deliver(world.server, r, s))
        for _ in range(rounds):
            service.resume_speech('qq', send)
            if service.speech_tasks:
                await asyncio.gather(*service.speech_tasks.values())
        return service
    return asyncio.run(scenario())


def test_a_shared_link_becomes_a_delivered_cover_from_request_to_memory(world):
    row = asked_row(world)
    source = row['cover_request']['source']
    send = Send()
    run_service(world, row, send)
    assert row['cover_status'] == 'DELIVERED' and row['cover_finished'] is True
    assert world.orders == [b'RIFFaudio']                                   # her cover is made from the kept song
    assert send.files == [('晴天（翻唱）.mp3', b'ID3RIFFcover')]               # an mp3 named after the song
    assert send.texts == ['给你唱了《晴天》的翻唱，发给你了。']                      # one closing line, after the file
    assert world.library == [('晴天（翻唱）', b'RIFFcover')]                   # added to the music library
    assert SongSources(world.tmp).get(source)['used'] is True
    assert not cover_delivery.work_folder(world.tmp, row).exists()            # the work files are gone
    [memory] = list(cover_chat.memory_rows(row))
    assert memory['reply_text'] == send.texts[0] and memory['letter_status'] == 'COMPLETED'
    assert memory['letter_id'].startswith('cover-')
    run_service(world, row, send, rounds=2)                                     # a restart sends and orders nothing more
    assert len(send.files) == 1 and len(send.texts) == 1 and len(world.orders) == 1


def test_covers_in_a_row_are_each_made_and_sent(world):
    first = asked_row(world, 'turn-1')
    send = Send()
    run_service(world, first, send)
    second = asked_row(world, 'turn-2', share={**NETEASE_CARD, 'meta': {'music': {
        **NETEASE_CARD['meta']['music'], 'title': '夜曲', 'jumpUrl': 'https://y.music.163.com/m/song?id=9'}}})
    run_service(world, second, send)
    assert [row['cover_status'] for row in (first, second)] == ['DELIVERED', 'DELIVERED']
    assert [name for name, data in send.files] == ['晴天（翻唱）.mp3', '夜曲（翻唱）.mp3']
    assert len(world.library) == 2 and len(world.orders) == 2


def test_a_second_cover_asked_while_one_is_being_made_waits_its_turn_with_a_plain_message(world):
    first = asked_row(world, 'turn-1')
    first['cover_status'] = 'MAKING'
    first['delivery_status'] = 'PENDING'      # being made in another task: this pass only looks at the second
    second = asked_row(world, 'turn-2')
    send = Send()
    run_service(world, second, send)
    assert second['cover_status'] == 'FAILED' and second['cover_error_code'] == 'COVER_IN_PROGRESS'
    assert len(send.texts) == 1 and 'COVER_IN_PROGRESS' in send.texts[0] and not send.files and not world.orders
    assert SongSources(world.tmp).get(second['cover_request']['source'])['used'] is False   # she can try again


# --- failures: one specific message each ---------------------------------------------------------------------

def failing_order(error):
    def order(source, output):
        raise error
    return order


def task_failed(code):
    error = CloudError('GPU_TASK_FAILED', 502)
    error.detail = code
    return error


@pytest.mark.parametrize('error,code,words', [
    (CloudError('GPU_INSUFFICIENT_BALANCE', 402), 'GPU_INSUFFICIENT_BALANCE', '余额不够，需要充值'),
    (CloudError('GPU_BILLING_CONSENT_REQUIRED', 428), 'GPU_BILLING_CONSENT_REQUIRED', '同意计费'),
    (CloudError('GPU_QUEUE_FULL', 429), 'GPU_QUEUE_FULL', '正忙'),
    (CloudError('GPU_LANE_BUSY', 429), 'GPU_LANE_BUSY', '一直忙着'),
    (CloudError('GPU_TASK_TIMEOUT', 504), 'GPU_TASK_TIMEOUT', '做了很久'),
    (CloudError('GPU_CONNECT_FAILED'), 'GPU_CONNECT_FAILED', '连不上'),
    (CloudError('GPU_AUTH_FAILED', 502), 'GPU_AUTH_FAILED', '没有认可'),
    (task_failed('YUE2_TEMPO_OUT_OF_RANGE'), 'YUE2_TEMPO_OUT_OF_RANGE', '处理不了这首歌'),
    (task_failed(None), 'GPU_TASK_FAILED', '做歌的服务出了问题'),
    (CloudError('COVER_GENERATION_FAILED'), 'COVER_GENERATION_FAILED', '中间出了点问题'),
    (ValueError('YUE2_TOO_SHORT: 12 s of singing'), 'YUE2_TOO_SHORT', '处理不了这首歌'),
    (RuntimeError('boom'), 'COVER_FAILED', '中间出了点问题')])
def test_a_failed_order_is_told_once_with_its_reason_and_code(world, monkeypatch, error, code, words):
    monkeypatch.setattr(cover_delivery, '_order', failing_order(error))
    row = asked_row(world)
    send = Send()
    run_service(world, row, send, rounds=3)
    assert row['cover_status'] == 'FAILED' and row['cover_error_code'] == code
    [message] = send.texts
    assert words in message and f'错误码 {code}' in message and '元' not in message
    assert not send.files and world.library == [] and len(world.persisted) > 0
    assert SongSources(world.tmp).get(row['cover_request']['source'])['used'] is False


@pytest.mark.parametrize('change,code,words', [
    ({'account': {'mode': 'money', 'balance_cents': 0}}, 'GPU_INSUFFICIENT_BALANCE', '余额不足'),
    ({'capabilities': {'kinds': ['tts']}}, 'GPU_CAPABILITY_UNAVAILABLE', '没有这项功能'),
    ({'sings_covers': False}, 'COVER_ENGINE_UNAVAILABLE', '自己声音'),
    ({'capabilities': {'kinds': ['cover'], 'night_batch': {'kinds': ['cover']}}}, 'COVER_NIGHT_ONLY', '夜里'),
    ({'letter': True}, 'LETTER_IN_PROGRESS', '另一封回信')])
def test_a_refused_check_is_told_once_and_nothing_is_ordered(world, change, code, words):
    if 'letter' in change:
        world.letter = {'letter_id': 'l1'}
    else:
        world.facts.update(change)
    row = asked_row(world)
    send = Send()
    run_service(world, row, send, rounds=2)
    assert row['cover_status'] == 'FAILED' and row['cover_error_code'] == code
    [message] = send.texts
    assert words in message and f'错误码 {code}' in message
    assert world.orders == [] and not send.files


def test_a_song_that_has_vanished_is_told_with_its_code(world):
    row = asked_row(world)
    SongSources(world.tmp).remove(row['cover_request']['source'])
    send = Send()
    run_service(world, row, send)
    assert row['cover_status'] == 'FAILED' and 'COVER_SOURCE_REQUIRED' in send.texts[0] and len(send.texts) == 1


def test_a_service_that_cannot_encode_or_a_computer_without_the_cloud_is_told(world, monkeypatch):
    def broken(wav, mp3):
        raise cover_chat.CoverFailure('COVER_ENCODE_FAILED')
    monkeypatch.setattr(cover_delivery, 'to_mp3', broken)
    row = asked_row(world)
    send = Send()
    run_service(world, row, send)
    assert row['cover_status'] == 'FAILED' and 'COVER_ENCODE_FAILED' in send.texts[0] and not send.files
    monkeypatch.delenv('OLIVIA_GPU_ROUTE')
    other = asked_row(world, 'turn-2')
    send = Send()
    run_service(world, other, send)
    assert 'COVER_RUNTIME_UNAVAILABLE' in send.texts[0] and world.orders == [b'RIFFaudio']   # only the first got as far as an order


def test_a_file_qq_refuses_is_failed_and_one_it_may_have_accepted_is_never_sent_again(world):
    row = asked_row(world)
    send = Send(file_failure=QQFileRejected('no'))
    run_service(world, row, send, rounds=2)
    assert row['cover_status'] == 'FAILED' and len(send.files) == 1
    assert len(send.texts) == 1 and 'QQ_FILE_REJECTED' in send.texts[0]
    unsure = asked_row(world, 'turn-2')
    send = Send(file_failure=TimeoutError())
    run_service(world, unsure, send, rounds=3)
    assert unsure['cover_status'] == 'UNKNOWN' and len(send.files) == 1 and len(send.texts) == 1
    assert 'QQ_FILE_UNCONFIRMED' in send.texts[0] and '没收到的话告诉我' in send.texts[0]
    assert unsure.get('cover_completed_at') is None and list(cover_chat.memory_rows(unsure)) == []   # not a memory


# --- restarts, the lane, the switch -----------------------------------------------------------------------------

@pytest.fixture
def pipeline(world, monkeypatch):
    """The real order path against a scripted media service."""
    monkeypatch.setattr(cover_delivery, '_order', REAL_ORDER)
    monkeypatch.setattr('runtime.media.music_reply._media_duration_seconds', lambda *a, **k: 200.0)
    return world


def test_a_restart_in_the_middle_of_an_order_fetches_the_same_task_and_sends_once(pipeline, monkeypatch):
    world = pipeline
    row = asked_row(world)
    send = Send()
    first = Service(submits=[task('task-1', 'queued', 'queued')],
                    statuses=[task('task-1', 'running'), Restart()]).install(monkeypatch)
    with pytest.raises(Restart):
        asyncio.run(cover_delivery.deliver(world.server, row, send))
    assert row['cover_status'] == 'MAKING' and len(first.of('submit')) == 1 and not send.files
    folder = cover_delivery.work_folder(world.tmp, row)
    assert json.loads((folder / 'cover-remote-order.private.json').read_text(encoding='utf-8'))['task_id'] == 'task-1'
    second = Service(statuses=[task('task-1', 'running'), task('task-1', 'succeeded', 'completed')]).install(monkeypatch)
    restarted = json.loads(json.dumps(row))                     # the row as it was saved, in a new run
    world.rows[:] = [restarted]
    run_service(world, restarted, send)
    assert second.of('submit') == [] and second.of('upload') == [] and second.of('download') == ['task-1']
    assert restarted['cover_status'] == 'DELIVERED' and len(send.files) == 1 and len(send.texts) == 1
    run_service(world, restarted, send, rounds=2)
    assert len(send.files) == 1 and len(send.texts) == 1


def test_a_result_already_fetched_before_a_restart_is_not_ordered_again(pipeline, monkeypatch):
    world = pipeline
    row = asked_row(world)
    folder = cover_delivery.work_folder(world.tmp, row)
    folder.mkdir(parents=True)
    (folder / 'cover.wav').write_bytes(b'RIFFcover')
    row['cover_status'] = 'MAKING'
    service = Service().install(monkeypatch)
    send = Send()
    run_service(world, row, send)
    assert service.of('submit') == [] and row['cover_status'] == 'DELIVERED' and len(send.files) == 1


def test_a_busy_lane_is_waited_for_with_the_same_order(pipeline, monkeypatch):
    world = pipeline
    row = asked_row(world)
    service = Service(submits=[busy(), busy(), task('task-2', 'queued', 'queued')],
                      statuses=[task('task-2', 'succeeded', 'completed')]).install(monkeypatch)
    send = Send()
    run_service(world, row, send)
    submitted = service.of('submit')
    assert len(submitted) == 3 and len({item['request_id'] for item in submitted}) == 1
    assert submitted[0]['kind'] == 'cover' and submitted[0]['input']['cover_options']['engine'] == 'yue2'
    assert submitted[0]['input']['lyrics'] == '' and row['cover_status'] == 'DELIVERED' and world.slept[:2] == [2, 4]


def test_a_lane_that_stays_busy_ends_with_GPU_LANE_BUSY_told_once(pipeline, monkeypatch):
    world = pipeline
    monkeypatch.setattr(cover_chat, 'LANE_WAIT_SECONDS', 0)
    row = asked_row(world)
    Service(submits=[busy()] * 5).install(monkeypatch)
    send = Send()
    run_service(world, row, send, rounds=2)
    assert row['cover_status'] == 'FAILED' and len(send.texts) == 1 and 'GPU_LANE_BUSY' in send.texts[0]


def test_with_the_switch_off_nothing_runs_and_nothing_is_offered(world, monkeypatch):
    row = asked_row(world)
    monkeypatch.setattr(cover_chat, 'ENABLED', False)
    send = Send()
    run_service(world, row, send, rounds=2)
    assert row['cover_status'] == 'PENDING' and not send.files and not send.texts and world.orders == []
    assert asyncio.run(cover_chat.available({})) is False
    assert asyncio.run(cover_chat.offered(world.server, {'channel': 'qq'}, {})) == []


# --- the relay probe ------------------------------------------------------------------------------------------

def probe_against(handler, scenario):
    async def run():
        app = web.Application()
        app.router.add_post('/v1/songs/resolve', handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        try:
            return await scenario('http://127.0.0.1:%d' % site._server.sockets[0].getsockname()[1])
        finally:
            await runner.cleanup()
    return asyncio.run(run())


@pytest.mark.parametrize('status,body,ready', [
    (400, {'error_code': 'SONG_BAD_REQUEST', 'message': 'x'}, True),
    (404, {'error_code': 'SONG_NOT_FOUND'}, False), (404, 'Not Found', False),
    (503, {'error_code': 'SONG_NOT_AVAILABLE'}, False), (400, {'error_code': 'OTHER'}, False),
    (200, {'ok': True}, False), (500, 'oops', False)])
def test_the_probe_answers_only_for_a_relay_that_has_the_song_request(status, body, ready):
    async def handler(request):
        assert await request.json() == {}
        return web.json_response(body, status=status) if isinstance(body, dict) else web.Response(text=body, status=status)
    assert probe_against(handler, lambda base: song_resolver.probe(base, 'k')) is ready


def test_a_relay_that_does_not_answer_or_has_no_key_is_not_ready():
    assert asyncio.run(song_resolver.probe('http://127.0.0.1:9', 'k')) is False
    assert asyncio.run(song_resolver.probe('', '')) is False


def test_the_probe_is_remembered_for_ten_minutes_and_keeps_covers_off(monkeypatch):
    monkeypatch.setattr(cover_chat, 'ENABLED', True)
    cover_chat._probe.clear()
    calls = []
    answers = [False, True]

    async def probe(url, token):
        calls.append(1)
        return answers.pop(0)
    monkeypatch.setattr(song_resolver, 'probe', probe)

    async def caps(environment):
        return {'kinds': ['cover']}
    monkeypatch.setattr('runtime.media_capabilities.current', caps)
    env = {'OLIVIA_GPU_API_URL': 'https://gpu.example', 'OLIVIA_GPU_API_KEY': 'k'}
    clock = [1000.0]
    monkeypatch.setattr(cover_chat.time, 'monotonic', lambda: clock[0])
    assert asyncio.run(cover_chat.available(env)) is False and asyncio.run(cover_chat.available(env)) is False
    assert len(calls) == 1                                       # not asked again within the time
    clock[0] += cover_chat.PROBE_SECONDS + 1
    assert asyncio.run(cover_chat.available(env)) is True and len(calls) == 2
    assert asyncio.run(cover_chat.offered(SimpleNamespace(_local_data_root=lambda: None), {'channel': 'qq'}, env)) == []
    cover_chat._probe.clear()


def test_a_delivered_cover_is_read_by_the_memory_outbox_and_an_unsure_one_is_not(world, tmp_path):
    from runtime.memory.conversation_memory_outbox import CanonicalMemoryOutbox
    row = asked_row(world)
    run_service(world, row, Send())
    unsure = asked_row(world, 'turn-2')
    run_service(world, unsure, Send(file_failure=TimeoutError()))
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'personal_chats': world.rows}), encoding='utf-8')
    outbox = CanonicalMemoryOutbox.__new__(CanonicalMemoryOutbox)
    outbox.state_path = state
    remembered = [item for item in outbox._read_letters() if str(item.get('letter_id', '')).startswith('cover-')]
    assert len(remembered) == 1 and '晴天' in remembered[0]['reply_text'] and remembered[0]['origin'] == 'user'
