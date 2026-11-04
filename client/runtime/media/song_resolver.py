"""Ask the relay for the audio of a song the user shared; the relay finds and sends it, this client only receives it.

``POST /v1/songs/resolve`` takes one of
  {"kind": "netease", "song_id", "title", "singer", "seconds"} | {"kind": "bilibili", "bvid", "title"} |
  {"kind": "named", "title", "singer"}
and answers 200 with the audio bytes and the headers X-Song-Title, X-Song-Singer (percent-encoded UTF-8),
X-Song-Source (netease | bilibili), X-Song-Seconds and X-Song-Identity (sha256 hex), or a JSON error
{"error_code", "message"}. One call takes at most 90 seconds.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote

from aiohttp import ClientSession, ClientTimeout, ClientError, ClientSSLError, TCPConnector

from runtime import contact_context, ui_language
from runtime.cloud_service import endpoint, CloudError
from runtime.official_endpoints import canonical_api_origin
from runtime.tls import client_tls_context

PATH = '/v1/songs/resolve'
CALL_SECONDS = 95          # the relay answers within 90 s
MAX_BYTES = 64 * 1024 * 1024
SOURCES = frozenset({'netease', 'bilibili'})

# What each failure means for the user, in plain words (the code is shown beside it).
WORDS = {
    'SONG_NOT_FOUND': '没有找到这首歌',
    'SONG_UNAVAILABLE': '这首歌现在拿不到音频（可能是会员专享、版权限制或已下架）',
    'SONG_TOO_LONG': '这首歌太长了，超过了可以翻唱的长度',
    'SONG_FETCH_FAILED': '取这首歌的音频时出了问题',
    'SONG_BAD_REQUEST': '没能看懂分享的这首歌',
    'SONG_RATE_LIMITED': '最近取歌太频繁了，需要等一会儿再试',
    'SONG_NOT_AVAILABLE': '取歌的服务现在没有开放',
    'SONG_TIMEOUT': '取这首歌等了太久还没有结果',
    'SONG_SERVICE_UNREACHABLE': '连不上取歌的服务',
    'SONG_NOT_CONFIGURED': '还没有连接 Olivia 云服务',
    'SONG_LINK_UNREADABLE': '这条链接里认不出是哪首歌',
    'SONG_AUDIO_INVALID': '拿到的音频打不开',
    'SONG_AUTH_FAILED': 'Olivia 云服务没有认可这次请求',
    'SONG_FFMPEG_UNAVAILABLE': '这台电脑上缺少处理音频的组件',
    'SONG_FILE_TOO_LARGE': '这个文件太大了（上限 40 MB）',
    'SONG_FILE_UNAVAILABLE': '没能从QQ里取到这个文件',
}
KNOWN = frozenset(WORDS)


class SongError(Exception):
    def __init__(self, code, status=0):
        super().__init__(code)
        self.code = code if code in KNOWN else 'SONG_FETCH_FAILED'
        self.status = status

    @property
    def words(self) -> str:
        return WORDS[self.code]


def reason(code: str) -> str:
    """The plain reason with its code, as it is told to the user."""
    code = code if code in KNOWN else 'SONG_FETCH_FAILED'
    return f'{WORDS[code]}（错误码 {code}）'


def _text(value, limit) -> str:
    return re.sub(r'\s+', ' ', unquote(value or '')).strip()[:limit]


async def resolve(request: dict, target: Path, url: str, token: str) -> dict:
    """Fetch the song into ``target`` (written whole, or not at all) and return its details:
    {title, singer, source, seconds, identity, bytes}. Raises SongError."""
    if not url or not token:
        raise SongError('SONG_NOT_CONFIGURED')
    try:
        base = canonical_api_origin(endpoint(url))
    except CloudError:
        raise SongError('SONG_NOT_CONFIGURED') from None
    headers = {'Authorization': 'Bearer ' + token, 'Accept': '*/*', **ui_language.headers(), **contact_context.headers()}
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + '.part')
    try:
        async with ClientSession(timeout=ClientTimeout(total=CALL_SECONDS, sock_connect=15), trust_env=False,
                                 connector=TCPConnector(ssl=client_tls_context())) as session:
            async with session.post(base + PATH, json=request, headers=headers, allow_redirects=False) as response:
                if response.status != 200:
                    raise await _failure(response)
                size = 0
                with part.open('wb') as stream:
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise SongError('SONG_TOO_LONG', 200)
                        stream.write(chunk)
                details = _details(response.headers, size)
        part.replace(target)
        return details
    except SongError:
        raise
    except TimeoutError:
        raise SongError('SONG_TIMEOUT') from None
    except ClientSSLError:
        raise SongError('SONG_SERVICE_UNREACHABLE') from None
    except (ClientError, OSError):
        raise SongError('SONG_SERVICE_UNREACHABLE') from None
    finally:
        part.unlink(missing_ok=True)


def _details(headers, size) -> dict:
    source = headers.get('X-Song-Source', '')
    identity = headers.get('X-Song-Identity', '')
    seconds = headers.get('X-Song-Seconds', '')
    if size <= 0 or source not in SOURCES or not re.fullmatch(r'[0-9a-f]{64}', identity):
        raise SongError('SONG_FETCH_FAILED', 200)
    try:
        seconds = float(seconds)
    except ValueError:
        seconds = 0.0
    if not 0 <= seconds < 36000:
        seconds = 0.0
    return {'title': _text(headers.get('X-Song-Title'), 80), 'singer': _text(headers.get('X-Song-Singer'), 60),
            'source': source, 'seconds': seconds, 'identity': identity, 'bytes': size}


async def _failure(response) -> SongError:
    code = ''
    try:
        body = await response.content.read(16384)
        value = json.loads(body)
        if isinstance(value, dict) and isinstance(value.get('error_code'), str):
            code = value['error_code']
    except (ValueError, ClientError):
        pass
    if code in KNOWN:
        return SongError(code, response.status)
    if response.status in (401, 403):
        return SongError('SONG_AUTH_FAILED', response.status)
    if response.status == 404 or response.status == 501:
        return SongError('SONG_NOT_AVAILABLE', response.status)   # a relay that does not know this call
    if response.status == 429:
        return SongError('SONG_RATE_LIMITED', response.status)
    return SongError('SONG_FETCH_FAILED', response.status)


async def probe(url: str, token: str) -> bool:
    """Does the relay know this call? An empty request must be answered 400 SONG_BAD_REQUEST by a relay that has it;
    any other answer (not found, not available, a page, no answer) means the call is not there."""
    if not url or not token:
        return False
    try:
        base = canonical_api_origin(endpoint(url))
        headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json',
                   **ui_language.headers(), **contact_context.headers()}
        async with ClientSession(timeout=ClientTimeout(total=10, sock_connect=5), trust_env=False,
                                 connector=TCPConnector(ssl=client_tls_context())) as session:
            async with session.post(base + PATH, json={}, headers=headers, allow_redirects=False) as response:
                if response.status != 400:
                    return False
                value = json.loads(await response.content.read(16384))
                return isinstance(value, dict) and value.get('error_code') == 'SONG_BAD_REQUEST'
    except (ValueError, TimeoutError, ClientError, OSError, CloudError):
        return False
