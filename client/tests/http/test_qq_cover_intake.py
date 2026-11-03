"""A song shared in QQ (card, link, B站 video) or sent as a file becomes a cover source she may choose to sing:
reading the share, asking the relay, keeping the audio, the writer's optional ``cover`` field, and the checks before
an order."""
import asyncio
import base64
import json
import time
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from aiohttp import web

from runtime.media import song_resolver
from runtime.media.song_resolver import SongError, resolve
from runtime.media.song_sources import KEEP_SECONDS, MAX_KEPT, SongSources
from runtime.personal_chat import cover_chat, decision, song_intake, song_shares
from runtime.personal_chat.events import PersonalMessage, owner_message

IDENTITY = 'ab' * 32


def card(value):
    return {'type': 'json', 'data': {'data': json.dumps(value, ensure_ascii=False)}}


def qq(*segments):
    return {'post_type': 'message', 'message_type': 'private', 'self_id': 100, 'user_id': 200, 'message_id': 7,
            'time': 1792780000, 'message': list(segments)}


def text_of(*segments):
    return owner_message('qq', qq(*segments), account_id='100', owner_id='200').text


NETEASE_CARD = {'app': 'com.tencent.structmsg', 'view': 'music', 'prompt': '[分享]晴天',
                'meta': {'music': {'tag': '网易云音乐', 'title': '晴天', 'desc': '周杰伦',
                                   'jumpUrl': 'https://y.music.163.com/m/song?id=186016&userid=1',
                                   'musicUrl': 'https://music.163.com/song/media/outer/url?id=186016.mp3'}}}
QQMUSIC_CARD = {'app': 'com.tencent.structmsg', 'view': 'music',
                'meta': {'music': {'tag': 'QQ音乐', 'title': '夜曲', 'desc': '周杰伦',
                                   'jumpUrl': 'https://i.y.qq.com/v8/playsong.html?songmid=000abc'}}}
BILIBILI_CARD = {'app': 'com.tencent.miniapp_01', 'prompt': '[QQ小程序]哔哩哔哩',
                 'meta': {'detail_1': {'title': '哔哩哔哩', 'desc': '肖邦夜曲 现场', 'qqdocurl': 'https://b23.tv/AbCdEf1'}}}


# --- reading a share --------------------------------------------------------------------------------------------

def test_a_netease_card_gives_the_song_id_title_and_singer():
    share = song_shares.find(text_of(card(NETEASE_CARD)))
    assert (share.kind, share.song_id, share.title, share.singer) == ('netease', '186016', '晴天', '周杰伦')
    assert share.request() == {'kind': 'netease', 'song_id': '186016', 'title': '晴天', 'singer': '周杰伦'}


def test_another_music_apps_card_is_found_by_title_and_singer():
    share = song_shares.find(text_of(card(QQMUSIC_CARD)))
    assert share.request() == {'kind': 'named', 'title': '夜曲', 'singer': '周杰伦'}


@pytest.mark.parametrize('text,expected', [
    ('分享周杰伦的单曲《晴天》: https://music.163.com/song?id=186016 (来自@网易云音乐)',
     {'kind': 'netease', 'song_id': '186016', 'title': '晴天', 'singer': '周杰伦'}),
    ('https://music.163.com/#/song?id=186016', {'kind': 'netease', 'song_id': '186016', 'title': '', 'singer': ''}),
    ('唱这个 https://y.music.163.com/m/song?id=33&uct=x', {'kind': 'netease', 'song_id': '33', 'title': '', 'singer': ''}),
    ('分享周杰伦的单曲《晴天》: https://163cn.tv/AbCd12', {'kind': 'named', 'title': '晴天', 'singer': '周杰伦'}),
])
def test_links_in_his_words(text, expected):
    assert song_shares.find(text).request() == expected


