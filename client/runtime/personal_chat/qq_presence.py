"""Her QQ life around the chat (owner 2026-10-24).

- Her QQ status follows what she is doing in her world (练琴中, 睡觉中 …), changed at most every ten minutes.
- Once a day she likes his profile card.
- On an evening with something to show she posts a 说说, with a photo she already sent him that day, and may
  change her signature with it.
- His new 说说 get a like and sometimes a comment; his comments under hers get an answer.

Every written word is one call of his QQ model, counted like any reply. Each part has its own switch
(qq_extras.json). Nothing here ever holds up a chat reply: a failure is recorded and tried later.
"""
import asyncio
import json
import os
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHANGHAI = timezone(timedelta(hours=8))
SETTINGS_NAME = 'qq_extras.json'
STATE_NAME = 'qq_presence.json'
DEFAULTS = {'status': True, 'like': True, 'qzone': True, 'gestures': True}
STATUS = {'class': ('学习', '上课中'), 'practice': ('学习', '练琴中'), 'reading': ('学习', '看书中'),
          'meal': ('干饭', '吃饭中'), 'rest': ('困', '休息一下'), 'housework': ('转圈', '收拾屋子'),
          'walk': ('跳跳', '散步中'), 'errand': ('火车', '出门办事'), 'creative': ('学习', '写写画画'),
          'bath_started': ('转圈', '洗澡中'), 'bath_finished': ('茶', '刚洗完澡'), 'shopping': ('拿到红包', '逛街中')}
STATUS_EVERY = 600
POST_HOURS = (20, 23)          # her 说说 goes out in the evening, Shanghai time
POST_RETRY = 3600
WATCH_EVERY = 1800
COMMENTS_PER_DAY = 3
MODEL_TIMEOUT = 90


def _read(path, default):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else dict(default)
    except (OSError, ValueError):
        return dict(default)


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def settings(root):
    value = _read(Path(root) / SETTINGS_NAME, DEFAULTS)
    return {key: value.get(key, default) is not False for key, default in DEFAULTS.items()}


def save_settings(root, value):
    if not isinstance(value, dict) or set(value) != set(DEFAULTS) or any(type(v) is not bool for v in value.values()):
        raise ValueError('QQ_EXTRAS_INVALID')
    _write(Path(root) / SETTINGS_NAME, value)
    return dict(value)


def status_for(snapshot, now):
    """(face name, wording) for what she is doing now, or None when the world cannot say."""
    rhythm = snapshot.get('rhythm') or {}
    if rhythm.get('phase') == 'sleep':
        return ('睡', '睡觉中')
    if rhythm.get('phase') == 'bathing':
        return STATUS['bath_started']
    current = snapshot.get('current') or {}
    local = now.astimezone(SHANGHAI)
    if current.get('activity_kind') == 'rest' and (local.hour >= 23 or local.hour < 7):
        return ('睡', '睡觉中')
    return STATUS.get(current.get('activity_kind'))


def photos_today(rows, now):
    """Photos she sent him on QQ today: [(row id, local path, what it shows)]."""
    day = now.astimezone(SHANGHAI).date()
    found = []
    for row in rows:
        path = row.get('prepared_image')
        if (row.get('channel') != 'qq' or row.get('image_delivery_status') != 'DELIVERED' or not path
                or not Path(path).is_file()):
            continue
        try:
            if datetime.fromtimestamp(float(row.get('created_at', 0)), SHANGHAI).date() != day:
                continue
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        found.append((row['letter_id'], path, str(row.get('image_description') or '')[:240]))
    return found[-4:]


POST_RULES = '''你在 QQ 空间发一条今天的说说。说说他（和你的好友）都能看到。
- 用你自己的口吻，像真人随手发的：一到三句短话，80 字以内；不要话题标签，表情符号最多一个。
- 只写 today 里真实发生的事和你此刻的心情；不编新的经历，不提 AI、应用或生成。
- photos 是你今天已经发给他的照片，可以选一张配图（填它的 id），没有合适的就填 null。
- 今天没什么想发的就让 post 为空字符串。
- signature 是可选的新 QQ 个性签名，20 字以内，和今天的心情有关；不想换就填 null。
只输出 JSON：{"post":"..","photo":"id或null","signature":"..或null"}'''

