"""A song the user shares in QQ, read from his message: which song it is, never its audio.

What arrives (events.owner_message turns a received QQ card into text):
- a music card (网易云音乐, QQ音乐 and others): ``[分享卡片：网易云音乐 · 晴天 · 周杰伦] https://y.music.163.com/m/song?id=186016``;
- a B站 card or link: ``[分享卡片：哔哩哔哩 · 夜曲] https://b23.tv/AbCd123`` or a bilibili.com/video/BV... address;
- a link in the user's own words, often the app's share text: 「分享周杰伦的单曲《晴天》: https://music.163.com/song?id=186016」.

Only the identity of the song is read here (a 网易云 song id, a B站 video id, or a title and singer). The relay finds and
sends the audio; nothing in this module touches a music service.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import parse_qs, urlsplit

NETEASE, BILIBILI, NAMED = 'netease', 'bilibili', 'named'
APPS = ('QQ音乐', '网易云音乐', '酷狗音乐', '酷我音乐', '汽水音乐', '咪咕音乐')
BILIBILI_APP = '哔哩哔哩'
_CARD = re.compile(r'\[分享卡片：([^·\]]{1,30}?)(?: · ([^\]]{1,400}))?\]')
_URL = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'*+,;=%]+")
_BV = re.compile(r'BV[0-9A-Za-z]{10}')
_ID = re.compile(r'[1-9][0-9]{0,15}')
_SHARED = re.compile(r'分享(.{1,40}?)的(?:单曲|歌曲)《([^》]{1,60})》')
_TITLE = re.compile(r'《([^》]{1,60})》')
NETEASE_HOSTS = frozenset({'music.163.com', 'y.music.163.com', 'm.music.163.com', 'st.music.163.com'})
NETEASE_SHORT_HOSTS = frozenset({'163cn.tv'})
BILIBILI_SHORT_HOST = 'b23.tv'
_NOT_SONGS = re.compile(r'(?:/m)?/(?:playlist|album|artist|mv|video|dj|program|djradio|user|discover)(?:/|$)')


@dataclass(frozen=True)
class Share:
    kind: str                 # netease | bilibili | named
    key: str                  # the same song gives the same key: netease:<id>, bilibili:<BV>, named:<title>|<singer>
    title: str = ''
    singer: str = ''
    app: str = ''
    song_id: str = ''
    bvid: str = ''
    short_link: str = ''      # a B站 short link whose video id is still to be read

    def request(self) -> dict:
        """The body of the relay's song request."""
        if self.kind == NETEASE:
            return {'kind': NETEASE, 'song_id': self.song_id, 'title': self.title, 'singer': self.singer}
        if self.kind == BILIBILI:
            return {'kind': BILIBILI, 'bvid': self.bvid, 'title': self.title}
        return {'kind': NAMED, 'title': self.title, 'singer': self.singer}


def _links(text):
    for url in _URL.findall(text or ''):
        yield url.rstrip(".,;:!?'")


def netease_id(url) -> str | None:
    """The 网易云 song a link names, or None (a playlist, an album or any other address)."""
    try:
        parts = urlsplit(str(url))
        host = (parts.hostname or '').lower()
    except ValueError:
        return None
    if parts.scheme not in ('http', 'https') or host not in NETEASE_HOSTS:
        return None
    path, query = parts.path or '/', parts.query
    if parts.fragment.startswith('/'):  # music.163.com/#/song?id=...
        path, _, query = parts.fragment.partition('?')
    path = path.rstrip('/') or '/'
    found = re.fullmatch(r'(?:/m)?/song/([1-9][0-9]{0,15})', path)
    value = found.group(1) if found else None
    if value is None and path in ('/song', '/m/song', '/song/media/outer/url'):
        value = (parse_qs(query).get('id') or [''])[0].removesuffix('.mp3')
    return value if value and _ID.fullmatch(value) else None


def bilibili_bvid(url) -> str | None:
    try:
        parts = urlsplit(str(url))
        host = (parts.hostname or '').lower()
    except ValueError:
        return None
    if parts.scheme in ('http', 'https') and (host == 'bilibili.com' or host.endswith('.bilibili.com')):
        found = re.match(r'/video/(BV[0-9A-Za-z]{10})(?:/|$)', parts.path or '')
        return found.group(1) if found else None
    return None


def _is_short(url, hosts) -> bool:
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return False
    host = (parts.hostname or '').lower()
    return parts.scheme in ('http', 'https') and host in hosts and bool(re.fullmatch(r'/[0-9A-Za-z]{4,16}/?', parts.path or ''))


def _clean(value, limit):
    return re.sub(r'\s+', ' ', str(value or '')).strip()[:limit]


def _named_key(title, singer):
    return 'named:' + re.sub(r'\s+', '', title).lower() + '|' + re.sub(r'\s+', '', singer).lower()


def find(text) -> Share | None:
    """The first song this message shares, or None. At most one song is taken per message."""
    text = str(text or '')
    card = _CARD.search(text)
    app, title, singer = '', '', ''
    if card:
        app = card.group(1).strip()
        words = [part.strip() for part in (card.group(2) or '').split(' · ')]
        if app == BILIBILI_APP:
            title = _clean(words[0], 80)
        elif app in APPS:
            title, singer = _clean(words[0], 60), _clean(words[1] if len(words) > 1 else '', 40)
        else:
            card = None  # some other kind of card: its link may still be a song
    after = text[card.end():] if card else text
    shared = _SHARED.search(text)
    for url in _links(after):
        song = netease_id(url)
        if song:
            if not title:
                named = shared or _TITLE.search(text)
                if named:
                    title = _clean(named.group(named.lastindex), 60)
                if shared:
                    singer = singer or _clean(shared.group(1), 40)
            return Share(NETEASE, 'netease:' + song, title, singer, app or '网易云音乐', song_id=song)
        bvid = bilibili_bvid(url)
        if bvid:
            return Share(BILIBILI, 'bilibili:' + bvid, title, '', app or BILIBILI_APP, bvid=bvid)
        if _is_short(url, {BILIBILI_SHORT_HOST}):
            return Share(BILIBILI, 'bilibili:' + url, title, '', app or BILIBILI_APP, short_link=url)
        if _is_short(url, NETEASE_SHORT_HOSTS) or (card and app in APPS):
            # A short link or another app's card: the title and singer name the song.
            if not title and shared:
                title, singer = _clean(shared.group(2), 60), _clean(shared.group(1), 40)
            if title:
                return Share(NAMED, _named_key(title, singer), title, singer, app or '网易云音乐')
    if card and app in APPS and title:
        return Share(NAMED, _named_key(title, singer), title, singer, app)
    return None


def with_bvid(share: Share, bvid: str) -> Share:
    """A B站 share whose short link has been read."""
    return Share(BILIBILI, 'bilibili:' + bvid, share.title, '', share.app, bvid=bvid)
