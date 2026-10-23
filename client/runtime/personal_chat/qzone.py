"""QQ空间 through the session NapCat already holds: read a 说说 list, like one, comment on one, answer a comment.

NapCat publishes 说说 itself (send_qzone_msg). Reading, liking and commenting use the QQ空间 web endpoints with the
cookies NapCat hands out for that domain (get_cookies); nothing here logs in or stores a credential, and the
cookies live only for the request they are fetched for.
"""
import json
import re
from urllib.parse import urlencode

import aiohttp

BASE = 'https://user.qzone.qq.com/proxy/domain'
TIMEOUT = aiohttp.ClientTimeout(total=20)
_CALLBACK = re.compile(r'^[^(]*\((.*)\)\s*;?\s*$', re.S)
_FRAME = re.compile(r'frameElement\.callback\((\{.*?\})\)', re.S)


class QzoneUnavailable(RuntimeError):
    pass


def g_tk(cookies):
    """QQ空间's request token, derived from the p_skey cookie."""
    match = re.search(r'(?:^|;\s*)p_skey=([^;]+)', cookies)
    if not match:
        raise QzoneUnavailable('QZONE_COOKIE_MISSING')
    value = 5381
    for char in match.group(1):
        value += (value << 5) + ord(char)
    return value & 0x7fffffff


def _json(text):
    text = text.strip()
    match = _CALLBACK.match(text) if not text.startswith('{') else None
    try:
        return json.loads(match.group(1) if match else text)
    except ValueError:
        frame = _FRAME.search(text)
        if frame:
            try:
                return json.loads(frame.group(1))
            except ValueError:
                pass
    raise QzoneUnavailable('QZONE_RESPONSE_INVALID')


class Qzone:
    def __init__(self, cookies, self_uin, session=None):
        self.cookies, self.self_uin, self.token = cookies, str(self_uin), g_tk(cookies)
        self.session = session

    async def _request(self, method, url, form=None):
        headers = {'Cookie': self.cookies, 'Referer': f'https://user.qzone.qq.com/{self.self_uin}',
                   'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36'}
        own = self.session is None
        session = self.session or aiohttp.ClientSession(timeout=TIMEOUT, trust_env=False)
        try:
            async with session.request(method, url, data=form, headers=headers, allow_redirects=False) as response:
                if response.status != 200:
                    raise QzoneUnavailable('QZONE_HTTP_' + str(response.status))
                body = await response.content.read(2 * 1024 * 1024 + 1)
                if len(body) > 2 * 1024 * 1024:
                    raise QzoneUnavailable('QZONE_RESPONSE_TOO_LARGE')
                return _json(body.decode('utf-8', 'replace'))
        except aiohttp.ClientError:
            raise QzoneUnavailable('QZONE_NETWORK') from None
        finally:
            if own:
                await session.close()

    async def posts(self, uin, count=10):
        """The newest 说说 of one QQ: [{tid, text, created, pictures, comments:[{id, uin, text, created}]}]."""
        query = urlencode({'uin': uin, 'ftype': 0, 'sort': 0, 'pos': 0, 'num': count, 'replynum': 100,
                           'g_tk': self.token, 'code_version': 1, 'format': 'json', 'need_private_comment': 1})
        data = await self._request('GET', f'{BASE}/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6?{query}')
        if not isinstance(data, dict) or data.get('code') != 0:
            raise QzoneUnavailable('QZONE_LIST_REFUSED')
        result = []
        for item in data.get('msglist') or []:
            if not isinstance(item, dict) or not isinstance(item.get('tid'), str):
                continue
            comments = [{'id': str(c.get('tid')), 'uin': str(c.get('uin')), 'text': str(c.get('content') or ''),
                         'created': int(c.get('create_time') or 0)}
                        for c in item.get('commentlist') or [] if isinstance(c, dict)]
            result.append({'tid': item['tid'], 'text': str(item.get('content') or '')[:2000],
                           'created': int(item.get('created_time') or 0), 'pictures': len(item.get('pic') or []),
                           'comments': comments})
        return result

    async def like(self, uin, tid, created):
        key = f'http://user.qzone.qq.com/{uin}/mood/{tid}'
        form = {'qzreferrer': f'https://user.qzone.qq.com/{self.self_uin}', 'opuin': self.self_uin, 'unikey': key,
                'curkey': key, 'from': 1, 'appid': 311, 'typeid': 0, 'abstime': created, 'fid': tid, 'active': 0,
                'fupdate': 1}
        data = await self._request('POST', f'{BASE}/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app?g_tk={self.token}', form)
        if not isinstance(data, dict) or data.get('code') != 0:
            raise QzoneUnavailable('QZONE_LIKE_REFUSED')

    async def comment(self, uin, tid, text, *, reply_to=None):
        """A comment on a 说说, or with reply_to=(comment id, its author) an answer to one comment under it."""
        form = {'topicId': f'{uin}_{tid}__1', 'feedsType': 100, 'inCharset': 'utf-8', 'outCharset': 'utf-8',
                'plat': 'qzone', 'source': 'ic', 'hostUin': uin, 'platformid': 52, 'uin': self.self_uin,
                'format': 'fs', 'ref': 'feeds', 'content': text, 'richval': '', 'richtype': '', 'private': 0,
                'paramstr': 1, 'qzreferrer': f'https://user.qzone.qq.com/{self.self_uin}'}
        if reply_to is not None:
            form.update(commentId=reply_to[0], commentUin=reply_to[1])
        data = await self._request('POST', f'{BASE}/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds?g_tk={self.token}', form)
        if not isinstance(data, dict) or data.get('code') != 0:
            raise QzoneUnavailable('QZONE_COMMENT_REFUSED')