COMMENT_RULES = '''他刚在 QQ 空间发了一条说说（post），你看到了，想在下面留一句评论。
- 用你平时和他说话的口吻：一句短话，30 字以内，接住他说的内容；别客套，别问一连串问题。
- 他配了图时你看不到图的内容，不要假装看到了；可以只回应文字。
- 不适合评论（比如很私人、很沉重、只是转发）就让 comment 为空字符串，你仍然会点个赞。
只输出 JSON：{"comment":".."}'''

REPLY_RULES = '''他在你的 QQ 空间说说（post）下面评论了你（comment），你来回复他。
- 一句短话，30 字以内，像在评论区里自然地回他；可以俏皮或温柔，按你们的关系来。
- 只回应他这条评论，不编新的经历。
只输出 JSON：{"reply":".."}'''


async def _ask(server, rules, packet, *, request_id, field_names):
    """One call of his QQ model. Returns the parsed JSON object or raises."""
    from llm_gateway import GatewayRequestScope
    from runtime.call.context import persona_text, relationship_text
    from runtime.model_routes import using
    gateway = getattr(server.letters_adapter.gateway, 'primary', server.letters_adapter.gateway)
    system = persona_text(server) + '\n' + relationship_text(server) + '\n\n' + rules
    messages = ({'role': 'system', 'content': system},
                {'role': 'user', 'content': json.dumps(packet, ensure_ascii=False, separators=(',', ':'))})
    with using('qq'):
        result = await asyncio.wait_for(gateway.complete_structured_scoped(messages, request_id=request_id,
            scope=GatewayRequestScope.BACKGROUND_REASONING, response_format={'type': 'json_object'}), MODEL_TIMEOUT)
    text = result.text
    value = json.loads(text[text.find('{'):text.rfind('}') + 1])
    if not isinstance(value, dict) or not set(field_names) <= value.keys():
        raise ValueError('QQ_PRESENCE_ANSWER_INVALID')
    return value