def test_bilibili_links_and_cards():
    video = song_shares.find('看这个 https://www.bilibili.com/video/BV1LDny6zESy/?spm=x')
    assert video.request() == {'kind': 'bilibili', 'bvid': 'BV1LDny6zESy', 'title': ''}
    short = song_shares.find(text_of(card(BILIBILI_CARD)))
    assert short.short_link == 'https://b23.tv/AbCdEf1' and short.bvid == '' and short.title == '肖邦夜曲 现场'
    read = song_shares.with_bvid(short, 'BV1LDny6zESy')
    assert read.request() == {'kind': 'bilibili', 'bvid': 'BV1LDny6zESy', 'title': '肖邦夜曲 现场'}
    assert read.key == 'bilibili:BV1LDny6zESy'


@pytest.mark.parametrize('text', ['今天天气不错', 'https://music.163.com/playlist?id=5', 'https://example.com/song?id=1',
                                  '[分享卡片：小红书 · 猫咪的一天] https://www.xiaohongshu.com/x/1',
                                  'https://music.163.com/song?id=abc'])
def test_what_is_not_a_song_is_not_taken(text):
    assert song_shares.find(text) is None


# --- the relay call ---------------------------------------------------------------------------------------------

def serve(handler, scenario):
    """Run ``scenario(base_url)`` against a loopback server answering POST /v1/songs/resolve with ``handler``."""
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


def test_resolve_saves_the_audio_and_reads_its_details(tmp_path):
    seen = {}

    async def handler(request):
        seen.update(body=await request.json(), auth=request.headers.get('Authorization'))
        return web.Response(body=b'ID3audio', headers={
            'X-Song-Title': quote('晴天'), 'X-Song-Singer': quote('周杰伦'), 'X-Song-Source': 'netease',
            'X-Song-Seconds': '215.5', 'X-Song-Identity': IDENTITY})
    target = tmp_path / 'song' / 'upload.bin'
    details = serve(handler, lambda base: resolve({'kind': 'netease', 'song_id': '1', 'title': '', 'singer': ''},
                                                  target, base, 'synthetic-key'))
    assert details == {'title': '晴天', 'singer': '周杰伦', 'source': 'netease', 'seconds': 215.5,
                       'identity': IDENTITY, 'bytes': 8}
    assert target.read_bytes() == b'ID3audio' and not target.with_name('upload.bin.part').exists()
    assert seen['body']['song_id'] == '1' and seen['auth'] == 'Bearer synthetic-key'


@pytest.mark.parametrize('status,code', [
    (404, 'SONG_NOT_FOUND'), (422, 'SONG_UNAVAILABLE'), (413, 'SONG_TOO_LONG'), (502, 'SONG_FETCH_FAILED'),
    (400, 'SONG_BAD_REQUEST'), (429, 'SONG_RATE_LIMITED'), (503, 'SONG_NOT_AVAILABLE')])
def test_each_error_code_reaches_the_caller_with_plain_words(tmp_path, status, code):
    async def handler(request):
        return web.json_response({'error_code': code, 'message': 'x'}, status=status)
    target = tmp_path / 'upload.bin'
    with pytest.raises(SongError) as raised:
        serve(handler, lambda base: resolve({'kind': 'named', 'title': 'a', 'singer': ''}, target, base, 'k'))
    assert raised.value.code == code and raised.value.status == status
    assert song_resolver.WORDS[code] in song_resolver.reason(code) and code in song_resolver.reason(code)
    assert not target.exists()


@pytest.mark.parametrize('status,body,code', [
    (404, b'not found', 'SONG_NOT_AVAILABLE'),            # a relay that does not know this call
    (401, b'{}', 'SONG_AUTH_FAILED'), (429, b'slow down', 'SONG_RATE_LIMITED'),
    (500, b'<html>', 'SONG_FETCH_FAILED'),
    (400, json.dumps({'error_code': 'SOMETHING_NEW'}).encode(), 'SONG_FETCH_FAILED')])
def test_errors_without_a_known_code_are_mapped_by_status(tmp_path, status, body, code):
    async def handler(request):
        return web.Response(body=body, status=status)
    with pytest.raises(SongError) as raised:
        serve(handler, lambda base: resolve({'kind': 'named', 'title': 'a', 'singer': ''}, tmp_path / 'u.bin', base, 'k'))
    assert raised.value.code == code


