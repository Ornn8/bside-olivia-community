"""Opt-in lifecycle inside the existing local server, not a second data writer."""
import asyncio
from datetime import datetime
import json
import os
import re
from pathlib import Path

from aiohttp import web

from .service import PersonalChatService


_RUNTIME = web.AppKey("personal_chat", dict)


def _failure_code(exc):
    code = str(exc)
    return code if re.fullmatch(r"(?:PERSONAL_CHAT|WECHAT|QQ)_[A-Z0-9_]{1,80}", code) else "PERSONAL_CHAT_UNAVAILABLE"


def _publish_status(server, runtime):
    """Content-free local diagnostics; never expose accounts or credentials."""
    counts = {}
    for row in server.store.personal_chats:
        state = row.get("delivery_status")
        if state in {"GENERATING", "GENERATED", "SENDING", "DELIVERED", "FAILED"}:
            counts[state] = counts.get(state, 0) + 1
    value = {"channels": dict(runtime["status"]), "errors": dict(runtime.get("errors", {})),
             "exchanges": counts, "diagnostic_roundtrips": dict(runtime.get("roundtrips", {})),
             "consumer_pending": sum(bool(r.get("consumer_error_code")) for r in server.store.personal_chats)}
    writer = getattr(server, "_atomic_write_store_file", None)
    if callable(writer):
        writer(server._state_root() / "personal-chat-status.json", json.dumps(value, sort_keys=True))


async def generate(server, event, row):
    from persona_loader import load_persona
    from reply_context import ReplyMode
    from reply_orchestrator import ReplyRequest, ReplyState
    adapter = server.letters_adapter
    if not adapter.config.persona_v2_enabled or not server._llm_runtime_ready(adapter.config):
        raise RuntimeError("PERSONAL_CHAT_PERSONA_UNAVAILABLE")
    if load_persona(adapter.persona_v2_path).snapshot.status != "READY":
        raise RuntimeError("PERSONAL_CHAT_PERSONA_UNAVAILABLE")
    if server.daily_life_runtime is None or not server._official_history_private_world_available():
        raise RuntimeError("PERSONAL_CHAT_WORLD_UNAVAILABLE")
    # Keep the same memory readiness policy; do not bypass a slow extraction.
    deadline = asyncio.get_running_loop().time() + server.MEMORY_READY_REPLY_TIMEOUT_SECONDS
    while not server._conversation_memory_ready_for_reply():
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("PERSONAL_CHAT_MEMORY_UNAVAILABLE")
        await asyncio.sleep(.25)
    source = server._CURRENT_LETTER_MEMORY_SOURCE.set(f"reply:{event.exchange_id}:1")
    receipt = server._CURRENT_LETTER_RECEIPT.set(datetime.fromisoformat(row["life_received_at"]))
    try:
        request = ReplyRequest(content=event.text, request_id="personal-chat:" + event.exchange_id,
            idempotency_key=event.exchange_id, max_input_chars=adapter.config.max_input_chars,
            gateway_scope=(server.GatewayRequestScope.TEXT_LETTER_MAX_REASONING
                           if server.supports_scoped_reasoning(adapter.config) else None))
        result = await asyncio.wait_for(server.reply_pipeline.run(request,
            adapter.build_reply_context(ReplyMode.FUTURE_IM, future_im_enabled=True)),
            server._reply_pipeline_timeout_seconds(ReplyMode.TEXT_LETTER.value))
        if result.state is not ReplyState.COMPLETED:
            raise RuntimeError("PERSONAL_CHAT_GENERATION_FAILED")
        return result.text
    finally:
        server._CURRENT_LETTER_MEMORY_SOURCE.reset(source)
        server._CURRENT_LETTER_RECEIPT.reset(receipt)


