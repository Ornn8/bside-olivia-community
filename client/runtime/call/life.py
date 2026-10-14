"""A finished call enters her life the way a QQ exchange does.

The dialogue goes through the same steps as a delivered chat: a private-world delivery, the daily-life
exchange extraction (what she said she will do or did becomes her record; a call while she rests
interrupts her rest), and the relationship signal of the exchange. Each call is absorbed once; a
failed attempt is retried a few times when the call page next reads the call config.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import logging

log = logging.getLogger(__name__)

ATTEMPTS = 3
TEXT_CHARS = 3000      # each side, the end of the call kept when longer (extraction input bound)
_tasks: dict[str, asyncio.Task] = {}


def _sides(row):
    user = '\n'.join(r['text'] for r in row.get('transcript') or [] if r['role'] == 'user')
    her = '\n'.join(r['text'] for r in row.get('transcript') or [] if r['role'] == 'her')
    return user[-TEXT_CHARS:], her[-TEXT_CHARS:]


def pending(row) -> bool:
    return (not row.get('missed') and bool(row.get('transcript')) and row.get('life_status') not in ('COMMITTED', 'SKIPPED')
            and row.get('life_attempts', 0) < ATTEMPTS)


def absorb_later(server, row):
    """Schedule `absorb` for this call (once at a time)."""
    if not pending(row):
        return None
    task = _tasks.get(row['call_id'])
    if task is not None and not task.done():
        return task
    task = _tasks[row['call_id']] = asyncio.get_running_loop().create_task(absorb(server, row))
    return task


async def absorb(server, row):
    runtime = getattr(server, 'daily_life_runtime', None)
    if runtime is None:
        row['life_status'] = 'SKIPPED'
        return
    user, her = _sides(row)
    if not her.strip():
        row['life_status'] = 'SKIPPED'
        return
    row['life_attempts'] = row.get('life_attempts', 0) + 1
    letter_id = 'call-' + row['call_id']
    source_id = f'reply:{letter_id}:1'
    ended, started = datetime.fromisoformat(row['ended_at']), datetime.fromisoformat(row['started_at'])
    try:
        committer = getattr(server, 'private_world_committer', None)
        delivered = False
        if committer is not None:
            from runtime.memory.private_world_delivery import DeliveryEvent, DeliveryStatus
            status = committer.commit(DeliveryEvent(
                delivery_id=letter_id + ':1', occurred_at=ended,
                semantic_key='canonical.' + hashlib.sha256(letter_id.encode()).hexdigest(),
                canonical_reply_sha256=hashlib.sha256(her.encode()).hexdigest()))
            delivered = status in {DeliveryStatus.COMMITTED, DeliveryStatus.DUPLICATE}
        await runtime.consume_exchange(source_id, user, her, occurred_at=ended, received_at=started)
        signal = runtime.store.exchange_relationship(source_id, user, her)
        relationship = getattr(server, 'private_world_relationship_committer', None)
        if signal is not None and relationship is not None and delivered:
            relationship.commit_exchange(letter_id + ':1', user, her, signal, occurred_at=ended)
            refresh = getattr(server, '_refresh_contact_relationship_projection', None)
            if callable(refresh):
                refresh()
        row['life_status'] = 'COMMITTED'
        row.pop('life_error', None)
    except Exception as exc:  # a failed extraction never affects the call or its memory
        row['life_status'] = 'FAILED'
        row['life_error'] = type(exc).__name__
        log.info('call life absorption failed: %s', type(exc).__name__)
    finally:
        persist = getattr(server, '_persist_store_state', None)
        if callable(persist):
            persist()


def retry_pending(server):
    from runtime.call.store import _calls
    for row in _calls(server):
        if pending(row) and row.get('life_status') == 'FAILED':
            absorb_later(server, row)