@pytest.mark.parametrize('headers', [
    {'X-Song-Source': 'spotify', 'X-Song-Identity': IDENTITY},
    {'X-Song-Source': 'netease', 'X-Song-Identity': 'nothex'},
    {'X-Song-Source': 'netease'}])
def test_a_200_without_valid_song_headers_is_a_failed_fetch(tmp_path, headers):
    async def handler(request):
        return web.Response(body=b'audio', headers=headers)
    with pytest.raises(SongError) as raised:
        serve(handler, lambda base: resolve({'kind': 'named', 'title': 'a', 'singer': ''}, tmp_path / 'u.bin', base, 'k'))
    assert raised.value.code == 'SONG_FETCH_FAILED' and not (tmp_path / 'u.bin').exists()


def test_not_configured_and_unreachable(tmp_path):
    with pytest.raises(SongError) as raised:
        asyncio.run(resolve({'kind': 'named', 'title': 'a', 'singer': ''}, tmp_path / 'u.bin', '', ''))
    assert raised.value.code == 'SONG_NOT_CONFIGURED'
    with pytest.raises(SongError) as raised:
        asyncio.run(resolve({'kind': 'named', 'title': 'a', 'singer': ''}, tmp_path / 'u.bin', 'http://127.0.0.1:9', 'k'))
    assert raised.value.code == 'SONG_SERVICE_UNREACHABLE'


def test_a_slow_relay_ends_as_a_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(song_resolver, 'CALL_SECONDS', 0.3)

    async def handler(request):
        await asyncio.sleep(2)
        return web.Response(body=b'late')
    with pytest.raises(SongError) as raised:
        serve(handler, lambda base: resolve({'kind': 'named', 'title': 'a', 'singer': ''}, tmp_path / 'u.bin', base, 'k'))
    assert raised.value.code == 'SONG_TIMEOUT'


# --- the kept sources -------------------------------------------------------------------------------------------

def keep(store, name='a', *, now=None, key=None, used=False):
    source_id = store.new_id()
    (store.folder(source_id)).mkdir(parents=True)
    (store.folder(source_id) / 'source.wav').write_bytes(b'RIFF')
    row = store.add(source_id, key=key or name, title=name, singer='s', origin='netease', identity=IDENTITY,
                    seconds=100, now=now)
    if used:
        store.mark_used(source_id, now=now)
    return row


def test_sources_are_indexed_found_by_key_and_marked_used(tmp_path):
    store = SongSources(tmp_path)
    first, second = keep(store, 'a', now=1000), keep(store, 'b', now=2000)
    assert [row['title'] for row in store.unused(limit=5, now=2500)] == ['b', 'a']
    assert store.get(first['id'])['title'] == 'a' and store.get('nope') is None
    assert store.find_key('b', now=2500)['id'] == second['id']
    assert store.mark_used(first['id'], now=2600) and not store.mark_used('x' * 32)
    assert [row['title'] for row in store.unused(limit=5, now=2700)] == ['b']
    assert SongSources(tmp_path).get(first['id'])['used'] is True      # durable


def test_unused_sources_go_after_three_days_and_so_do_used_ones(tmp_path):
    store = SongSources(tmp_path)
    old, fresh = keep(store, 'old', now=1000), keep(store, 'fresh', now=1000 + KEEP_SECONDS)
    spent = keep(store, 'spent', now=1000, used=True)
    now = 1000 + KEEP_SECONDS + 5
    assert store.find_key('old', now=now) is None            # too old to be reused
    assert sorted(store.cleanup(now=now)) == sorted([old['id'], spent['id']])
    assert not store.folder(old['id']).exists() and store.folder(fresh['id']).exists()
    assert store.get(fresh['id']) and store.get(old['id']) is None


def test_the_store_keeps_at_most_a_few_unused_songs_and_forgets_a_vanished_folder(tmp_path):
    store = SongSources(tmp_path)
    rows = [keep(store, f's{i}', now=1000 + i) for i in range(MAX_KEPT + 3)]
    gone = store.cleanup(now=2000)
    assert sorted(gone) == sorted(row['id'] for row in rows[:3])
    import shutil
    shutil.rmtree(store.folder(rows[5]['id']))
    assert rows[5]['id'] in store.cleanup(now=2000)


