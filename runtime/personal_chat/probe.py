"""Owner-only transport acceptance, deliberately without an LLM or production data.

Run with --help. Only the owner's exact /连接测试 command receives a fixed reply.
Weixin wire format: Tencent/openclaw-weixin@7c04adc docs/protocol.md (MIT).
QQ wire format: OneBot v11, transport supplied by the separately installed NapCat.
"""
import argparse
import asyncio
import base64
import json
import secrets
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from .events import owner_message


def checked_url(url, *, local=False):
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("CHAT_ENDPOINT_INVALID")
    if local:
        if parsed.scheme not in {"ws", "http"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("CHAT_ENDPOINT_NOT_LOOPBACK")
    elif (parsed.scheme != "https" or parsed.port not in {None, 443}
          or not parsed.hostname or not parsed.hostname.endswith(".weixin.qq.com")):
        raise ValueError("WECHAT_ENDPOINT_INVALID")
    return url.rstrip("/")


class ProbeJournal:
    """Reserve before sending: an uncertain send is never automatically duplicated."""
    def __init__(self, root):
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "probe.sqlite3")
        self.db.execute("CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY, state TEXT NOT NULL)")

    def reserve(self, identifier):
        with self.db:
            return self.db.execute("INSERT OR IGNORE INTO sent VALUES (?, 'sending')", (identifier,)).rowcount == 1

    def delivered(self, identifier):
        with self.db:
            self.db.execute("UPDATE sent SET state='sent' WHERE id=?", (identifier,))


