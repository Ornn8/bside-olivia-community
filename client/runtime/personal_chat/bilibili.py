"""A B站 video he shares is watched on his own computer: its first minute (picture and sound, through the same
media observation as a video he sends on QQ, counted like any reply), plus title, uploader, length, description and
a few top comments (owner 2026-10-24: 「林离怎么没看视频内容？」).

The page facts reach her as an observation, never as his words, and say what they are: she has read about the
video, she has not watched or heard it. B站 serves video details only to signed (wbi) requests that carry the
cookies its home page sets: the cookies are fetched for each turn, the signing key is kept for an hour. Nothing is logged in and nothing is sent
to B站 but the video id. A link that cannot be read leaves the turn as it was.
"""
import asyncio
import hashlib
import json
import re
import time
from urllib.parse import urlencode, urlsplit

import aiohttp

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
HOME, API = 'https://www.bilibili.com/', 'https://api.bilibili.com'
_LINK = re.compile(r'https?://(?:b23\.tv/[A-Za-z0-9]{4,16}|(?:www\.|m\.)?bilibili\.com/video/(BV[0-9A-Za-z]{10}))')
_BV = re.compile(r'BV[0-9A-Za-z]{10}')
_MIXIN = (46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39,
          12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63,
          57, 62, 11, 36, 20, 34, 44, 52)
KEEP_SECONDS = 3600
TIMEOUT_SECONDS = 8
WATCH_SECONDS = 59.5           # the media observation takes at most a minute of video
WATCH_TIMEOUT_SECONDS = 75
MEANING = ('这是他分享的B站视频，不是他说的话。watched 是你刚看过的开头部分（画面和声音），像真的看过那样自然地聊里面的内容；'
           '后面没看到的部分只知道标题、简介和几条热门评论，不编画面和情节。热门评论是别人写的，不是他的看法。'
           '没有 watched 时你还没看视频本身，只知道标题和简介，可以自然地说还没来得及看。'
           '不要提页面信息、读取、识别、截取这类说法。')
_session = {'until': 0, 'mixin': None}


def video_links(text):
    """B站 video links in his message, in order, at most two."""
    found = []
    for match in _LINK.finditer(text or ''):
        if match.group(0) not in found:
            found.append(match.group(0))
    return found[:2]


def sign(params, mixin, now=None):
    params = dict(params, wts=int(now if now is not None else time.time()))
    query = urlencode(sorted((key, re.sub(r"[!'()*]", '', str(value))) for key, value in params.items()))
    return query + '&w_rid=' + hashlib.md5((query + mixin).encode()).hexdigest()


def mixin_key(img_url, sub_url):
    raw = urlsplit(img_url).path.rsplit('/', 1)[-1].split('.')[0] + urlsplit(sub_url).path.rsplit('/', 1)[-1].split('.')[0]
    if len(raw) < 64:
        raise ValueError('BILIBILI_KEYS_INVALID')
    return ''.join(raw[i] for i in _MIXIN)[:32]


async def _json(http, url, referer=HOME, codes=(0,)):
    async with http.get(url, headers={'Referer': referer, 'Origin': 'https://www.bilibili.com',
                                      'Accept': 'application/json, text/plain, */*'}) as response:
        if response.status != 200:
            raise RuntimeError('BILIBILI_HTTP_' + str(response.status))
        body = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            body.extend(chunk)
            if len(body) > 2 * 1024 * 1024:
                raise RuntimeError('BILIBILI_RESPONSE_TOO_LARGE')
        value = json.loads(body)
    if not isinstance(value, dict) or value.get('code') not in codes:
        raise RuntimeError('BILIBILI_REFUSED')
    return value.get('data') or {}


async def _bvid(http, link):
    match = _BV.search(urlsplit(link).path)
    if match:
        return match.group(0)
    async with http.get(link, allow_redirects=False) as response:  # b23.tv answers with the full link
        target = response.headers.get('Location', '')
    host = urlsplit(target).hostname or ''
    match = _BV.search(urlsplit(target).path)
    if not match or not (host == 'bilibili.com' or host.endswith('.bilibili.com')):
        raise RuntimeError('BILIBILI_LINK_UNRESOLVED')
    return match.group(0)


def _clip(text, limit):
    text = re.sub(r'\s+', ' ', str(text or '')).strip()
    return text if len(text) <= limit else text[:limit] + '…'


