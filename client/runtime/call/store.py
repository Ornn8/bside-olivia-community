"""Finished and missed calls: kept in the local state, remembered, and visible to QQ replies.

A finished call becomes memory through the shared canonical outbox (memory_rows), like piano
recordings. Until then, and for a day after, QQ replies see the latest call and any missed call
since the last QQ exchange (recent_context), so she can pick the conversation up.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import uuid

CHUNK_CHARS = 3000
MAX_ROWS = 200
TRANSCRIPT_ROWS = 600
RECENT = timedelta(hours=24)


def _now():
    return datetime.now(timezone.utc)


def _calls(server):
    store = server.store
    if not isinstance(getattr(store, 'calls', None), list):
        store.calls = []
    return store.calls


def _keep(server, row):
    calls = _calls(server)
    calls.append(row)
    del calls[:-MAX_ROWS]
    server._persist_store_state()
    return row


def record_missed(server, reason, now=None):
    now = now or _now()
    return _keep(server, {'call_id': uuid.uuid4().hex, 'started_at': now.isoformat(), 'missed': True, 'reason': reason})


def _clean_transcript(value):
    rows = []
    for item in value if isinstance(value, list) else []:
        if (isinstance(item, dict) and item.get('role') in ('user', 'her') and isinstance(item.get('text'), str)
                and item['text'].strip()):
            rows.append({'role': item['role'], 'text': item['text'].strip()[:1000],
                         **({'interrupted': True} if item.get('interrupted') is True else {})})
    return rows[:TRANSCRIPT_ROWS]


def _clean_notes(value):
    value = value if isinstance(value, dict) else {}
    notes = {'summary': str(value.get('summary') or '')[:600]}
    for key in ('user_shared', 'her_promises'):
        notes[key] = [str(x)[:120] for x in value.get(key) or [] if isinstance(x, str)][:12]
    return notes


def record_call(server, body, now=None):
    """The call page's end report. Returns the stored row (idempotent by call_id)."""
    now = now or _now()
    call_id = body.get('call_id')
    if not isinstance(call_id, str) or not 8 <= len(call_id) <= 64 or not call_id.replace('-', '').isalnum():
        raise ValueError('CALL_END_INVALID')
    for row in _calls(server):
        if row.get('call_id') == call_id:
            return row
    seconds = body.get('seconds')
    seconds = int(seconds) if type(seconds) in (int, float) and 0 <= seconds <= 6 * 3600 else 0
    charge = body.get('charge_cents')
    return _keep(server, {
        'call_id': call_id, 'started_at': (now - timedelta(seconds=seconds)).isoformat(), 'ended_at': now.isoformat(),
        'seconds': seconds, 'pickup': body.get('pickup') if body.get('pickup') in ('answer', 'brief', 'sleepy') else 'answer',
        'reason': str(body.get('reason') or '')[:40],
        'charge_cents': charge if type(charge) is int and charge >= 0 else None,
        'transcript': _clean_transcript(body.get('transcript')), 'notes': _clean_notes(body.get('notes'))})


def _label(row):
    return '她' if row['role'] == 'her' else '用户'


def memory_rows(row):
    """Canonical memory rows of one finished call: the dialogue in chunks, the notes on the first."""
    if row.get('missed') or not row.get('transcript'):
        return
    try:
        when = datetime.fromisoformat(row['ended_at'])
    except (KeyError, TypeError, ValueError):
        return
    chunks, current = [], ''
    for item in row['transcript']:
        line = f"{_label(item)}：{item['text']}\n"
        if current and len(current) + len(line) > CHUNK_CHARS:
            chunks.append(current)
            current = ''
        current += line
    if current:
        chunks.append(current)
    minutes = max(1, round(row.get('seconds', 0) / 60))
    notes = row.get('notes') or {}
    for index, chunk in enumerate(chunks):
        head = f'（用户和她的语音电话记录，约{minutes}分钟' + (f'，第{index + 1}/{len(chunks)}段）\n' if len(chunks) > 1 else '）\n')
        reply = ('通话笔记：' + notes['summary'] + (('；她答应：' + '、'.join(notes['her_promises'])) if notes.get('her_promises') else '')
                 if index == 0 and notes.get('summary') else '（同一通电话的后续）')
        identity = f"{row['call_id']}:{index}"
        yield dict(letter_id='call-' + hashlib.sha256(identity.encode()).hexdigest()[:32], reply_revision=1,
                   content=head + chunk.rstrip(), reply_text=reply, letter_status='COMPLETED', origin='user',
                   created_at=when.timestamp(), private_world_occurred_at=when.isoformat())


def recent_context(server, now=None) -> str:
    """A short note appended to QQ observation context; empty when nothing recent happened."""
    from runtime.call.context import local_now_text
    now = now or _now()
    calls = _calls(server)
    if not calls:
        return ''
    def when(value):
        try:
            stamp = datetime.fromisoformat(value)
            return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
    chats = [when(r.get('life_received_at')) for r in getattr(server.store, 'personal_chats', [])]
    last_chat = max((c for c in chats if c), default=datetime.min.replace(tzinfo=timezone.utc))
    parts = []
    missed = [r for r in calls if r.get('missed') and when(r['started_at']) > last_chat
              and now - when(r['started_at']) <= RECENT]
    if missed:
        parts.append(f"对方在{local_now_text(datetime.fromisoformat(missed[-1]['started_at']))}给你打过电话，"
                     f"你当时在洗澡没接到{'（打了' + str(len(missed)) + '次）' if len(missed) > 1 else ''}。")
    done = [r for r in calls if not r.get('missed') and r.get('ended_at')
            and now - datetime.fromisoformat(r['ended_at']) <= RECENT]
    if done:
        last = done[-1]
        summary = (last.get('notes') or {}).get('summary') or ''
        parts.append(f"你们在{local_now_text(datetime.fromisoformat(last['ended_at']))}通过电话，"
                     f"约{max(1, round(last.get('seconds', 0) / 60))}分钟" + (f"，聊了：{summary}" if summary else '') + '。')
    return ('\n\n[语音电话]' + ''.join(parts)) if parts else ''
