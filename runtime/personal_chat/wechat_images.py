"""Official PNG image upload for personal-chat stickers.

Protocol: Tencent/openclaw-weixin src/cdn/upload.ts and messaging/send.ts.
"""
import base64
import hashlib
from pathlib import Path
import secrets
from urllib.parse import urlencode, urlsplit

from .probe import wechat_request
from .image_crypto import encrypt_image


async def upload(session, credentials, owner, path):
    path = Path(path)
    if path.suffix.lower() != '.png' or not 24 <= path.stat().st_size <= 20 * 1024 * 1024:
        raise ValueError('WECHAT_IMAGE_INVALID')
    raw = path.read_bytes()
    if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('WECHAT_IMAGE_INVALID')
    key = secrets.token_bytes(16)
    encrypted = encrypt_image(raw, key)
    identifier = secrets.token_hex(16)
    info = await wechat_request(session, credentials['base'], '/ilink/bot/getuploadurl',
        token=credentials['token'], body={'filekey': identifier, 'media_type': 1, 'to_user_id': owner,
        'rawsize': len(raw), 'rawfilemd5': hashlib.md5(raw).hexdigest(), 'filesize': len(encrypted),
        'no_need_thumb': True, 'aeskey': key.hex()})
    url = info.get('upload_full_url')
    if not url and info.get('upload_param'):
        url = 'https://novac2c.cdn.weixin.qq.com/c2c/upload?' + urlencode({
            'encrypted_query_param': info['upload_param'], 'filekey': identifier})
    parsed = urlsplit(url or '')
    if (parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.weixin.qq.com')
            or parsed.username or parsed.password or parsed.port not in {None,443}):
        raise ValueError('WECHAT_CDN_URL_INVALID')
    async with session.post(url, data=encrypted, headers={'Content-Type':'application/octet-stream'}, allow_redirects=False) as response:
        download = response.headers.get('x-encrypted-param')
        if response.status != 200 or not download:
            raise RuntimeError('WECHAT_IMAGE_UPLOAD_FAILED')
    return {'type':2, 'image_item':{'mid_size':len(encrypted),
        'media':{'encrypt_query_param':download,'aes_key':base64.b64encode(key.hex().encode()).decode(), 'encrypt_type':1}}}