async def commit(server, row):
    if row.get("delivery_status") != "DELIVERED":
        return
    if row.get("private_world_status") == "PENDING":
        if not server._commit_private_world_letter(row):
            server._persist_store_state()
            raise RuntimeError("PERSONAL_CHAT_WORLD_COMMIT_UNAVAILABLE")
        server._persist_store_state()
    if row.get("candidate_delivery_status") not in {"CREATED", "DUPLICATE", "SKIPPED", "DISABLED"}:
        if server.private_world_candidate_store is None:
            row["candidate_delivery_status"] = "DISABLED"
        else:
            result = await server._deliver_private_world_candidate(row, row["content"], row["reply_text"])
            status = getattr(result, "value", None)
            if status not in {"CREATED", "DUPLICATE", "SKIPPED"}:
                raise RuntimeError("PERSONAL_CHAT_CANDIDATE_UNAVAILABLE")
            row["candidate_delivery_status"] = status
        server._persist_store_state()
    if row.get("daily_life_status") != "COMMITTED":
        server._schedule_daily_life_exchange(row)
        task = server.daily_life_tasks.get(f"reply:{row['letter_id']}:1")
        if task is not None:
            await task
        if row.get("daily_life_status") != "COMMITTED":
            raise RuntimeError("PERSONAL_CHAT_DAILY_LIFE_UNAVAILABLE")
    # Mem0 is consumed by the existing canonical outbox over persisted state;
    # no second writer or extraction algorithm is introduced here.
    if not row.get("legacy_memory_delivered"):
        server.letters_adapter.remember_conversation(row["content"], row["reply_text"])
        row["legacy_memory_delivered"] = True
        server._persist_store_state()


async def recoverable_commit(server, row):
    """A delivered reply survives optional consumer failure without a resend."""
    if row.get("consumer_failures", 0) >= 3:
        return  # Durable exhausted state remains visible for operator recovery.
    try:
        await commit(server, row)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        row["consumer_error_code"] = _failure_code(exc)
        row["consumer_failures"] = row.get("consumer_failures", 0) + 1
        server._persist_store_state()
        server._safe_log("personal_chat_consumer_pending", error_code=row["consumer_error_code"])
    else:
        if row.pop("consumer_error_code", None) is not None:
            row.pop("consumer_failures", None)
            server._persist_store_state()


class _Cursor:
    def __init__(self, server, account):
        import hashlib
        self.server = server
        self.key = "personal_wechat_cursor_" + hashlib.sha256(account.encode()).hexdigest()

    def load(self):
        return self.server.store.personal_chat_cursors.get(self.key, "")

    def save(self, value):
        self.server.store.personal_chat_cursors[self.key] = value
        self.server._persist_store_state()


def _absolute_file(value):
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError("PERSONAL_CHAT_PATH_INVALID")
    return Path(value)


