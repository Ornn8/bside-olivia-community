"""B站 links he shares: found in his words, read and watched once per turn, and told to her as an observation."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from runtime.personal_chat import bilibili


def test_links_are_found_in_his_text_and_cards():
    text = ('[分享卡片：哔哩哔哩 · 夜曲] https://b23.tv/AbCd123 再看 https://www.bilibili.com/video/BV1LDny6zESy/?spm=x '
            'https://b23.tv/AbCd123 https://m.bilibili.com/video/BV1GJ411x7h7 http://evil.example/video/BV1GJ411x7h7')
    assert bilibili.video_links(text) == ['https://b23.tv/AbCd123', 'https://www.bilibili.com/video/BV1LDny6zESy']
    assert bilibili.video_links('今天好累') == [] and bilibili.video_links(None) == []


def test_requests_are_signed_the_way_bilibili_expects():
    img = 'https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png'
    sub = 'https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png'
    mixin = bilibili.mixin_key(img, sub)
    assert len(mixin) == 32
    signed = bilibili.sign({'foo': '114', 'bar': '514', 'zab': 1919810}, mixin, now=1702204169)
    # The worked example in the community API notes: same mixin key and w_rid.
    assert mixin == 'ea1db124af3c7062474693fa704f4ff8'
    assert signed == 'bar=514&foo=114&wts=1702204169&zab=1919810&w_rid=8f6f2b5b3d485fe1886cec6a0be8c5d4'
    with pytest.raises(ValueError):
        bilibili.mixin_key('https://x/short.png', 'https://x/y.png')


def run(row, text, *, video=None, watched=None, fail_watch=False):
    async def read_video(http, link):
        if video is None:
            raise RuntimeError('BILIBILI_REFUSED')
        return dict(video)
    async def watch(server, http, item, row_):
        if fail_watch:
            raise RuntimeError('BILIBILI_STREAM_UNREADABLE')
        return watched
    class Http:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False
        def get(self, url):
            class Response:
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *exc):
                    return False
                async def read(self):
                    return b''
            return Response()
    original = (bilibili.read_video, bilibili._watch, bilibili.aiohttp.ClientSession)
    bilibili.read_video, bilibili._watch = read_video, watch
    bilibili.aiohttp.ClientSession = lambda **kwargs: Http()
    try:
        server = SimpleNamespace(_persist_store_state=lambda: None)
        return asyncio.run(bilibili.link_context(server, SimpleNamespace(text=text), row))
    finally:
        bilibili.read_video, bilibili._watch, bilibili.aiohttp.ClientSession = original


VIDEO = {'bvid': 'BV1LDny6zESy', 'cid': 7, 'seconds': 189, 'title': '肖邦夜曲', 'uploader': '某UP', 'category': '音乐',
         'length': '3分9秒', 'description': '现场', 'top_comments': ['好听']}


def test_she_watches_the_first_video_and_the_note_says_what_she_saw():
    row = {'letter_id': 'im-1'}
    out = run(row, 'https://www.bilibili.com/video/BV1LDny6zESy', video=VIDEO,
              watched={'seconds': 60, 'summary': '一个人在舞台上弹钢琴'})
    note = json.loads(out.split('\n', 2)[2])
    assert note['videos'][0]['watched']['summary'] == '一个人在舞台上弹钢琴' and 'cid' not in note['videos'][0]
    assert '页面信息' in note['meaning'] and '不要提' in note['meaning']
    assert run(row, 'https://www.bilibili.com/video/BV1LDny6zESy', video=None) == out  # read once per turn


def test_a_video_she_could_not_watch_still_reaches_her_by_its_page():
    row = {'letter_id': 'im-2'}
    out = run(row, 'https://b23.tv/AbCd123', video=VIDEO, fail_watch=True)
    assert row['bilibili_watch_failed'] is True and 'watched' not in row['bilibili_observations'][0]
    assert '肖邦夜曲' in out


def test_an_unreadable_link_leaves_the_turn_as_it_was():
    row = {'letter_id': 'im-3'}
    assert run(row, 'https://b23.tv/AbCd123', video=None) == '' and row['bilibili_unread'] == 1
    assert run({}, '没有链接') == ''
