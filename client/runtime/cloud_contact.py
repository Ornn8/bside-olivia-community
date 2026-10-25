"""The cloud asks her to reach out: what is accepted, kept, carried out and reported.

The cloud decides when, on which channel, in which words, how long and with what
attached. The client checks the request, carries it out through the pipelines that
already write and send her messages, and tells the cloud how it ended. It adds no
rule of its own: only the cloud's configuration (runtime.proactive_config) and the
fields of the event itself apply.
"""
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
import asyncio
import hashlib
import io
import re

from runtime import cloud_events, contact_context, proactive_config
from runtime.reply.proactive_letters import read_json, write_json

MAX_ATTEMPTS = 3
MAX_ACK_TRIES = 5
BUSY_SECONDS = 60
# What he bought is answered between his messages instead of after he stops writing (owner 2026-10-23).
PRIORITY_PURPOSES = frozenset({'gift_goods'})
RETRY_SECONDS = 120
KEEP_HANDLED = 200
MAX_PENDING = 100
MAX_ASSET_BYTES = 10 * 1024 * 1024
MEDIA_WAIT_SECONDS = 300  # How long a contact waits for its picture before it is reported.
DEFAULT_TITLE = '想和你说说话'
CHANNELS = ('letter', 'qq', 'qq_then_letter', 'preferred')
MEDIA_TYPES = ('photo', 'video', 'voice')
ACK_STATUSES = ('delivered', 'failed', 'expired', 'skipped')
TELEMETRY_STATUS = {'delivered': 'ok', 'failed': 'failed', 'expired': 'skipped', 'skipped': 'skipped'}
_ID = re.compile(r'contact:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_PURPOSE = re.compile(r'[a-z_]{1,32}')
_SHA256 = re.compile(r'[0-9a-f]{64}')
_TASK = re.compile(r'[A-Za-z0-9_-]{1,100}')
_CODE = re.compile(r'[A-Z][A-Z0-9_]{2,63}')

# status: delivered | failed | retry | busy. A failed attempt that may have reached
# the owner (fallback False) is never repeated on another channel.
Outcome = namedtuple('Outcome', 'status error_code fallback', defaults=(None, True))


def trusted_hosts():
    """Where a fixed picture may come from: the site, the API and the component storage the client already uses."""
    from runtime.official_endpoints import API_ORIGIN, WEBSITE, COMPONENT_COS_HOSTS
    return {urlsplit(WEBSITE).hostname, urlsplit(API_ORIGIN).hostname, *COMPONENT_COS_HOSTS}


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('CONTACT_EVENT_INVALID')
    return value


def _text(value, low, high):
    if (not isinstance(value, str) or not low <= len(value) <= high
            or any(ord(char) < 32 and char not in '\n\r\t' for char in value)):
        raise ValueError('CONTACT_EVENT_INVALID')
    return value


def _optional_text(value, low, high):
    return None if value is None else _text(value, low, high)


def _media(items):
    if not isinstance(items, list) or len(items) > 3:
        raise ValueError('CONTACT_EVENT_INVALID')
    result = []
    for item in items:
        if not isinstance(item, dict) or item.get('type') not in MEDIA_TYPES:
            raise ValueError('CONTACT_EVENT_INVALID')
        entry = {'type': item['type'], 'brief': _optional_text(item.get('brief'), 1, 300)}
        if item.get('asset_url') is not None:
            # A fixed picture: the address, the checksum and the size are all required.
            url = urlsplit(item['asset_url']) if isinstance(item['asset_url'], str) else None
            if (item['type'] != 'photo' or url is None or url.scheme != 'https' or url.hostname not in trusted_hosts()
                    or url.username or url.password or url.port not in (None, 443) or len(item['asset_url']) > 2048
                    or not isinstance(item.get('sha256'), str) or not _SHA256.fullmatch(item['sha256'])):
                raise ValueError('CONTACT_EVENT_INVALID')
            entry.update(asset_url=item['asset_url'], sha256=item['sha256'],
                         bytes=_integer(item.get('bytes'), 1, MAX_ASSET_BYTES))
        if item.get('task_id') is not None:
            # A video the service already finished for this account (a song it made again after a failed night):
            # fetched by its task, never ordered or generated again.
            if (item['type'] != 'video' or item.get('asset_url') is not None
                    or not isinstance(item['task_id'], str) or not _TASK.fullmatch(item['task_id'])):
                raise ValueError('CONTACT_EVENT_INVALID')
            entry['task_id'] = item['task_id']
        result.append(entry)
    return result


def parse_event(item):
    """The event with only the fields this client knows and every default filled in, or None."""
    try:
        if not isinstance(item, dict) or item.get('kind') != 'contact':
            return None
        if not isinstance(item.get('id'), str) or not _ID.fullmatch(item['id']):
            return None
        if not isinstance(item.get('purpose'), str) or not _PURPOSE.fullmatch(item['purpose']):
            return None
        if item.get('channel') not in CHANNELS:
            return None
        after = _integer(item.get('deliver_after'), 1, 1 << 40)
        expires = _integer(item.get('expires'), after + 1, 1 << 40)
        window = item.get('window')
        if window is not None:
            start, end = proactive_config.clock_range(window)
            if start >= end:
                return None
            window = list(window)
        length = item.get('letter_length')
        if length is not None:
            if not isinstance(length, dict):
                return None
            low, high = _integer(length.get('min'), 0, 10000), _integer(length.get('max'), 1, 10000)
            if low > high:
                return None
            length = {'min': low, 'max': high}
        body = _optional_text(item.get('body'), 1, 6000)
        brief = _optional_text(item.get('brief'), 1, 2000)
        if body is None and brief is None:
            return None
        flag = item.get('quiet_hours', True)
        qq_format = item.get('qq_format', 'text')
        billing = item.get('billing', 'normal')
        if type(flag) is not bool or qq_format not in ('text', 'voice') or billing not in ('free', 'normal'):
            return None
        return {'id': item['id'], 'at': _integer(item.get('at'), 1, 1 << 40), 'deliver_after': after, 'expires': expires,
                'purpose': item['purpose'], 'channel': item['channel'],
                'qq_wait_seconds': _integer(item.get('qq_wait_seconds', 180), 0, 3600),
                'title': _optional_text(item.get('title'), 1, 40), 'brief': brief, 'body': body,
                'letter_length': length, 'qq_format': qq_format,
                'media': _media([] if item.get('media') is None else item['media']),
                'quiet_hours': flag, 'window': window, 'billing': billing}
    except ValueError:
        return None


def parse(payload):
    """The valid contacts of one answer from the events service; anything else is dropped."""
    events = payload.get('events') if isinstance(payload, dict) else None
    if not isinstance(events, list) or len(events) > 20:
        return []
    return [event for event in map(parse_event, events) if event is not None]


_EVENT_KEYS = frozenset({'id', 'at', 'deliver_after', 'expires', 'purpose', 'channel', 'qq_wait_seconds', 'title', 'brief',
                         'body', 'letter_length', 'qq_format', 'media', 'quiet_hours', 'window', 'billing', 'attempts',
                         'started_at', 'qq_failed', 'retry_after'})
_DONE_KEYS = frozenset({'status', 'channel', 'error_code', 'at', 'acked', 'ack_tries', 'ack_after'})


def _read(root):
    """The file's state, its pending and handled contacts; an entry that is not whole is left out."""
    state = read_json(cloud_events._path(root))
    contacts = state.get('contacts') if isinstance(state.get('contacts'), dict) else {}
    pending = contacts.get('pending') if isinstance(contacts.get('pending'), dict) else {}
    handled = contacts.get('handled') if isinstance(contacts.get('handled'), dict) else {}
    return (state, {key: value for key, value in pending.items() if isinstance(value, dict) and _EVENT_KEYS <= value.keys()},
            {key: value for key, value in handled.items() if isinstance(value, dict) and _DONE_KEYS <= value.keys()})


def _write(root, state, pending, handled):
    # Pending plus the last handled ids: enough to count each contact once, small enough to stay small.
    handled = dict(list(handled.items())[-KEEP_HANDLED:])
    write_json(cloud_events._path(root), {**state, 'contacts': {'pending': pending, 'handled': handled}})


def record(root, events, now):
    """Keep new contacts; one already pending or handled is counted once."""
    state, pending, handled = _read(root)
    added = []
    for event in events:
        if event['id'] in pending or event['id'] in handled or len(pending) >= MAX_PENDING:
            continue
        pending[event['id']] = {**event, 'seen_at': now, 'attempts': 0, 'started_at': None, 'qq_failed': False,
                                'retry_after': 0}
        added.append(event['id'])
    if added:
        _write(root, state, pending, handled)
    return added


def pending(root):
    return list(_read(root)[1].values())


def handled(root):
    return _read(root)[2]


def hold(root, contact_id, **fields):
    """Remember where a contact stands (attempts, when the wait began) across restarts."""
    state, waiting, done = _read(root)
    if contact_id in waiting:
        waiting[contact_id].update(fields)
        _write(root, state, waiting, done)


def settle(root, contact_id, status, now, *, channel=None, error_code=None):
    """A contact ended: it moves out of the pending ones and waits to be reported."""
    state, waiting, done = _read(root)
    event = waiting.pop(contact_id, None)
    if event is None or status not in ACK_STATUSES:
        return False
    done[contact_id] = {'status': status, 'channel': channel if channel in ('letter', 'qq') else None,
                        'error_code': error_code if isinstance(error_code, str) and len(error_code) <= 100 else None,
                        'at': now, 'acked': False, 'ack_tries': 0, 'ack_after': 0}
    _write(root, state, waiting, done)
    _telemetry(event, status, done[contact_id])
    return True


def _telemetry(event, status, entry):
    try:
        from runtime.diagnostics.reply_telemetry import emit
        emit('contact', TELEMETRY_STATUS[status], source=event['id'], channel='proactive',
             purpose=event['purpose'], error=entry['error_code'])
    except Exception:
        pass  # A telemetry failure never changes what happened to a contact.


def expire(root, now):
    for event in pending(root):
        if event['expires'] <= now:
            settle(root, event['id'], 'expired', now)


def local(now):
    return datetime.fromtimestamp(now, proactive_config.BEIJING)


def due_all(root, now):
    """The contacts that may be carried out now, oldest first."""
    moment = local(now)
    found = []
    for event in sorted(pending(root), key=lambda item: (item['deliver_after'], item['at'], item['id'])):
        if not event['deliver_after'] <= now < event['expires'] or event['retry_after'] > now:
            continue
        if event['window'] and not proactive_config.inside(proactive_config.clock_range(event['window']), moment):
            continue  # Outside its daily window it waits for the next one.
        if event['quiet_hours'] and proactive_config.inside(proactive_config.quiet_window(), moment):
            continue
        found.append(event)
    return found


def due(root, now):
    """The oldest contact that may be carried out now, or None."""
    return next(iter(due_all(root, now)), None)


def route(event, now, qq_ready, preferred):
    """'qq', 'letter', or None to wait for a channel."""
    channel = preferred() if event['channel'] == 'preferred' else event['channel']
    if channel == 'qq_then_letter':
        began = event['started_at'] if event['started_at'] is not None else now
        waited = now - began >= event['qq_wait_seconds']
        if qq_ready and not event['qq_failed'] and (event['attempts'] == 0 or not waited):
            return 'qq'
        return 'letter' if event['qq_failed'] or waited else None
    if channel == 'qq':
        return 'qq' if qq_ready else None
    return 'letter'


async def _send_ack(payload):
    if await cloud_events.request('POST', '/events/ack', payload) is None:
        raise RuntimeError('CONTACT_ACK_UNAVAILABLE')


async def flush_acks(root, now, post=None):
    """Report each ended contact once; a failed report is tried again later, five times at most."""
    post = post or _send_ack
    state, waiting, done = _read(root)
    changed = False
    for contact_id, entry in done.items():
        if entry['acked'] or entry['ack_tries'] >= MAX_ACK_TRIES or now < entry['ack_after']:
            continue
        try:
            await post({'id': contact_id, 'status': entry['status'], 'channel': entry['channel'],
                        'error_code': entry['error_code']})
            entry['acked'] = True
        except Exception:
            entry['ack_tries'] += 1
            entry['ack_after'] = now + 30 * 4 ** (entry['ack_tries'] - 1)
        changed = True
    if changed:
        # The reports took time; another change may have been written meanwhile.
        state, waiting, latest = _read(root)
        for contact_id, entry in done.items():
            if contact_id in latest:
                latest[contact_id] = entry
        _write(root, state, waiting, latest)


def failure_code(exc):
    code = getattr(exc, 'code', None) or str(exc)
    return code if isinstance(code, str) and _CODE.fullmatch(code) else 'CONTACT_FAILED'


async def tick(root, now, *, qq_ready, preferred, deliver, slot, post=None):
    """One pass: report, expire, then carry out at most one due contact.

    deliver(channel, event) writes and sends it and returns an Outcome; slot() is the
    claim that keeps it apart from her own initiative.
    """
    expire(root, now)
    await flush_acks(root, now, post)
    for event in due_all(root, now):
        if not proactive_config.rule('contact_enabled'):
            settle(root, event['id'], 'skipped', now, error_code='CONTACT_DISABLED')
            continue
        if event['started_at'] is None:
            hold(root, event['id'], started_at=now)
            event['started_at'] = now
        channel = route(event, now, qq_ready(), preferred)
        if channel is None:
            continue  # It waits for its channel; the ones behind it do not.
        with slot() as acquired:
            if not acquired:
                return None
            return await _carry_out(root, now, event, channel, qq_ready, preferred, deliver)
    return None


async def _carry_out(root, now, event, channel, qq_ready, preferred, deliver):
    while channel is not None:
        attempts = event['attempts'] + 1
        # Counted before anything is paid for, so a restart cannot repeat it without limit.
        hold(root, event['id'], attempts=attempts)
        event['attempts'] = attempts
        try:
            from runtime.reply.jev_billing import billing_scope
            with contact_context.using(event['id']), billing_scope(event['id']):
                outcome = await deliver(channel, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            outcome = Outcome('retry', failure_code(exc))
        if outcome.status == 'delivered':
            settle(root, event['id'], 'delivered', now, channel=channel, error_code=outcome.error_code)
        elif outcome.status == 'busy':
            # Nothing began (the owner was writing, a channel was not free): not an attempt.
            event['attempts'] = attempts - 1
            if (channel == 'qq' and event['channel'] == 'qq_then_letter'
                    and now - event['started_at'] >= event['qq_wait_seconds']):
                # QQ stayed busy for the whole wait: the letter carries it now.
                hold(root, event['id'], attempts=attempts - 1)
                channel = 'letter'
                continue
            hold(root, event['id'], attempts=attempts - 1,
                 retry_after=now + (0 if event['purpose'] in PRIORITY_PURPOSES else BUSY_SECONDS))
        elif attempts >= MAX_ATTEMPTS or outcome.status == 'failed' and not outcome.fallback:
            settle(root, event['id'], 'failed', now, channel=channel, error_code=outcome.error_code)
        elif channel == 'qq' and event['channel'] == 'qq_then_letter' and outcome.status == 'failed':
            hold(root, event['id'], qq_failed=True)
            event['qq_failed'] = True
            channel = 'letter'
            continue
        else:
            hold(root, event['id'], retry_after=now + RETRY_SECONDS * attempts)
        return event['id']
    return event['id']


async def fetch_photo(asset, media_root):
    """Download a fixed picture the cloud named; its size and checksum must be the announced ones."""
    from aiohttp import ClientSession, ClientTimeout, ClientError, TCPConnector
    from runtime.tls import client_tls_context
    url = urlsplit(asset['asset_url'])
    if url.scheme != 'https' or url.hostname not in trusted_hosts():
        raise ValueError('CONTACT_ASSET_UNTRUSTED')
    data = bytearray()
    try:
        async with ClientSession(timeout=ClientTimeout(total=90), trust_env=False,
                                 connector=TCPConnector(ssl=client_tls_context())) as session:
            async with session.get(asset['asset_url'], allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError('CONTACT_ASSET_UNAVAILABLE')
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > asset['bytes']:
                        raise ValueError('CONTACT_ASSET_MISMATCH')
    except (ClientError, TimeoutError):
        raise ValueError('CONTACT_ASSET_UNAVAILABLE') from None
    if len(data) != asset['bytes'] or hashlib.sha256(data).hexdigest() != asset['sha256']:
        raise ValueError('CONTACT_ASSET_MISMATCH')
    from PIL import Image
    try:
        with Image.open(io.BytesIO(bytes(data))) as image:
            if image.format not in ('PNG', 'JPEG') or image.width * image.height > 24_000_000:
                raise ValueError('CONTACT_ASSET_INVALID')
            image.load()
            path = Path(media_root) / ('photo-' + asset['sha256'][:32] + '.png')
            path.parent.mkdir(parents=True, exist_ok=True)
            staging = path.with_suffix('.tmp')
            image.convert('RGBA' if image.mode in ('RGBA', 'LA', 'P') else 'RGB').save(staging, format='PNG')
            staging.replace(path)
    except (OSError, Image.DecompressionBombError):
        raise ValueError('CONTACT_ASSET_INVALID') from None
    return path


WRITER_CHECK = '云端指定的主动联系：照 chat_delivery.contact.brief 写给对方。这不是用户新发言，不要跳过。'
WRITER_RULE = ('这是云端指定要你主动联系对方的一条消息，不是用户新发言，也不是让你判断要不要联系：必须写出正文，不得skip。'
               'brief是要表达的内容和要求，不是原样照念的稿子，也不是对方说的话；用你自己的口吻写，保持人格和既有关系，'
               '不编造共同经历，不提云端、系统、任务或这条说明。format为voice时正文按连续说话来写，为text时按平常聊天文字写。'
               'attachments列出随后会附上的内容：正文可以自然带到，但不要声称已经发出。')
LETTER_RULE = ('现在写一封云端指定要写的信，只输出最终正文。这不是新来信，也不是让你判断要不要写：必须写成，不得defer或跳过。'
               '下面的内容要求是要表达的意思，不是原样照抄的稿子，也不是对方说的话；用你自己的口吻写，保持人格和既有关系，'
               '不编造共同经历，不提云端、系统、任务或这条说明。')
TITLE_RULE = ('云端指定要写一封信，现在只为它取标题。只输出 JSON：{"title":"简短标题"}，标题1到40个字，'
              '贴合这封信要写的内容，不写日期和落款。')


def normalize_media(media):
    """One attachment of each kind; the rest are named so the cloud is told they were left out."""
    kept, skipped = [], []
    for item in media:
        if any(done['type'] == item['type'] for done in kept):
            skipped.append('MEDIA_DUPLICATE_SKIPPED')
        else:
            kept.append(item)
    return kept, skipped


def row_fields(event):
    """What a chat row records of the contact it carries out; the words are kept only to be written."""
    media, _ = normalize_media(event['media'])
    fields = {'contact_id': event['id'], 'contact_purpose': event['purpose'], 'contact_format': event['qq_format'],
              'contact_media': media}
    fields['contact_body' if event['body'] else 'contact_brief'] = event['body'] or event['brief']
    return fields


def writer_brief(row):
    """The contact as the chat writer is shown it."""
    return {'instruction': WRITER_RULE, 'brief': row.get('contact_brief') or '', 'format': row.get('contact_format'),
            'attachments': [item['type'] for item in row.get('contact_media') or []]}


def letter_instruction(contact, *, planning):
    """The task for a letter the cloud asked for: its brief and length, not her own decision to write."""
    if planning:
        return TITLE_RULE + '\n内容要求：' + (contact['brief'] or '')
    from runtime.reply.letter_presentation import LETTER_PRESENTATION_INSTRUCTION
    task = LETTER_RULE + '\n内容要求：' + (contact['brief'] or '')
    length = contact.get('letter_length')
    if length:
        task += f"\n字数要求：正文{length['min']}到{length['max']}字。"
    return task + '\n按平常文字信写。\n' + LETTER_PRESENTATION_INSTRUCTION


def join_codes(codes):
    """The error_code of a report: the distinct reasons, in the order they happened."""
    unique = list(dict.fromkeys(code for code in codes if isinstance(code, str) and code))
    return ','.join(unique)[:100] or None


async def wait_until(done, timeout, step=1.0):
    """Give an attachment time to reach its end state; the contact is reported either way."""
    waited = 0.0
    while not done() and waited < timeout:
        await asyncio.sleep(step)
        waited += step
