"""Weixin transport; delivery persistence belongs to the shared service."""
import asyncio
from contextlib import suppress

import aiohttp

from .events import owner_message, combine, mergeable_by_sent_time
from .probe import checked_url, wechat_request


class WechatAuthRequired(ValueError):
    pass


async def _notify_lifecycle(session, credentials, endpoint):
    """Best-effort lifecycle notify; it must never block channel startup/shutdown."""
    try:
        await asyncio.wait_for(
            wechat_request(
                session,
                credentials["base"],
                endpoint,
                token=credentials["token"],
                body={},
            ),
            5,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return False
    return True


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

    image_attempted = False
    async def send_image(path):
        nonlocal image_attempted
        if image_attempted:
            raise RuntimeError('WECHAT_SEND_ALREADY_ATTEMPTED')
        image_attempted = True
        from .wechat_images import upload
        item = await upload(session, credentials, event.owner_id, path)
        return await wechat_request(session, credentials['base'], '/ilink/bot/sendmessage',
            token=credentials['token'], body={'msg':{'from_user_id':'','to_user_id':event.owner_id,
            'client_id':event.exchange_id + '-sticker','message_type':2,'message_state':2,
            'context_token':context_token,'item_list':[item]}})

    def delivery_confirmation(response):
        if isinstance(response, dict):
            identifier = response.get("message_id")
            if isinstance(identifier, str) and identifier.strip():
                return "PLATFORM_ACCEPTED"
        return "UNCONFIRMED"

    send.image = send_image
    send.is_available = lambda: not session.closed
    send.for_exchange = lambda merged: _sender(session, credentials, merged, context_token)
    send.delivery_confirmation = delivery_confirmation
    return send


async def run_wechat(credentials, handle_message, stop_event, *, cursor_store=None, merge_seconds=2,
                     reconnect_delay=1, state_callback=None):
    """Run one authorized owner account, with no model or memory logic here.

    ``handle_message(event, send)`` must durably deduplicate ``exchange_id`` and
    mark sending before awaiting ``send(text)``. Send returns the checked API
    response (an empty object is valid). It never retries a delivery.

    Optional cursor_store.load()/save(cursor) are synchronous. A failed handler
    exits without saving the batch cursor, so replay cannot drop later messages;
    already processed exchanges must be deduplicated by the shared service.
    The transport reports PLATFORM_ACCEPTED only when sendmessage returns a
    non-empty message_id. A ret=0 response without message_id is UNCONFIRMED.
    """
    credentials = dict(credentials)
    if any(not isinstance(credentials.get(k), str) or not credentials[k].strip()
           for k in ("base", "token", "account", "owner")):
        raise ValueError("WECHAT_CREDENTIALS_INVALID")
    credentials["base"] = checked_url(credentials["base"])
    cursor = cursor_store.load() if cursor_store is not None else ""
    if not isinstance(cursor, str):
        raise ValueError("WECHAT_CURSOR_INVALID")
    if reconnect_delay <= 0:
        raise ValueError("WECHAT_RECONNECT_DELAY_INVALID")
    failures = 0
    connected = False

    def publish(state):
        if callable(state_callback):
            state_callback(state)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
        await _notify_lifecycle(session, credentials, "/ilink/bot/msg/notifystart")
        try:
            while not stop_event.is_set():
                try:
                if not connected:
                    publish("CONNECTING" if failures == 0 else "RECONNECTING")
                batch = await _read_until_stopped(session, credentials, cursor, stop_event)
                except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                    batch = None
                except ValueError as exc:
                    if str(exc) == "WECHAT_SESSION_STALE":
                        publish("AUTH_REQUIRED")
                        raise WechatAuthRequired("WECHAT_SESSION_STALE") from None
                    raise
                except aiohttp.ClientResponseError as exc:
                    if exc.status in {401, 403}:
                        publish("AUTH_REQUIRED")
                        raise WechatAuthRequired("WECHAT_AUTH_REQUIRED") from None
                    if exc.status != 429 and exc.status < 500:
                        raise RuntimeError("WECHAT_POLL_REJECTED") from None
                    batch = None
                if stop_event.is_set():
                    break
                if batch is None:
                    connected = False
                    publish("RECONNECTING")
                    failures += 1
                    try:
                        await asyncio.wait_for(
                            stop_event.wait(),
                            min(30, reconnect_delay * 2 ** min(failures - 1, 5)),
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue
                failures = 0
                connected = True
                publish("CONNECTED")
                messages = batch.get("msgs", [])
                next_cursor = batch.get("get_updates_buf", cursor)
                if not isinstance(messages, list) or not isinstance(next_cursor, str):
                    raise RuntimeError("WECHAT_BATCH_INVALID")
                # Collect a short burst without acknowledging its cursor before delivery.
                deadline = asyncio.get_running_loop().time() + 8
                while merge_seconds > 0 and messages and len(messages) < 32 and not stop_event.is_set():
                    wait = min(merge_seconds, deadline - asyncio.get_running_loop().time())
                    if wait <= 0:
                        break
                    try:
                        extra = await asyncio.wait_for(_read_until_stopped(session, credentials, next_cursor, stop_event), wait)
                    except (asyncio.TimeoutError, aiohttp.ClientError):
                        break
                    if not extra or not extra.get('msgs'):
                        break
                    if not isinstance(extra['msgs'], list) or not isinstance(extra.get('get_updates_buf'), str):
                        raise RuntimeError('WECHAT_BATCH_INVALID')
                    messages = [*messages, *extra['msgs']]
                    next_cursor = extra['get_updates_buf']
                grouped = []
                for raw in messages:
                    if stop_event.is_set():
                        return  # Leave cursor unchanged for the unfinished batch.
                    event = owner_message("wechat", raw, account_id=credentials["account"],
                                          owner_id=credentials["owner"])
                    if event is None:
                        continue
                    last_event = event
                    context = raw.get("context_token")
                    if not isinstance(context, str) or not context:
                        raise RuntimeError("WECHAT_REPLY_CONTEXT_MISSING")
                    if grouped and (merge_seconds == 0 or event.text.strip() == '/连接测试' or grouped[-1][0].text.strip() == '/连接测试'
                                    or len(grouped[-1][0].text)+len(event.text)+1 > 10000
                                    or not mergeable_by_sent_time(grouped[-1][2], event, merge_seconds)):
                        previous, token, _ = grouped.pop()
                        await handle_message(previous, _sender(session, credentials, previous, token))
                    if grouped:
                        previous, _, _ = grouped.pop()
                        event = combine([previous, event])
                    grouped.append((event, context, last_event))
                for event, context, _ in grouped:
                    await handle_message(event, _sender(session, credentials, event, context))
                if cursor_store is not None:
                    cursor_store.save(next_cursor)
                cursor = next_cursor
        finally:
            await _notify_lifecycle(session, credentials, "/ilink/bot/msg/notifystop")