# --- songs arriving in a turn ----------------------------------------------------------------------------------

@pytest.fixture
def intake(tmp_path, monkeypatch):
    monkeypatch.setenv('OLIVIA_GPU_API_URL', 'https://gpu.example')
    monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'synthetic-key')
    monkeypatch.setattr(cover_chat, 'ENABLED', True)
    calls = SimpleNamespace(resolve=[], sent=[], validated=[])

    def validate(raw, destination, environment):
        data = raw.read_bytes()
        if data == b'broken':
            raw.unlink(missing_ok=True)
            raise ValueError('COVER_AUDIO_INVALID')
        destination.write_bytes(b'RIFF' + data)
        raw.unlink()
        calls.validated.append(data)
        return 700.0 if data == b'long' else 200.0
    monkeypatch.setattr('runtime.media.cover_upload.validate_upload', validate)

    async def fake_resolve(request, target, url, token):
        calls.resolve.append(request)
        if isinstance(calls.outcome, SongError):
            raise calls.outcome
        target.write_bytes(calls.outcome)
        return {'title': '晴天', 'singer': '周杰伦', 'source': 'netease', 'seconds': 200.0, 'identity': IDENTITY,
                'bytes': len(calls.outcome)}
    calls.outcome = b'audio'
    monkeypatch.setattr(song_resolver, 'resolve', fake_resolve)

    async def caps(environment):
        return {'kinds': ['cover', 'tts'], 'shared_assets': []}
    monkeypatch.setattr('runtime.media_capabilities.current', caps)
    server = SimpleNamespace(_local_data_root=lambda: tmp_path, _persist_store_state=lambda: None)

    async def send(text):
        calls.sent.append(text)
        return 'ack'
    calls.server, calls.send, calls.root = server, send, tmp_path
    return calls


def run_turn(intake, text, row=None, **extra):
    row = {'letter_id': 'turn-1', 'channel': 'qq', **(row or {})}
    event = PersonalMessage('qq', '100', '200', '7', text)

    async def scenario():
        token = song_intake.QUICK_SEND.set(intake.send)
        try:
            await song_intake.receive_shares(intake.server, event, row)
        finally:
            song_intake.QUICK_SEND.reset(token)
    asyncio.run(scenario())
    return row


def test_a_shared_card_is_fetched_kept_and_announced_to_the_writer(intake):
    row = run_turn(intake, text_of(card(NETEASE_CARD)))
    [arrival] = row['song_arrivals']
    assert arrival['title'] == '晴天' and arrival['singer'] == '周杰伦' and arrival['seconds'] == 200
    assert intake.resolve == [{'kind': 'netease', 'song_id': '186016', 'title': '晴天', 'singer': '周杰伦'}]
    store = SongSources(intake.root)
    kept = store.get(arrival['source'])
    assert kept['origin'] == 'netease' and kept['identity'] == IDENTITY
    assert (store.folder(arrival['source']) / 'source.wav').read_bytes() == b'RIFFaudio'
    assert not (store.folder(arrival['source']) / 'upload.bin').exists()
    assert len(intake.sent) == 1 and row['song_listening_line'] == intake.sent[0]     # one short line first


def test_the_same_share_again_is_not_fetched_twice_and_one_turn_reads_once(intake):
    text = text_of(card(NETEASE_CARD))
    first = run_turn(intake, text)
    second = run_turn(intake, text)
    assert len(intake.resolve) == 1 and second['song_arrivals'] == first['song_arrivals']
    assert len(intake.sent) == 1                                  # the kept song needed no waiting line
    run_turn(intake, text, row=second)
    assert len(second['song_arrivals']) == 1