def _clean(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ''


async def _post(server, sender, state, rows, now):
    today = now.astimezone(SHANGHAI).date().isoformat()
    snapshot = server.daily_life_runtime.store.snapshot(now)
    activities = [{'time': item.get('occurred_at'), 'activity': item.get('activity'), 'note': item.get('note')}
                  for item in (snapshot.get('world') or {}).get('today_activities', [])][-8:]
    photos = photos_today(rows, now)
    if not activities and not photos:
        return 'nothing'
    answer = await _ask(server, POST_RULES, {'today': activities, 'photos': [{'id': key, 'shows': text}
                        for key, _, text in photos], 'now': now.astimezone(SHANGHAI).isoformat()},
                        request_id=f'qzone-post:{today}', field_names=('post',))
    post = _clean(answer.get('post'), 300)
    if not post:
        state['posted'] = today  # she chose not to post today
        return 'skipped'
    picture = [path for key, path, _ in photos if key == answer.get('photo')][:1]
    state['post_tid'] = await sender.qzone_post(post, picture)
    state['posted'] = today
    signature = _clean(answer.get('signature'), 20)
    if signature and '\n' not in signature:
        try:
            await sender.signature(signature)
            state['signature'] = signature
        except Exception:
            state['signature_error'] = today
    return 'posted'


async def _watch(server, sender, state, owner, account, now):
    """His new 说说 get a like and maybe a comment; his comments under hers get an answer."""
    from .qzone import Qzone
    today = now.astimezone(SHANGHAI).date().isoformat()
    zone = Qzone(await sender.cookies('user.qzone.qq.com'), account)
    seen = set(state.get('seen_posts') or [])
    his = await zone.posts(owner)
    first_look = 'seen_posts' not in state
    if state.get('comment_day') != today:
        state['comment_day'], state['comments_today'] = today, 0
    for post in his:
        if post['tid'] in seen:
            continue
        seen.add(post['tid'])
        # The first look only learns what is already there; only 说说 from then on are answered.
        if first_look or now.timestamp() - post['created'] > 86400:
            continue
        await zone.like(owner, post['tid'], post['created'])
        if state['comments_today'] >= COMMENTS_PER_DAY or any(c['uin'] == account for c in post['comments']):
            continue
        answer = await _ask(server, COMMENT_RULES, {'post': post['text'], 'pictures': post['pictures']},
                            request_id=f"qzone-comment:{post['tid']}", field_names=('comment',))
        comment = _clean(answer.get('comment'), 60)
        if comment:
            await zone.comment(owner, post['tid'], comment)
            state['comments_today'] += 1
    state['seen_posts'] = sorted(seen)[-200:]
    answered = set(state.get('answered_comments') or [])
    for post in await zone.posts(account, count=5):
        for item in post['comments']:
            key = post['tid'] + ':' + item['id']
            if item['uin'] != owner or key in answered or now.timestamp() - item['created'] > 86400:
                continue
            answered.add(key)
            answer = await _ask(server, REPLY_RULES, {'post': post['text'], 'comment': item['text']},
                                request_id=f"qzone-reply:{post['tid']}:{item['id']}", field_names=('reply',))
            reply = _clean(answer.get('reply'), 60)
            if reply:
                await zone.comment(account, post['tid'], reply, reply_to=(item['id'], owner))
    state['answered_comments'] = sorted(answered)[-400:]


async def tick(server, runtime, initiative, now=None):
    """One pass, every minute while QQ is connected. Never raises."""
    now = now or datetime.now(timezone.utc)
    root = server._state_root()
    target = getattr(initiative, 'target', None)
    if root is None or target is None or runtime['status'].get('qq') != 'CONNECTED' or target[0].channel != 'qq':
        return
    old, sender = target
    # The connection that is up now; the chat's own target may still hold one that has since closed.
    sender = (runtime.get('senders') or {}).get('qq') or sender
    if callable(getattr(sender, 'is_available', None)) and not sender.is_available():
        return
    prefs, path = settings(root), Path(root) / STATE_NAME
    state = _read(path, {})
    stamp, local = now.timestamp(), now.astimezone(SHANGHAI)
    today = local.date().isoformat()
    life = getattr(getattr(server, 'daily_life_runtime', None), 'store', None)
    try:
        if prefs['status'] and life is not None and stamp - state.get('status_at', 0) >= STATUS_EVERY:
            from .qq_faces import FACE_IDS
            wanted = status_for(life.snapshot(now), now)
            if wanted and state.get('status') != list(wanted):
                state['status_at'] = stamp
                await sender.status(FACE_IDS[wanted[0]], wanted[1])
                state['status'] = list(wanted)
        if prefs['like'] and state.get('liked') != today and local.hour >= 9:
            state['liked'] = today  # one try a day, kept or not
            await sender.like(10)
        if (prefs['qzone'] and state.get('posted') != today and POST_HOURS[0] <= local.hour < POST_HOURS[1]
                and stamp >= state.get('post_retry_at', 0) and life is not None):
            state['post_retry_at'] = stamp + POST_RETRY
            state['post_result'] = await _post(server, sender, state, list(server.store.personal_chats), now)
        if prefs['qzone'] and stamp - state.get('watched_at', 0) >= WATCH_EVERY and 8 <= local.hour < 24:
            state['watched_at'] = stamp
            await _watch(server, sender, state, old.owner_id, old.account_id, now)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        code = getattr(exc, 'args', [None])[0]
        state['error'] = code if isinstance(code, str) and code.isupper() else type(exc).__name__
        state['error_at'] = stamp  # kept until the next failure, so a later quiet pass does not hide it
        frame = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
        state['error_where'] = f'{Path(frame.filename).name}:{frame.name}:{frame.lineno}' if frame else None
        try:
            server._safe_log('qq_presence_deferred', error_code=state['error'])
        except Exception:
            pass
    _write(path, state)
