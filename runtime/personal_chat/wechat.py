"""Official Weixin transport; delivery persistence belongs to the shared service."""
import asyncio
from contextlib import suppress

import aiohttp

from .events import owner_message
from .probe import checked_url, wechat_request


async def _read_until_stopped(session, credentials, cursor, stop_event):
    read = asyncio.create_task(wechat_request(
        session, credentials["base"], "/ilink/bot/getupdates",
        token=credentials["token"], body={"get_updates_buf": cursor}))
    stop = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait((read, stop), return_when=asyncio.FIRST_COMPLETED)
        if stop in done:
            return None
        return await read
    finally:
        for task in (read, stop):
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task


def _sender(session, credentials, event, context_token):
    attempted = False

    async def send(text):
        nonlocal attempted
        if attempted:
            raise RuntimeError("WECHAT_SEND_ALREADY_ATTEMPTED")
        if not isinstance(text, str) or not text.strip() or len(text) > 10000:
            raise ValueError("WECHAT_REPLY_INVALID")
        # Reserve even when the HTTP acknowledgement is lost. Only the shared
        # durable service may decide how an uncertain exchange is recovered.
        attempted = True
        return await wechat_request(
            session, credentials["base"], "/ilink/bot/sendmessage",
            token=credentials["token"], body={"msg": {
                "from_user_id": "", "to_user_id": event.owner_id,
                "client_id": event.exchange_id, "message_type": 2,
                "message_state": 2, "context_token": context_token,
                "item_list": [{"type": 1, "text_item": {"text": text}}],
            }})

    return send


async def run_wechat(credentials, handle_message, stop_event, *, cursor_store=None):
    """Run one authorized owner account, with no model or memory logic here.

    ``handle_message(event, send)`` must durably deduplicate ``exchange_id`` and
    mark sending before awaiting ``send(text)``. Send returns the checked API
    response (an empty object is valid). It never retries a delivery.

    Optional cursor_store.load()/save(cursor) are synchronous. A failed handler
    exits without saving the batch cursor, so replay cannot drop later messages;
    already processed exchanges must be deduplicated by the shared service.
    """
    credentials = dict(credentials)
    if any(not isinstance(credentials.get(k), str) or not credentials[k].strip()
           for k in ("base", "token", "account", "owner")):
        raise ValueError("WECHAT_CREDENTIALS_INVALID")
    credentials["base"] = checked_url(credentials["base"])
    cursor = cursor_store.load() if cursor_store is not None else ""
    if not isinstance(cursor, str):
        raise ValueError("WECHAT_CURSOR_INVALID")
    failures = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
        while not stop_event.is_set():
            try:
                batch = await _read_until_stopped(session, credentials, cursor, stop_event)
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                batch = None
            except aiohttp.ClientResponseError as exc:
                if exc.status != 429 and exc.status < 500:
                    raise RuntimeError("WECHAT_POLL_REJECTED") from None
                batch = None
            if stop_event.is_set():
                break
            if batch is None:
                failures += 1
                if failures > 6:
                    raise RuntimeError("WECHAT_POLL_UNAVAILABLE")
                try:
                    await asyncio.wait_for(stop_event.wait(), min(2 ** (failures - 1), 30))
                except asyncio.TimeoutError:
                    pass
                continue
            failures = 0
            messages = batch.get("msgs", [])
            next_cursor = batch.get("get_updates_buf", cursor)
            if not isinstance(messages, list) or not isinstance(next_cursor, str):
                raise RuntimeError("WECHAT_BATCH_INVALID")
            for raw in messages:
                if stop_event.is_set():
                    return  # Leave cursor unchanged for the unfinished batch.
                event = owner_message("wechat", raw, account_id=credentials["account"],
                                      owner_id=credentials["owner"])
                if event is None:
                    continue
                context = raw.get("context_token")
                if not isinstance(context, str) or not context:
                    raise RuntimeError("WECHAT_REPLY_CONTEXT_MISSING")
                await handle_message(event, _sender(session, credentials, event, context))
            if cursor_store is not None:
                cursor_store.save(next_cursor)
            cursor = next_cursor