def test_a_b23_short_link_is_read_before_the_relay_is_asked(intake, monkeypatch):
    async def bvid(share):
        assert share.short_link == 'https://b23.tv/AbCdEf1'
        return 'BV1LDny6zESy'
    monkeypatch.setattr(song_intake, '_short_bvid', bvid)
    row = run_turn(intake, text_of(card(BILIBILI_CARD)))
    assert intake.resolve == [{'kind': 'bilibili', 'bvid': 'BV1LDny6zESy', 'title': '肖邦夜曲 现场'}]
    assert len(row['song_arrivals']) == 1

    async def broken(share):
        raise RuntimeError('BILIBILI_LINK_UNRESOLVED')
    monkeypatch.setattr(song_intake, '_short_bvid', broken)
    row = run_turn(intake, text_of(card(BILIBILI_CARD)).replace('AbCdEf1', 'Zz9Yy8X'))
    assert row['song_failures'][0]['code'] == 'SONG_LINK_UNREADABLE' and 'song_arrivals' not in row


@pytest.mark.parametrize('code', ['SONG_NOT_FOUND', 'SONG_UNAVAILABLE', 'SONG_TOO_LONG', 'SONG_FETCH_FAILED',
                                  'SONG_BAD_REQUEST', 'SONG_RATE_LIMITED', 'SONG_NOT_AVAILABLE', 'SONG_TIMEOUT'])
def test_a_failed_fetch_is_told_with_a_plain_reason_and_the_code(intake, code):
    intake.outcome = SongError(code)
    row = run_turn(intake, text_of(card(NETEASE_CARD)))
    [failure] = row['song_failures']
    assert failure['what'] == '晴天' and failure['code'] == code and code in failure['reason']
    assert song_resolver.WORDS[code] in failure['reason'] and 'song_arrivals' not in row
    assert not list((intake.root / 'cover-inputs').glob('*/'))      # nothing half-kept
    note = cover_chat.context({**row, 'cover_sources': []})
    assert code in note and '系统歌曲' in note


def test_audio_the_machine_cannot_open_or_that_is_too_long_is_refused(intake):
    intake.outcome = b'broken'
    row = run_turn(intake, text_of(card(NETEASE_CARD)))
    assert row['song_failures'][0]['code'] == 'SONG_AUDIO_INVALID'
    intake.outcome = b'long'
    row = run_turn(intake, text_of(card(QQMUSIC_CARD)))
    assert row['song_failures'][0]['code'] == 'SONG_TOO_LONG'
    assert not list((intake.root / 'cover-inputs').glob('*/source.wav'))


def test_nothing_happens_without_a_share_or_without_the_cloud_key(intake, monkeypatch):
    assert 'song_arrivals' not in run_turn(intake, '今天好冷')
    monkeypatch.delenv('OLIVIA_GPU_API_KEY')
    row = run_turn(intake, text_of(card(NETEASE_CARD)))
    assert 'song_arrivals' not in row and 'song_failures' not in row and intake.resolve == []


# --- audio files sent in the chat -------------------------------------------------------------------------------

def run_file(intake, name, data, *, row=None, reference='file-1', resolver=None):
    row = row if row is not None else {'letter_id': 'turn-2', 'channel': 'qq'}
    event = PersonalMessage('qq', '100', '200', '8', '[文件]')

    async def default(kind, reference):
        assert kind == 'file'
        return {'base64': base64.b64encode(data).decode()}

    async def scenario():
        from runtime.incoming_media import MEDIA_RESOLVER
        token = MEDIA_RESOLVER.set(resolver or default)
        try:
            return await song_intake.receive_audio_file(intake.server, event, row, '8', reference, name)
        finally:
            MEDIA_RESOLVER.reset(token)
    return asyncio.run(scenario()), row


@pytest.mark.parametrize('name', ['晴天.mp3', 'a.WAV', 'a.flac', 'a.m4a', 'a.aac', 'a.ogg'])
def test_audio_files_are_kept_as_cover_sources(intake, name):
    handled, row = run_file(intake, name, b'sound')
    assert handled is True
    [arrival] = row['song_arrivals']
    assert arrival['title'] == name.rsplit('.', 1)[0] and arrival['singer'] == ''
    assert SongSources(intake.root).get(arrival['source'])['origin'] == 'file'