def install_personal_chat(app, server):
    async def start(application):
        configured = os.environ.get("OLIVIA_PERSONAL_CHAT_CONFIG")
        if not configured and server._state_root() is not None:
            saved = server._state_root() / "personal-chat" / "config.json"
            if saved.is_file():
                configured = str(saved)
        if not configured:
            return
        try:
            if server._state_root() is None:
                raise RuntimeError("PERSONAL_CHAT_DURABLE_STATE_REQUIRED")
            server._require_store_state_available()
            config = json.loads(_absolute_file(configured).read_text(encoding="utf-8"))
            if not isinstance(config, dict) or set(config) - {"wechat", "qq"} or not config:
                raise ValueError("PERSONAL_CHAT_CONFIG_INVALID")
            bindings, jobs = {}, []
            stop_event = asyncio.Event()
            if "wechat" in config:
                from original_client_setup_api import _dpapi_unprotect
                from .wechat import run_wechat
                path = _absolute_file(config["wechat"]["credentials_file"])
                credentials = json.loads(_dpapi_unprotect(path.read_text(encoding="utf-8")))
                bindings["wechat"] = (credentials["account"], credentials["owner"])
                jobs.append(("wechat", lambda handler: run_wechat(credentials, handler, stop_event,
                    cursor_store=_Cursor(server, credentials["account"]))))
            if "qq" in config:
                from .qq import run_qq
                qq = config["qq"]
                token = os.environ.get("OLIVIA_PERSONAL_QQ_TOKEN", "")
                if not token and qq.get("credentials_file"):
                    from original_client_setup_api import _dpapi_unprotect
                    secret = _absolute_file(qq["credentials_file"]).read_text(encoding="utf-8")
                    token = json.loads(_dpapi_unprotect(secret))["token"]
                if len(token) < 16:
                    raise ValueError("PERSONAL_CHAT_QQ_TOKEN_REQUIRED")
                bindings["qq"] = (str(qq["account"]), str(qq["owner"]))
                jobs.append(("qq", lambda handler: run_qq(qq["url"], token, *bindings["qq"], handler, stop_event)))
            service = PersonalChatService(server.store.personal_chats, server._persist_store_state,
                lambda event, row: generate(server, event, row), lambda row: recoverable_commit(server, row), bindings)
            from .probe import ProbeJournal
            journal = ProbeJournal(server._state_root() / "personal-chat-diagnostics")
            runtime = {"stop": stop_event, "tasks": [], "service": service, "status": {},
                       "errors": {}, "roundtrips": {}, "journal": journal}
            application[_RUNTIME] = runtime

            async def handle(event, send):
                # Connection diagnostics are never persona/memory inputs.
                if event.text.strip() == "/连接测试":
                    if journal.reserve(event.exchange_id):
                        await send("连接测试成功。正式聊天通道已加载；这条测试消息不会写入记忆。")
                        journal.delivered(event.exchange_id)
                        runtime["roundtrips"][event.channel] = runtime["roundtrips"].get(event.channel, 0) + 1
                        _publish_status(server, runtime)
                    return
                try:
                    await service.handle(event, send)
                finally:
                    _publish_status(server, runtime)

            async def run(name, factory):
                try:
                    runtime["status"][name] = "LISTENING"
                    _publish_status(server, runtime)
                    await factory(handle)
                    runtime["status"][name] = "STOPPED"
                except asyncio.CancelledError:
                    runtime["status"][name] = "STOPPED"
                    raise
                except Exception as exc:
                    runtime["status"][name] = "FAILED"
                    runtime["errors"][name] = _failure_code(exc)
                    server._safe_log("personal_chat_stopped", channel=name, error_code=runtime["errors"][name])
                finally:
                    _publish_status(server, runtime)

            async def boot():
                # Listener liveness does not depend on slow/failed extraction.
                for name, factory in jobs:
                    runtime["tasks"].append(asyncio.create_task(run(name, factory)))
                try:
                    while not stop_event.is_set():
                        await service.recover()
                        _publish_status(server, runtime)
                        try:
                            await asyncio.wait_for(stop_event.wait(), 30)
                        except asyncio.TimeoutError:
                            pass
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    runtime["status"]["recovery"] = "FAILED"
                    runtime["errors"]["recovery"] = _failure_code(exc)
                    _publish_status(server, runtime)
                    server._safe_log("personal_chat_recovery_failed", error_code="PERSONAL_CHAT_UNAVAILABLE")
            runtime["tasks"].append(asyncio.create_task(boot()))
        except Exception:
            server._safe_log("personal_chat_start_failed", error_code="PERSONAL_CHAT_CONFIG_UNAVAILABLE")

    async def stop(application):
        runtime = application.get(_RUNTIME)
        if runtime is None:
            return
        runtime["stop"].set()
        for task in runtime["tasks"]:
            task.cancel()
        await asyncio.gather(*runtime["tasks"], return_exceptions=True)
        runtime["journal"].db.close()

    app.on_startup.append(start)
    # Installed before memory/world cleanup so no new sends race shutdown.
    app.on_cleanup.append(stop)
