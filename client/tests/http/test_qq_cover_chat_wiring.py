"""The cover path inside a QQ turn: the song a card brings reaches the writer, the writer's request is checked against
the user's words and kept on the row, and the delivery step is only reached when the feature is switched on."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from runtime.media import song_resolver
from runtime.media.song_sources import SongSources
from runtime.personal_chat import backend, cover_chat, song_intake
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService, _clear_draft
from tests.http.test_chat_jev_decision import server_fixture, pipeline_result
from tests.http.test_personal_chat_decision import envelope
from tests.http.test_qq_cover_intake import IDENTITY, NETEASE_CARD, card, text_of


@pytest.fixture
def covers(tmp_path, monkeypatch):
    monkeypatch.setenv('OLIVIA_GPU_API_URL', 'https://gpu.example')
    monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'synthetic-key')
    monkeypatch.setattr(cover_chat, 'ENABLED', True)

    async def ready(environment):
        return True
    monkeypatch.setattr(cover_chat, 'relay_ready', ready)

    def validate(raw, destination, environment):
        destination.write_bytes(b'RIFF' + raw.read_bytes())
        raw.unlink()
        return 200.0
    monkeypatch.setattr('runtime.media.cover_upload.validate_upload', validate)

    async def fetch(request, target, url, token):
        target.write_bytes(b'audio')
        return {'title': '晴天', 'singer': '周杰伦', 'source': 'netease', 'seconds': 200.0, 'identity': IDENTITY, 'bytes': 5}
    monkeypatch.setattr(song_resolver, 'resolve', fetch)

    async def caps(environment):
        return {'kinds': ['cover'], 'shared_assets': []}
    monkeypatch.setattr('runtime.media_capabilities.current', caps)
    sent = []

    async def quick(text):
        sent.append(text)
        return 'ack'
    return SimpleNamespace(tmp=tmp_path, sent=sent, quick=quick)


def test_a_shared_song_is_shown_to_the_writer_and_a_quoted_request_is_kept(monkeypatch, covers):
    user = text_of(card(NETEASE_CARD)) + '你翻唱这首歌吧'
    server, row, seen, _, _ = server_fixture(monkeypatch, covers.tmp, pipeline_result(text='{}'))
    server._local_data_root = lambda: covers.tmp
    row['letter_id'] = 'turn-1'

    async def run(request, context):
        from runtime.personal_chat.presentation import CURRENT
        shown = CURRENT.get()
        seen.append(deepcopy(shown))
        offered = shown['cover_sources']
        assert len(offered) == 1 and offered[0]['title'] == '晴天' and offered[0]['arrived'] == 'this_turn'
        assert '系统歌曲' in shown['incoming_observation_context'] and '晴天' in shown['incoming_observation_context']
        return pipeline_result(text=envelope(text='好呀，我唱给你听，等我一会儿。',
                                             cover={'source': offered[0]['id'], 'evidence': '你翻唱这首歌吧'}))
    server.reply_pipeline.run = run

    async def scenario():
        token = song_intake.QUICK_SEND.set(covers.quick)
        try:
            return await backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', user), row)
        finally:
            song_intake.QUICK_SEND.reset(token)
    text = asyncio.run(scenario())
    assert text == '好呀，我唱给你听，等我一会儿。'
    source = SongSources(covers.tmp).unused(limit=1)[0]['id']
    assert row['cover_request'] == {'source': source, 'title': '晴天', 'evidence': '你翻唱这首歌吧'}
    assert row['cover_status'] == 'PENDING' and len(covers.sent) == 1
    assert '"cover"' not in json.dumps({k: v for k, v in row.items() if k != 'cover_request'})


def test_a_request_that_quotes_nothing_the_user_said_is_dropped_but_the_reply_is_sent(monkeypatch, covers):
    user = text_of(card(NETEASE_CARD)) + '好听吧'
    server, row, seen, _, _ = server_fixture(monkeypatch, covers.tmp, pipeline_result(text='{}'))
    server._local_data_root = lambda: covers.tmp
    row['letter_id'] = 'turn-1'

    async def run(request, context):
        from runtime.personal_chat.presentation import CURRENT
        offered = CURRENT.get()['cover_sources']
        return pipeline_result(text=envelope(text='真好听。', cover={'source': offered[0]['id'], 'evidence': '你翻唱这首歌吧'}))
    server.reply_pipeline.run = run
    text = asyncio.run(backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', user), row))
    assert text == '真好听。' and 'cover_request' not in row
    assert row['cover_request_dropped'] == 'COVER_EVIDENCE_NOT_QUOTED'


def test_with_the_feature_off_nothing_about_songs_reaches_the_writer(monkeypatch, covers):
    monkeypatch.setattr(cover_chat, 'ENABLED', False)
    server, row, seen, _, _ = server_fixture(monkeypatch, covers.tmp, pipeline_result())
    server._local_data_root = lambda: covers.tmp
    asyncio.run(backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', text_of(card(NETEASE_CARD))), row))
    assert seen[0]['cover_sources'] == [] and '系统歌曲' not in seen[0]['incoming_observation_context']
    assert 'song_arrivals' not in row and not (covers.tmp / 'cover-inputs').exists() and covers.sent == []


def test_a_turn_without_a_song_offers_nothing(monkeypatch, covers):
    server, row, seen, _, _ = server_fixture(monkeypatch, covers.tmp, pipeline_result())
    server._local_data_root = lambda: covers.tmp
    asyncio.run(backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', '你好'), row))
    assert seen[0]['cover_sources'] == [] and '系统歌曲' not in seen[0]['incoming_observation_context']


def test_a_new_draft_forgets_the_cover_request_but_not_the_songs_that_arrived():
    row = {'content': 'new', 'cover_request': {'source': 'x'}, 'cover_status': 'PENDING', 'cover_sources': [{}],
           'cover_request_dropped': 'X', 'song_arrivals': [{'source': 'a'}], 'song_failures': [], 'song_share_read': True}
    _clear_draft(row)
    assert row == {'content': 'new', 'song_arrivals': [{'source': 'a'}], 'song_failures': [], 'song_share_read': True}


# --- the delivery step is reached only through the switch -------------------------------------------------------

def service_with(cover, rows):
    async def generate(event, row):
        return 'x'

    async def commit(row):
        pass
    return PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('b', 'u')}, cover=cover)


def delivered_row(**extra):
    from runtime.personal_chat.events import PersonalMessage as Event
    return {'letter_id': 'turn-9', 'channel': 'qq', 'delivery_status': 'DELIVERED', 'letter_status': 'COMPLETED',
            'binding_id': Event('qq', 'b', 'u', '', '').binding_id, 'cover_request': {'source': 'a' * 32, 'title': 't'},
            'cover_status': 'PENDING', **extra}


class Send:
    def __init__(self):
        self.texts = []

    async def __call__(self, text):
        self.texts.append(text)
        return 'ack'

    async def file(self, path, name):
        return 'file-ack'


@pytest.mark.parametrize('enabled', [False, True])
def test_the_cover_hook_runs_only_when_the_feature_is_on(monkeypatch, enabled):
    monkeypatch.setattr(cover_chat, 'ENABLED', enabled)
    calls = []

    async def cover(row, send):
        calls.append(row['cover_request']['source'])
        row['cover_status'] = 'DELIVERED'
    row, send = delivered_row(), Send()

    async def scenario():
        service = service_with(cover, [row])
        service.resume_speech('qq', send)
        await asyncio.gather(*service.speech_tasks.values())
        return service
    asyncio.run(scenario())
    assert calls == (['a' * 32] if enabled else [])


def test_a_failed_cover_is_told_at_once_with_its_reason_and_code_and_not_retried(monkeypatch):
    monkeypatch.setattr(cover_chat, 'ENABLED', True)
    from runtime.cloud_service import CloudError
    calls = []

    async def cover(row, send):
        calls.append(1)
        error = CloudError('GPU_TASK_FAILED', 502)
        error.detail = 'YUE2_TEMPO_OUT_OF_RANGE'
        raise error
    row, send = delivered_row(), Send()

    async def scenario():
        service = service_with(cover, [row])
        service.resume_speech('qq', send)
        await asyncio.gather(*service.speech_tasks.values())
        service.resume_speech('qq', send)
        assert not service.speech_tasks
    asyncio.run(scenario())
    assert calls == [1] and row['cover_status'] == 'FAILED' and row['cover_error_code'] == 'YUE2_TEMPO_OUT_OF_RANGE'
    assert len(send.texts) == 1 and 'YUE2_TEMPO_OUT_OF_RANGE' in send.texts[0]


def _generate(server, row, text):
    return asyncio.run(backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', text), row))


def test_one_slow_capabilities_answer_does_not_switch_covers_off_for_a_merged_card_and_request(monkeypatch, covers):
    asked = []

    async def caps(environment, retry=False):
        asked.append(retry)
        return {'kinds': ['cover']} if retry else None  # the first read times out, the second one answers
    monkeypatch.setattr('runtime.media_capabilities.current', caps)
    user = text_of(card(NETEASE_CARD)) + '\n我想听你唱这首歌'   # two QQ messages merged into one turn
    server, row, seen, _, _ = server_fixture(monkeypatch, covers.tmp, pipeline_result())
    server._local_data_root = lambda: covers.tmp
    _generate(server, row, user)
    assert asked[:2] == [False, True]
    assert [item['title'] for item in seen[0]['cover_sources']] == ['晴天'] and 'cover_skip' not in row


def test_a_song_shared_while_covers_are_unavailable_is_told_with_its_code_and_picked_up_later(monkeypatch, covers):
    async def no_caps(environment, retry=False):
        return None
    monkeypatch.setattr('runtime.media_capabilities.current', no_caps)
    server, row, seen, _, _ = server_fixture(monkeypatch, covers.tmp, pipeline_result())
    server._local_data_root = lambda: covers.tmp
    _generate(server, row, text_of(card(NETEASE_CARD)) + '\n你唱这首吧')
    assert row['cover_skip'] == 'COVER_CAPABILITIES_UNREAD' and seen[0]['cover_sources'] == []
    note = seen[0]['incoming_observation_context']
    assert '系统歌曲' in note and 'COVER_CAPABILITIES_UNREAD' in note and 'song_arrivals' not in row
    assert (covers.tmp / 'cover-inputs' / song_intake.PENDING_FILE).exists()

    async def caps(environment, retry=False):
        return {'kinds': ['cover']}
    monkeypatch.setattr('runtime.media_capabilities.current', caps)
    server2, row2, seen2, _, _ = server_fixture(monkeypatch, covers.tmp, pipeline_result())
    server2._local_data_root = lambda: covers.tmp
    _generate(server2, row2, '怎么样了')
    offered = seen2[0]['cover_sources']
    assert [(item['title'], item['arrived']) for item in offered] == [('晴天', 'earlier')]
    assert not (covers.tmp / 'cover-inputs' / song_intake.PENDING_FILE).exists()


def test_an_order_in_flight_and_a_request_that_never_became_an_order_are_shown_to_the_writer(monkeypatch, covers):
    server, row, seen, _, _ = server_fixture(monkeypatch, covers.tmp, pipeline_result())
    server._local_data_root = lambda: covers.tmp
    binding = PersonalMessage('qq', 'b', 'u', '', '').binding_id
    row['binding_id'] = binding
    server.store.personal_chats[:] = [row, {'channel': 'qq', 'binding_id': binding, 'cover_status': 'MAKING', 'cover_title': '什么'}]
    _generate(server, row, '好了吗')
    assert row['cover_orders'] == [{'what': '翻唱', 'title': '什么', 'state': '制作中'}]
    assert '"orders"' in seen[0]['incoming_observation_context'] and '什么' in seen[0]['incoming_observation_context']
    import time
    server.store.personal_chats[:] = [row, {'channel': 'qq', 'binding_id': binding, 'created_at': time.time(),
                                            'cover_request_dropped': 'COVER_EVIDENCE_NOT_QUOTED'}]
    assert cover_chat.in_flight(server, row) == [{'what': '翻唱', 'state': '没有成功下单', 'code': 'COVER_EVIDENCE_NOT_QUOTED'}]