def test_the_same_file_twice_is_kept_once_and_other_files_are_left_alone(intake):
    handled, row = run_file(intake, 'a.mp3', b'sound')
    again, row = run_file(intake, 'a.mp3', b'sound', row=row)
    assert again is True and len(row['song_arrivals']) == 1
    other, row2 = run_file(intake, 'a.mp3', b'sound', reference='file-2')
    assert other is True and row2['song_arrivals'][0]['source'] == row['song_arrivals'][0]['source']
    assert len(list((intake.root / 'cover-inputs').glob('*/source.wav'))) == 1
    for name in ('notes.pdf', 'song.mid', 'a.txt', 'noext'):
        assert run_file(intake, name, b'x')[0] is False


def test_a_file_that_is_too_large_or_unreadable_is_reported_with_its_reason(intake, monkeypatch):
    monkeypatch.setattr(song_intake, 'MAX_FILE_BYTES', 4)
    handled, row = run_file(intake, 'a.mp3', b'too many bytes')
    assert handled is True and row['song_failures'][0]['code'] == 'SONG_FILE_TOO_LARGE'
    assert '40 MB' in row['song_failures'][0]['reason']

    async def refuse(kind, reference):
        raise RuntimeError('QQ_MEDIA_DOWNLOAD_FAILED')
    handled, row = run_file(intake, 'b.mp3', b'x', resolver=refuse)
    assert row['song_failures'][0]['code'] == 'SONG_FILE_UNAVAILABLE'
    monkeypatch.setattr(song_intake, 'MAX_FILE_BYTES', 1024)
    handled, row = run_file(intake, 'c.mp3', b'broken')
    assert row['song_failures'][0]['code'] == 'SONG_AUDIO_INVALID'
    assert not list((intake.root / 'cover-inputs').glob('*/'))


def test_with_covers_off_an_audio_file_takes_the_ordinary_path(intake, monkeypatch):
    monkeypatch.setattr(cover_chat, 'ENABLED', False)
    assert run_file(intake, 'a.mp3', b'sound')[0] is False


def test_an_audio_file_in_a_turn_is_taken_by_the_media_step(intake, monkeypatch):
    from runtime import incoming_media

    async def midi(*args):
        return False
    monkeypatch.setattr('runtime.personal_chat.piano.receive_midi', midi)
    event = PersonalMessage('qq', '100', '200', '9', '[文件]', input_kind='file', media=(('9', 'file', 'ref-1', 'x.mp3'),))
    row = {'letter_id': 'turn-3', 'channel': 'qq'}

    async def resolver(kind, reference):
        return {'base64': base64.b64encode(b'sound').decode()}

    async def scenario():
        token = incoming_media.MEDIA_RESOLVER.set(resolver)
        try:
            await incoming_media.understand_incoming(intake.server, event, row)
        finally:
            incoming_media.MEDIA_RESOLVER.reset(token)
    asyncio.run(scenario())
    assert len(row['song_arrivals']) == 1 and row['incoming_media_status'] == 'COMPLETED'
    assert row['incoming_media_failed'] == 0 and not row.get('incoming_media_observations')


# --- what the writer is offered and may answer -----------------------------------------------------------------

def test_the_writer_is_offered_this_turns_songs_first_then_earlier_unused_ones(intake):
    store = SongSources(intake.root)
    earlier = keep(store, 'earlier')
    used = keep(store, 'used', used=True)
    row = run_turn(intake, text_of(card(NETEASE_CARD)))
    offered = asyncio.run(cover_chat.offered(intake.server, row, {}))
    assert [(song['title'], song['arrived']) for song in offered] == [('晴天', 'this_turn'), ('earlier', 'earlier')]
    assert all(len(song['id']) == 8 and song['source'].startswith(song['id']) for song in offered)
    assert used['id'] not in [song['source'] for song in offered]
    assert row['cover_sources'] is offered                          # frozen for the turn
    note = cover_chat.context(row)
    assert note.startswith('\n[系统歌曲，非用户原话]') and '"id"' in note and 'source' not in json.loads(
        note.split('\n', 2)[2])['songs'][0]
    assert row['song_listening_line'] in note


