"""A B站 video he shares is watched on his own computer: all of its sound and eight pictures a minute (through the
same media observation as a video he sends on QQ, counted like any reply), plus title, uploader, length,
description and a few top comments (owner 2026-10-24: 「林离怎么没看视频内容？」).

What she saw and heard reaches her as an observation, never as his words, in time order; a video too long to
finish within the reply's wait is watched as far as that wait allows, and she knows so. B站 serves video details only to signed (wbi) requests that carry the
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
# The whole video, however long (owner 2026-10-24 「不用定时长」): all its sound, and 「每分钟抽8帧」 of its picture.
FRAMES_PER_MINUTE = 8
FRAMES_PER_CLIP = 55          # one picture observation: one second per frame, safely under its one-minute limit
SOUND_PIECE = 59              # one sound observation: under its one-minute limit
OBSERVATIONS_AT_ONCE = 6
WATCH_TIMEOUT_SECONDS = 120   # the reply never waits longer; what was seen by then is what she saw
MEANING = ('这是他分享的B站视频，不是他说的话。watched 是你看这个视频的结果：sound 按时间是你听到的内容，'
           'pictures 按时间段是你看到的画面；像真的看过那样自然地聊里面的内容。complete=false 时你只看了前面这些，'
           '后面的只知道标题、简介和几条热门评论，不编画面和情节。热门评论是别人写的，不是他的看法。'
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


async def _streams(http, video):
    """The lightest picture stream (H.264) and sound stream of the video, as https addresses."""
    page = f"https://www.bilibili.com/video/{video['bvid']}/"
    play = await _json(http, API + '/x/player/wbi/playurl?' + sign({'bvid': video['bvid'], 'cid': video['cid'], 'qn': 16,
                                                                     'fnval': 16, 'fnver': 0, 'fourk': 0},
                                                                    _session['mixin']), page)
    dash = play.get('dash') or {}
    pictures = sorted((item for item in dash.get('video') or [] if str(item.get('codecs', '')).startswith('avc')),
                      key=lambda item: item.get('bandwidth') or 0)
    sounds = sorted(dash.get('audio') or [], key=lambda item: item.get('bandwidth') or 0)
    found = []
    for items in (pictures, sounds):
        url = (items[0].get('baseUrl') or items[0].get('base_url')) if items else None
        if not isinstance(url, str) or not url.startswith('https://') or any(c.isspace() for c in url):
            raise RuntimeError('BILIBILI_STREAM_UNAVAILABLE')
        found.append(url)
    return found


def _ffmpeg(arguments, timeout, *, remote=True):
    """remote: the input is a B站 stream and needs its browser headers; a local file must not get them."""
    from runtime.media.latentsync_reply import resolve_ffmpeg_executable
    from runtime.media.managed_subprocess import run_managed_process
    headers = ['-user_agent', UA, '-headers', 'Referer: https://www.bilibili.com/\r\n'] if remote else []
    return run_managed_process([str(resolve_ffmpeg_executable()), '-nostdin', '-v', 'error', '-y', *headers, *arguments],
                               timeout_seconds=timeout)


async def _sound(url, folder):
    """The whole sound track as mono 16 kHz pieces of at most SOUND_PIECE seconds."""
    result = await asyncio.to_thread(_ffmpeg, ['-i', url, '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le',
                                               '-f', 'segment', '-segment_time', str(SOUND_PIECE),
                                               str(folder / 'sound-%04d.wav')], WATCH_TIMEOUT_SECONDS)
    pieces = sorted(folder.glob('sound-*.wav'))
    if result.returncode and not pieces:
        raise RuntimeError('BILIBILI_SOUND_UNREADABLE')
    return pieces


async def _pictures(url, seconds, folder, deadline):
    """FRAMES_PER_MINUTE pictures a minute, spread evenly, then FRAMES_PER_CLIP of them to a one-second-a-frame clip."""
    count = max(1, round(seconds / 60 * FRAMES_PER_MINUTE))
    gate = asyncio.Semaphore(6)

    async def grab(index):
        async with gate:
            if time.monotonic() > deadline:
                return  # the pictures taken by now are the ones she sees
            moment = (index + 0.5) * seconds / count
            await asyncio.to_thread(_ffmpeg, ['-ss', f'{moment:.2f}', '-i', url, '-frames:v', '1',
                                              '-vf', 'scale=480:-2', str(folder / f'frame-{index:05d}.jpg')], 30)
    await asyncio.gather(*(grab(index) for index in range(count)))
    frames = sorted(folder.glob('frame-*.jpg'))
    taken = {int(frame.stem.split('-')[1]) for frame in frames}
    frames = [frame for frame in frames if int(frame.stem.split('-')[1]) < min(set(range(count + 1)) - taken)]
    clips = []
    for number, first in enumerate(range(0, len(frames), FRAMES_PER_CLIP)):
        group = frames[first:first + FRAMES_PER_CLIP]
        listing = folder / f'clip-{number:03d}.txt'
        listing.write_text(''.join(f"file '{frame.name}'\nduration 1\n" for frame in group)
                           + f"file '{group[-1].name}'\n", encoding='utf-8')
        clip = folder / f'clip-{number:03d}.mp4'
        result = await asyncio.to_thread(_ffmpeg, ['-f', 'concat', '-safe', '0', '-i', str(listing),
                                                   '-vf', 'fps=1,format=yuv420p', '-c:v', 'libx264', '-preset', 'veryfast',
                                                   '-movflags', '+faststart', str(clip)], 60, remote=False)
        if not result.returncode and clip.is_file():
            covered = (first / count * seconds, min(seconds, (first + len(group)) / count * seconds))
            clips.append((clip, covered))
    return clips


def _clock(seconds):
    return f'{int(seconds) // 60}:{int(seconds) % 60:02d}'


async def _watch(server, http, video, row):
    """What she saw and heard of the whole video, piece by piece in time order, or None."""
    from runtime.incoming_media import describe_media
    import shutil
    seconds = video.get('seconds') or 0
    if seconds <= 0:
        return None
    folder = server._state_root() / 'media' / f"bilibili-{video['bvid']}-{row['letter_id'][-12:]}"
    folder.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + WATCH_TIMEOUT_SECONDS
    try:
        picture_url, sound_url = await _streams(http, video)
        sounds, clips = await asyncio.gather(_sound(sound_url, folder),
                                             _pictures(picture_url, seconds, folder, deadline - 30))
        gate = asyncio.Semaphore(OBSERVATIONS_AT_ONCE)

        async def observe(path, kind, key):
            async with gate:
                item = await describe_media(server, path, kind, '', f"{row['letter_id']}:bilibili:{video['bvid']}:{key}")
                return item['summary']
        jobs = [asyncio.ensure_future(observe(path, 'audio', f'sound{index}')) for index, path in enumerate(sounds)]
        jobs += [asyncio.ensure_future(observe(clip, 'video', f'pictures{index}')) for index, (clip, _) in enumerate(clips)]
        done, pending = (await asyncio.wait(jobs, timeout=max(5, deadline - time.monotonic())) if jobs
                         else (set(), set()))
        for job in pending:
            job.cancel()  # the reply does not wait longer; what was seen by now is what she saw

        def result(job):
            return job.result() if job in done and not job.cancelled() and job.exception() is None else None
        sound = [{'from': _clock(index * SOUND_PIECE), 'heard': _clip(result(job), 400)}
                 for index, job in enumerate(jobs[:len(sounds)]) if result(job)]
        pictures = [{'from': _clock(covered[0]), 'to': _clock(covered[1]), 'seen': _clip(result(job), 700)}
                    for job, (_, covered) in zip(jobs[len(sounds):], clips) if result(job)]
        if not sound and not pictures:
            return None
        return {'video_length': video.get('length'), 'complete': not pending, 'sound': sound, 'pictures': pictures}
    finally:
        shutil.rmtree(folder, ignore_errors=True)


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
                        watched = await asyncio.wait_for(_watch(server, http, videos[0], row), WATCH_TIMEOUT_SECONDS + 30)
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