async def read_video(http, link):
    if _session['until'] < time.time():
        nav = await _json(http, API + '/x/web-interface/nav', codes=(0, -101))  # -101: not signed in, keys still given
        _session.update(mixin=mixin_key(nav['wbi_img']['img_url'], nav['wbi_img']['sub_url']),
                        until=time.time() + KEEP_SECONDS)
    bvid = await _bvid(http, link)
    page = f'https://www.bilibili.com/video/{bvid}/'
    data = await _json(http, API + '/x/web-interface/wbi/view?' + sign({'bvid': bvid}, _session['mixin']), page)
    seconds = int(data.get('duration') or 0)
    video = {'bvid': bvid, 'cid': data.get('cid'), 'seconds': seconds, 'title': _clip(data.get('title'), 80), 'uploader': _clip((data.get('owner') or {}).get('name'), 30),
             'category': _clip(data.get('tname'), 20), 'length': f'{seconds // 60}分{seconds % 60}秒' if seconds else None,
             'description': _clip(data.get('desc'), 300), 'top_comments': []}
    try:
        replies = await _json(http, API + '/x/v2/reply/wbi/main?' + sign({'oid': data['aid'], 'type': 1, 'mode': 3, 'ps': 5},
                                                                            _session['mixin']), page)
        video['top_comments'] = [_clip((item.get('content') or {}).get('message'), 80)
                                 for item in (replies.get('replies') or [])[:3] if isinstance(item, dict)]
    except (RuntimeError, KeyError, ValueError, aiohttp.ClientError):
        pass  # the video itself is enough
    return video


async def _first_minute(http, video, target):
    """The opening of the video, re-encoded small enough for one media observation, written to target."""
    page = f"https://www.bilibili.com/video/{video['bvid']}/"
    play = await _json(http, API + '/x/player/wbi/playurl?' + sign({'bvid': video['bvid'], 'cid': video['cid'], 'qn': 16,
                                                                     'fnval': 1, 'fnver': 0, 'fourk': 0},
                                                                    _session['mixin']), page)
    stream = ((play.get('durl') or [{}])[0] or {}).get('url')
    if not isinstance(stream, str) or not stream.startswith('https://') or any(c.isspace() for c in stream):
        raise RuntimeError('BILIBILI_STREAM_UNAVAILABLE')
    from runtime.media.latentsync_reply import resolve_ffmpeg_executable
    from runtime.media.managed_subprocess import run_managed_process
    result = await asyncio.to_thread(run_managed_process, [str(resolve_ffmpeg_executable()), '-nostdin', '-v', 'error', '-y',
        '-user_agent', UA, '-headers', 'Referer: https://www.bilibili.com/\r\n', '-i', stream, '-t', str(WATCH_SECONDS),
        '-map', '0:v:0', '-map', '0:a:0?', '-vf', 'scale=640:640:force_original_aspect_ratio=decrease:force_divisible_by=2,fps=2',
        '-c:v', 'libx264', '-preset', 'veryfast', '-b:v', '300k', '-maxrate', '400k', '-bufsize', '800k',
        '-c:a', 'aac', '-b:a', '48k', '-ar', '16000', '-movflags', '+faststart', str(target)], timeout_seconds=60)
    if result.returncode or not target.is_file() or target.stat().st_size < 1024:
        raise RuntimeError('BILIBILI_STREAM_UNREADABLE')


async def _watch(server, http, video, row):
    """Her look at the first minute: a summary of what is seen and heard, or None."""
    from runtime.incoming_media import describe_media
    target = server._state_root() / 'media' / f"bilibili-{video['bvid']}.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        await _first_minute(http, video, target)
        item = await describe_media(server, target, 'video', '', f"{row['letter_id']}:bilibili:{video['bvid']}")
        return {'seconds': int(min(video['seconds'] or WATCH_SECONDS, 60)), 'summary': item['summary'][:2000]}
    finally:
        target.unlink(missing_ok=True)


async def link_context(server, event, row):
    """The observation text for B站 links in this turn ('' when there are none); read once per turn."""
    links = video_links(event.text)
    if not links:
        return ''
    if 'bilibili_observations' not in row:
        videos, failures = [], 0
        jar = aiohttp.CookieJar()
        try:
            async with aiohttp.ClientSession(cookie_jar=jar, headers={'User-Agent': UA}, trust_env=False,
                                             timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS + WATCH_TIMEOUT_SECONDS)) as http:
                async with http.get(HOME) as response:  # the cookies B站 expects with its signed requests
                    await response.read()
                for link in links:
                    try:
                        videos.append(await asyncio.wait_for(read_video(http, link), TIMEOUT_SECONDS))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        failures += 1
                        _session['until'] = 0  # fetch keys and cookies afresh next time
                if videos and videos[0].get('cid'):
                    try:  # she watches the first video he sent; the others she knows by their page
                        watched = await asyncio.wait_for(_watch(server, http, videos[0], row), WATCH_TIMEOUT_SECONDS)
                        if watched:
                            videos[0]['watched'] = watched
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        row['bilibili_watch_failed'] = True
                for video in videos:
                    video.pop('cid', None)
        except aiohttp.ClientError:
            failures = len(links)
        row['bilibili_observations'] = videos
        row['bilibili_unread'] = failures
        try:
            server._persist_store_state()
        except Exception:
            pass
    if not row['bilibili_observations']:
        return ''
    return '\n[系统链接观察，非用户原话]\n' + json.dumps({'meaning': MEANING, 'videos': row['bilibili_observations']},
                                                        ensure_ascii=False)