def wechat_headers(token=None):
    headers = {"AuthorizationType": "ilink_bot_token", "iLink-App-Id": "bot",
               "iLink-App-ClientVersion": str((2 << 16) | (4 << 8) | 8),
               "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode()}
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


async def wechat_request(session, base, path, *, token=None, body=None, params=None):
    base = checked_url(base)
    if body is not None and token:
        body = {**body, "base_info": {"channel_version": "2.4.8", "bot_agent": "OliviaTransportProbe/0.1"}}
    async with session.request("POST" if body is not None else "GET", base + path,
                               headers=wechat_headers(token), json=body, params=params,
                               allow_redirects=False) as response:
        response.raise_for_status()
        # The live Weixin endpoint labels valid JSON application/octet-stream.
        data = await response.json(content_type=None)
    if not isinstance(data, dict) or data.get("ret", 0) != 0 or data.get("errcode", 0) != 0:
        raise RuntimeError("WECHAT_API_REJECTED")
    return data


async def wechat(args, session, journal):
    from original_client_setup_api import _dpapi_protect, _dpapi_unprotect
    credentials_path = args.data / "wechat.dpapi"
    base = "https://ilinkai.weixin.qq.com"
    if args.login or not credentials_path.exists():
        import qrcode
        qr = await wechat_request(session, base, "/ilink/bot/get_bot_qrcode",
                                 params={"bot_type": "3"}, body={"local_token_list": []})
        image_path = args.data / "wechat-login.png"
        qrcode.make(qr["qrcode_img_content"]).save(image_path)
        print(json.dumps({"status": "SCAN_REQUIRED", "image": str(image_path)}, ensure_ascii=False), flush=True)
        for _ in range(150):
            await asyncio.sleep(2)
            login = await wechat_request(session, base, "/ilink/bot/get_qrcode_status", params={"qrcode": qr["qrcode"]})
            status = login.get("status")
            if status == "confirmed":
                credentials = {"token": login["bot_token"], "account": login["ilink_bot_id"],
                               "owner": login["ilink_user_id"], "base": checked_url(login.get("baseurl") or base)}
                credentials_path.write_text(_dpapi_protect(json.dumps(credentials)), encoding="utf-8")
                break
            if status == "scaned_but_redirect":
                host = login.get("redirect_host", "")
                base = checked_url(host if host.startswith("https://") else "https://" + host)
            elif status not in {"wait", "scaned"}:
                raise RuntimeError("WECHAT_LOGIN_REQUIRES_ATTENTION")
        else:
            raise RuntimeError("WECHAT_LOGIN_EXPIRED")
    credentials = json.loads(_dpapi_unprotect(credentials_path.read_text(encoding="utf-8")))
    print('微信连接测试就绪；请在授权后的会话中发送 /连接测试。普通消息不会发送给模型或写入记忆。', flush=True)
    cursor = ""
    for _ in range(args.polls):
        try:
            batch = await wechat_request(session, credentials["base"], "/ilink/bot/getupdates",
                token=credentials["token"], body={"get_updates_buf": cursor})
        except asyncio.TimeoutError:
            continue
        for raw in batch.get("msgs", []):
            event = owner_message("wechat", raw, account_id=credentials["account"], owner_id=credentials["owner"])
            if not event or event.text.strip() != "/连接测试" or not raw.get("context_token"):
                continue
            if not journal.reserve(event.exchange_id):
                continue
            await wechat_request(session, credentials["base"], "/ilink/bot/sendmessage", token=credentials["token"], body={
                "msg": {"from_user_id": "", "to_user_id": event.owner_id, "client_id": event.exchange_id,
                        "message_type": 2, "message_state": 2, "context_token": raw["context_token"],
                        "item_list": [{"type": 1, "text_item": {"text": "连接测试成功。这是固定测试消息，还没有调用林离的记忆和世界。"}}]}})
            # Official sendMessage accepts an absent ret; the HTTP wrapper
            # already rejects transport failures and explicit business errors.
            journal.delivered(event.exchange_id)
            print('WECHAT_OWNER_ROUNDTRIP_PASSED', flush=True)
            return
        cursor = batch.get("get_updates_buf") or cursor


async def qq(args, session, journal):
    import os
    if not args.owner or not args.account:
        raise ValueError("QQ_OWNER_REQUIRED")
    token = os.environ.get("OLIVIA_ONEBOT_TOKEN", "")
    if len(token) < 16:
        raise ValueError("ONEBOT_TOKEN_REQUIRED")
    async with session.ws_connect(checked_url(args.onebot_url, local=True),
                                  headers={"Authorization": "Bearer " + token}, heartbeat=20) as ws:
        await ws.send_json({"action": "get_login_info", "echo": "login"})
        account = None
        pending = set()
        print('QQ 已连接协议端，等待登录状态；仅响应指定用户的 /连接测试。', flush=True)
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            raw = json.loads(message.data)
            if raw.get("echo") == "login":
                if raw.get("status") != "ok" or raw.get("retcode") != 0:
                    raise RuntimeError("QQ_LOGIN_UNAVAILABLE")
                account = str(raw["data"]["user_id"])
                if account != args.account:
                    raise ValueError("QQ_ACCOUNT_MISMATCH")
                if account == args.owner:
                    raise ValueError("QQ_BOT_AND_OWNER_MUST_DIFFER")
                print('QQ_OWNER_TEST_READY', flush=True)
                continue
            if raw.get("echo") in pending:
                if raw.get("status") != "ok" or raw.get("retcode") != 0 or not raw.get("data", {}).get("message_id"):
                    raise RuntimeError("QQ_SEND_UNCONFIRMED")
                journal.delivered(raw["echo"])
                print('QQ_OWNER_ROUNDTRIP_PASSED', flush=True)
                return
            if not account:
                continue
            event = owner_message("qq", raw, account_id=account, owner_id=args.owner)
            if event and event.text.strip() == "/连接测试" and journal.reserve(event.exchange_id):
                pending.add(event.exchange_id)
                await ws.send_json({"action": "send_private_msg", "echo": event.exchange_id,
                    "params": {"user_id": int(event.owner_id), "message": [{"type": "text", "data": {
                        "text": "连接测试成功。这是固定测试消息，还没有调用林离的记忆和世界。"}}]}})


async def run(args):
    journal = ProbeJournal(args.data)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
            await asyncio.wait_for(wechat(args, session, journal) if args.channel == "wechat" else qq(args, session, journal), 900)
    finally:
        journal.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channel", choices=["wechat", "qq"])
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--login", action="store_true")
    parser.add_argument("--owner")
    parser.add_argument("--account")
    parser.add_argument("--onebot-url", default="ws://127.0.0.1:3001")
    parser.add_argument("--polls", type=int, default=20)
    args = parser.parse_args()
    if not args.data.is_absolute():
        parser.error("--data must be absolute")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Never print network exceptions, login response bodies or credentials.
        print(json.dumps({"status": "FAILED", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