def test_nothing_is_offered_when_covers_are_off_or_not_offered_by_the_service(intake, monkeypatch):
    row = {'channel': 'qq'}
    keep(SongSources(intake.root), 'earlier')
    monkeypatch.setattr(cover_chat, 'ENABLED', False)
    assert asyncio.run(cover_chat.offered(intake.server, dict(row), {})) == []
    monkeypatch.setattr(cover_chat, 'ENABLED', True)

    async def caps(environment):
        return {'kinds': ['tts']}
    monkeypatch.setattr('runtime.media_capabilities.current', caps)
    assert asyncio.run(cover_chat.offered(intake.server, dict(row), {})) == []
    assert cover_chat.context({}) == ''


OFFERED = [{'id': 'abcd1234', 'source': 'abcd1234' + '0' * 24, 'title': '晴天', 'singer': '周杰伦', 'seconds': 200,
            'arrived': 'this_turn'}]


def envelope(**extra):
    return json.dumps({'text': '好呀，我唱给你听。', 'delivery': 'text', 'text_reason': None, 'listening': 'keep',
                       'initiative': 'keep', 'pause_until': None, 'letter': 'keep', 'letter_until': None,
                       'followup_at': None, 'evidence': '', 'sticker': None, 'skip': False, **extra}, ensure_ascii=False)


def decode(user, cover=None, sources=OFFERED, **options):
    extra = {} if cover is None else {'cover': cover}
    return decision.decode(envelope(**extra), user=user, now=time.time(), cover_sources=sources, **options)


def test_a_cover_the_user_asked_for_in_his_words_becomes_a_request():
    user = '[分享卡片：网易云音乐 · 晴天 · 周杰伦] https://y.music.163.com/m/song?id=1 你翻唱一下这首歌吧！'
    result = decode(user, {'source': 'abcd1234', 'evidence': '你翻唱一下这首歌吧'})
    assert result['cover_request'] == {'source': 'abcd1234' + '0' * 24, 'title': '晴天', 'evidence': '你翻唱一下这首歌吧'}
    assert 'cover' not in result and 'dropped_cover' not in result


def test_the_quote_is_compared_without_punctuation_spaces_or_case():
    result = decode('Sing it,  please！！', {'source': 'abcd1234', 'evidence': 'sing it please'})
    assert result['cover_request']['source'].startswith('abcd1234')


@pytest.mark.parametrize('user,cover,reason', [
    ('在吗', {'source': 'abcd1234', 'evidence': '你翻唱一下'}, 'COVER_EVIDENCE_NOT_QUOTED'),         # not his words
    ('你翻唱一下', {'source': 'ffffffff', 'evidence': '你翻唱一下'}, 'COVER_SOURCE_NOT_OFFERED'),     # not an offered song
    ('你翻唱一下', {'source': 'abcd1234', 'evidence': ''}, 'COVER_EVIDENCE_NOT_QUOTED'),
    ('你翻唱一下', {'source': 'abcd1234', 'evidence': '唱'}, 'COVER_EVIDENCE_NOT_QUOTED'),           # too short to prove
    ('[分享卡片：网易云音乐 · 晴天 · 周杰伦]', {'source': 'abcd1234', 'evidence': '网易云音乐 晴天 周杰伦'},
     'COVER_EVIDENCE_NOT_QUOTED'),                                                                     # a card is not a request
    ('你翻唱一下', {'source': 'abcd1234'}, 'COVER_REQUEST_SHAPE'),
    ('你翻唱一下', {'source': 'abcd1234', 'evidence': '你翻唱一下', 'extra': 1}, 'COVER_REQUEST_SHAPE'),
    ('你翻唱一下', 'yes', 'COVER_REQUEST_SHAPE'),
    ('你翻唱一下', {'source': 7, 'evidence': '你翻唱一下'}, 'COVER_REQUEST_SHAPE')])
def test_an_unquoted_or_unoffered_cover_is_dropped_and_the_reply_still_goes_out(user, cover, reason):
    result = decode(user, cover)
    assert result['text'] == '好呀，我唱给你听。'
    assert 'cover_request' not in result and result['dropped_cover'] == reason


