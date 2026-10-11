"""帮助改进: upload anonymized exchanges only after the user turns it on.

Nothing is sent while the switch is off. Only text is sent: the user's message
and her reply (spoken replies are already text). Contact details, numbers and
links are removed on this computer first. Each upload carries a random device
identifier that is not linked to the account key; withdrawing asks the server to
delete that identifier's data and then starts a fresh identifier.
"""
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

BATCH = 50
MAX_TEXT = 8000
STATE = 'improve/state.json'
ENDPOINT = '/improve/exchanges'
FORGET = '/improve/forget'

_PATTERNS = (
    (re.compile(r'https?://\S+|www\.\S+', re.I), '[链接]'),
    (re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+'), '[邮箱]'),
    (re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)'), '[证件号]'),
    (re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'), '[手机号]'),
    (re.compile(r'(?<!\d)\d{5,}(?!\d)'), '[号码]'),
    (re.compile(r'(?:QQ|qq|微信|vx|VX|wx|WX)\s*[号:：]?\s*[A-Za-z][\w-]{4,}'), '[账号]'),
    (re.compile(r'@\S{1,20}'), '@[某人]'),
)


def anonymize(text):
    text = text if isinstance(text, str) else ''
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:MAX_TEXT]


def _now():
    return datetime.now(timezone.utc)


def read_state(root):
    try:
        value = json.loads((Path(root) / STATE).read_text('utf-8'))
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    return {'enabled': value.get('enabled') is True,
            'device_id': value.get('device_id') if isinstance(value.get('device_id'), str) else None,
            'consent_at': value.get('consent_at') if isinstance(value.get('consent_at'), str) else None,
            'uploaded_until': value.get('uploaded_until') if isinstance(value.get('uploaded_until'), str) else None,
            'uploaded': value.get('uploaded') if type(value.get('uploaded')) is int else 0}


def write_state(root, state):
    path = Path(root) / STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False), 'utf-8')
    temporary.replace(path)


def set_enabled(root, enabled, *, now=None):
    if type(enabled) is not bool:
        raise ValueError('IMPROVE_SETTING_INVALID')
    state = read_state(root)
    if enabled and not state['enabled']:
        # Consent starts now: earlier conversations are never uploaded.
        state.update(enabled=True, device_id=state['device_id'] or secrets.token_hex(16),
                     consent_at=(now or _now()).isoformat(), uploaded_until=None)
    elif not enabled:
        state['enabled'] = False
    write_state(root, state)
    return public(state)


def public(state):
    return {'enabled': state['enabled'], 'uploaded': state['uploaded'], 'since': state['consent_at']}


def _stamp(row):
    value = row.get('life_received_at')
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return stamp if stamp.utcoffset() is not None else None
    except ValueError:
        return None


def _label(value):
    return value if isinstance(value, str) and re.fullmatch(r'[a-z_]{1,40}|[A-Z_]{1,40}', value) else None


def _media(row):
    """Labels only: which media the turn could carry, what the plan chose, why a video did not go out."""
    offered = [kind for kind, present in (
        ('image', row.get('image_available') and (row.get('image_reply_settings') or {}).get('enabled')),
        ('video', row.get('daily_video_candidates'))) if present]
    value = {'offered': offered, 'planned': _label(row.get('companion_delivery')),
             'video': _label(row.get('daily_video_dropped') or row.get('daily_video_status') or row.get('share_video_status'))}
    return {key: item for key, item in value.items() if item}


def pending(rows, state):
    """Delivered exchanges received after consent and after the last upload, oldest first."""
    if not state['enabled'] or not state['consent_at']:
        return []
    after = datetime.fromisoformat(state['uploaded_until'] or state['consent_at'])
    items = []
    for row in rows:
        if not isinstance(row, dict) or row.get('read_only'):
            continue
        letter = (row.get('channel') or 'letter') == 'letter'
        if (row.get('letter_status') != 'COMPLETED') if letter else (row.get('delivery_status') != 'DELIVERED'):
            continue
        stamp = _stamp(row)
        reply = row.get('reply_text') if isinstance(row.get('reply_text'), str) else ''
        if stamp is None or stamp <= after or not reply.strip():
            continue
        items.append((stamp, {
            'channel': row.get('channel') or 'letter', 'origin': 'proactive' if row.get('origin') == 'proactive' else 'user',
            'hour': stamp.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:00Z'),
            'reply_mode': row.get('reply_mode') if isinstance(row.get('reply_mode'), str) else None,
            'delivery': row.get('requested_format') if isinstance(row.get('requested_format'), str) else None,
            'user': anonymize(row.get('content')), 'reply': anonymize(reply),
            **({'media': _media(row)} if _media(row) else {})}))
    items.sort(key=lambda item: item[0])
    return items[:BATCH]


async def upload_once(root, rows, post, *, client_version):
    """post(path, payload) sends JSON to the cloud and raises on failure."""
    state = read_state(root)
    batch = pending(rows, state)
    if not batch:
        return 0
    await post(ENDPOINT, {'device_id': state['device_id'], 'client_version': client_version,
                          'exchanges': [item for _, item in batch]})
    state = read_state(root)
    if state['enabled']:
        state.update(uploaded_until=batch[-1][0].isoformat(), uploaded=state['uploaded'] + len(batch))
        write_state(root, state)
    return len(batch)


async def forget(root, post):
    """Ask the server to delete this device's uploads, then start a fresh identifier."""
    state = read_state(root)
    if state['device_id']:
        await post(FORGET, {'device_id': state['device_id']})
    state.update(enabled=False, device_id=None, consent_at=None, uploaded_until=None, uploaded=0)
    write_state(root, state)
    return public(state)


def post_json(base, path, payload):
    """POST anonymized data to the cloud relay without any account credential."""
    import urllib.request
    base = (base or '').rstrip('/')
    if not base.startswith('https://'):
        raise RuntimeError('IMPROVE_ENDPOINT_UNAVAILABLE')
    request = urllib.request.Request(base + path, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError('IMPROVE_UPLOAD_FAILED')
