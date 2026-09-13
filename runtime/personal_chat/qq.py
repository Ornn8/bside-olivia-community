"""Owner-only OneBot transport; the shared service owns durable delivery state."""
import asyncio
import json
import logging
import uuid

import aiohttp

from .events import owner_message, combine
from .probe import checked_url

log = logging.getLogger(__name__)


def _ack(raw):
    if raw.get("status") != "ok" or raw.get("retcode") != 0 or not isinstance(raw.get("data"), dict):
        raise RuntimeError("QQ_ACTION_UNCONFIRMED")
    return raw["data"]


async def _connection(ws, account_id, owner_id, handle_message, stop_event, ack_timeout, merge_seconds=2):
    login_echo = uuid.uuid4().hex
    await ws.send_json({"action": "get_login_info", "echo": login_echo})
    async with asyncio.timeout(ack_timeout):
        while True:
            message = await ws.receive()
            if message.type != aiohttp.WSMsgType.TEXT:
                raise ConnectionError("QQ_LOGIN_DISCONNECTED")
            try:
                raw = json.loads(message.data)
            except (ValueError, TypeError):
                continue
            if isinstance(raw, dict) and raw.get("echo") == login_echo:
                if str(_ack(raw).get("user_id", "")) != account_id:
                    raise ValueError("QQ_ACCOUNT_MISMATCH")
                break

    queue = asyncio.Queue(maxsize=32)
    pending = {}
    processing = False

    async def send_item(item):
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        pending[echo] = future
        try:
            await ws.send_json({"action": "send_private_msg", "echo": echo,
                "params": {"user_id": int(owner_id),
                    "message": [item]}})
            result = _ack(await asyncio.wait_for(future, ack_timeout))
            identifier = result.get("message_id")
            if isinstance(identifier, bool) or not isinstance(identifier, (str, int)) or not str(identifier):
                raise RuntimeError("QQ_SEND_UNCONFIRMED")
            return str(identifier)
        finally:
            pending.pop(echo, None)
            if not future.done():
                future.cancel()

    async def send(text):
        if not isinstance(text, str) or not text.strip() or len(text) > 10000:
            raise ValueError('QQ_REPLY_INVALID')
        return await send_item({'type': 'text', 'data': {'text': text}})

    async def send_audio(path):
        from pathlib import Path
        return await send_item({'type': 'record', 'data': {'file': Path(path).resolve().as_uri()}})

    send.audio = send_audio
    send.is_available = lambda: not ws.closed

    async def reader():
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                raw = json.loads(message.data)
            except (ValueError, TypeError):
                continue
            if not isinstance(raw, dict):
                continue
            echo = raw.get("echo")
            future = pending.get(echo) if isinstance(echo, str) else None
            if future is not None:
                if not future.done():
                    future.set_result(raw)
                continue
            event = owner_message("qq", raw, account_id=account_id, owner_id=owner_id)
            if event:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    # Never block the ACK reader behind generation work.
                    raise RuntimeError("QQ_OWNER_QUEUE_FULL") from None

    async def worker():
        nonlocal processing
        carry = None
        while True:
            event = carry or await queue.get()
            carry = None
            events = [event]
            processing = True
            try:
                deadline = asyncio.get_running_loop().time() + 8
                while event.text.strip() != '/连接测试' and len(events) < 16:
                    wait = min(merge_seconds, deadline - asyncio.get_running_loop().time())
                    if wait <= 0:
                        break
                    try:
                        next_event = await asyncio.wait_for(queue.get(), wait)
                    except asyncio.TimeoutError:
                        break
                    if next_event.text.strip() == '/连接测试' or sum(len(e.text)+1 for e in events) + len(next_event.text) > 10000:
                        carry = next_event
                        break
                    events.append(next_event)
                await handle_message(combine(events), send)
            except Exception:
                # Neither generation nor ambiguous sends are retried here.
                raise RuntimeError("QQ_MESSAGE_HANDLER_FAILED") from None
            finally:
                processing = False
                for _ in events:
                    queue.task_done()

    reader_task = asyncio.create_task(reader())
    worker_task = asyncio.create_task(worker())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        await asyncio.wait({reader_task, worker_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if worker_task.done():
            worker_task.result()
        if reader_task.done():
            reader_task.result()
            if not stop_event.is_set() and (processing or not queue.empty()):
                raise RuntimeError("QQ_CONNECTION_LOST_DURING_EXCHANGE")
    finally:
        for task in (reader_task, worker_task, stop_task):
            task.cancel()
        await asyncio.gather(reader_task, worker_task, stop_task, return_exceptions=True)


async def run_qq(url, token, account_id, owner_id, handle_message, stop_event,
                 *, ack_timeout=30, reconnect_delay=1, merge_seconds=2):
    """Run until stopped. send(text) returns a confirmed platform message ID.

    The handler must persist a sending reservation before calling send: a timeout
    or disconnect cannot establish whether the platform delivered the message.
    """
    url = checked_url(url, local=True)
    if not isinstance(token, str) or len(token) < 16:
        raise ValueError("ONEBOT_TOKEN_REQUIRED")
    account_id, owner_id = str(account_id), str(owner_id)
    if not account_id.isascii() or not account_id.isdigit() or not owner_id.isascii() or not owner_id.isdigit() or account_id == owner_id:
        raise ValueError("QQ_ACCOUNT_OWNER_INVALID")
    if ack_timeout <= 0 or reconnect_delay <= 0:
        raise ValueError("QQ_TIMEOUT_INVALID")
    failures = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
        while not stop_event.is_set():
            try:
                async with session.ws_connect(url, headers={"Authorization": "Bearer " + token}, heartbeat=20) as ws:
                    await _connection(ws, account_id, owner_id, handle_message, stop_event, ack_timeout, merge_seconds)
            except (aiohttp.ClientError, ConnectionError, TimeoutError):
                log.warning("QQ_TRANSPORT_DISCONNECTED")
            if stop_event.is_set():
                break
            failures += 1
            try:
                await asyncio.wait_for(stop_event.wait(), min(30, reconnect_delay * 2 ** min(failures - 1, 5)))
            except TimeoutError:
                pass