def test_without_offered_songs_or_in_a_proactive_turn_a_cover_is_dropped():
    assert decode('你翻唱一下', {'source': 'abcd1234', 'evidence': '你翻唱一下'}, sources=())['dropped_cover']
    result = decode('', {'source': 'abcd1234', 'evidence': '你翻唱一下'}, proactive=True)
    assert 'cover_request' not in result and result['dropped_cover'] == 'COVER_REQUEST_CONTEXT'


def test_a_reply_without_cover_is_unchanged_and_the_field_is_known_to_old_clients_only_as_optional():
    result = decode('你翻唱一下')
    assert 'cover_request' not in result and 'dropped_cover' not in result
    assert 'cover' in decision.OPTIONAL_FIELDS
    assert 'cover' not in decision.NEUTRAL_METADATA            # the required fields did not change
    assert '"cover"' not in decision.INSTRUCTION.split('字段必须完整')[1].split('。')[0]


# --- the checks before an order -------------------------------------------------------------------------------

CAPS = {'kinds': ['cover', 'tts']}
ACCOUNT = {'mode': 'money', 'balance_cents': 500}


def preflight(**changes):
    facts = dict(capabilities=CAPS, sings_covers=True, account=ACCOUNT, letter_active=False, cover_active=False)
    return cover_chat.cover_preflight(**{**facts, **changes})


@pytest.mark.parametrize('changes,code', [
    ({}, ''),
    ({'account': None}, ''),                                                # an unread account does not block
    ({'account': {'mode': 'simulation', 'balance_cents': 0}}, ''),
    ({'capabilities': None}, 'GPU_CAPABILITY_UNAVAILABLE'),
    ({'capabilities': {'kinds': ['tts']}}, 'GPU_CAPABILITY_UNAVAILABLE'),
    ({'capabilities': {'kinds': ['cover'], 'night_batch': {'kinds': ['cover']}}}, 'COVER_NIGHT_ONLY'),
    ({'sings_covers': False}, 'COVER_ENGINE_UNAVAILABLE'),
    ({'account': {'mode': 'money', 'balance_cents': 0}}, 'GPU_INSUFFICIENT_BALANCE'),
    ({'account': {'mode': 'money', 'balance_cents': -3}}, 'GPU_INSUFFICIENT_BALANCE'),
    ({'letter_active': True}, 'LETTER_IN_PROGRESS'),
    ({'cover_active': True}, 'COVER_IN_PROGRESS')])
def test_preflight(changes, code):
    ok, reason, words = preflight(**changes)
    assert (ok, reason) == (code == '', code) and bool(words) == (code != '')


def test_preflight_words_follow_the_interface_language(monkeypatch):
    from runtime import ui_language
    assert '回信' in preflight(letter_active=True)[2]
    monkeypatch.setattr(ui_language, 'current', lambda: ui_language.ENGLISH)
    for changes in ({'letter_active': True}, {'cover_active': True}, {'sings_covers': False},
                    {'account': {'mode': 'money', 'balance_cents': 0}}, {'capabilities': None}):
        assert not any('一' <= ch <= '鿿' for ch in preflight(**changes)[2])


def test_failure_words_name_the_reason_and_the_code():
    from runtime.cloud_service import CloudError
    error = CloudError('GPU_TASK_FAILED', 502)
    error.detail = 'YUE2_TEMPO_OUT_OF_RANGE'
    assert '错误码 YUE2_TEMPO_OUT_OF_RANGE' in cover_chat.failure_words(error)
    assert '余额不足' in cover_chat.failure_words(CloudError('GPU_INSUFFICIENT_BALANCE', 402))
    assert '错误码 GPU_LANE_BUSY' in cover_chat.failure_words(CloudError('GPU_LANE_BUSY', 429))
    assert '错误码 COVER_FAILED' in cover_chat.failure_words(ValueError('weird text'))


def test_his_words_right_after_a_cards_link_still_count_as_his_words():
    user = text_of(card(NETEASE_CARD), {'type': 'text', 'data': {'text': '你翻唱这首歌吧'}})
    assert 'userid=1你翻唱' in user      # the card's link and his words arrive without a space between them
    result = decode(user, {'source': 'abcd1234', 'evidence': '你翻唱这首歌吧'})
    assert result['cover_request']['evidence'] == '你翻唱这首歌吧'
