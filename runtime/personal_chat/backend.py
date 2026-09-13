"""Opt-in lifecycle inside the existing local server, not a second data writer."""
import asyncio
from datetime import datetime
import json
import os
import re
from pathlib import Path

from aiohttp import web

from .service import PersonalChatService
from runtime.private_world.life_rhythm import LOCAL


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
    from .presentation import CURRENT, parse, parse_social
    from .initiative import letter_invitation_allowed
    allowed = letter_invitation_allowed(server.store.personal_chats, getattr(server.store, 'letters', []),
                                         datetime.now().timestamp())
    previous = next((r.get('listening_preference', 'voice_ok') for r in reversed(server.store.personal_chats)
                     if r.get('delivery_status') == 'DELIVERED' and 'listening_preference' in r), 'voice_ok')
    voice_available = event.channel != 'wechat' and bool(row.get('voice_available')) and server._voice_reply_configured(os.environ)
    context = adapter.build_reply_context(ReplyMode.FUTURE_IM, future_im_enabled=True)
    from .stickers import choices, extract
    sticker_choices = choices(server.store.personal_chats, context.private_behavior) if event.channel == 'wechat' else {}
    presentation = CURRENT.set({'voice_available': voice_available, 'listening_preference': previous,
                                'structured': True, 'decision_now': datetime.now(LOCAL).isoformat(),
                                'due_followup': row.get('followup_quote'),
                                'channel': event.channel, 'incoming_format': event.input_kind,
                                'letter_invitation_allowed': allowed,
                                'sticker_choices': sticker_choices,
                                'proactive': row.get('origin') == 'proactive'})
    try:
        attempt_id = event.exchange_id + ':' + str(row.get('generation_attempts', 1))
        request = ReplyRequest(content=event.text or '应用主动聊天检查：现在是否有值得和对方分享的话？没有则跳过。', request_id="personal-chat:" + attempt_id,
            idempotency_key=attempt_id, max_input_chars=adapter.config.max_input_chars,
            gateway_scope=(server.GatewayRequestScope.PERSONAL_CHAT_JSON
                           if server.supports_scoped_reasoning(adapter.config) else None))
        result = await asyncio.wait_for(server.reply_pipeline.run(request,
            context),
            server._reply_pipeline_timeout_seconds(ReplyMode.TEXT_LETTER.value))
        if result.state is not ReplyState.COMPLETED:
            raise RuntimeError("PERSONAL_CHAT_GENERATION_FAILED")
        from .decision import decode
        decision = decode(result.text, user=event.text, now=datetime.now().timestamp(), proactive=row.get('origin') == 'proactive')
        if decision['skip']:
            return '[[skip]]'
        text, mode = decision['text'].strip(), decision['delivery']
        preference = previous if decision['listening'] == 'keep' else decision['listening']
        for kind, until in [('initiative', 'pause_until'), ('letter', 'letter_until')]:
            if decision[kind] != 'keep':
                row[kind + '_preference'] = decision[kind]
                row[until] = decision[until]
        row['letter_invitation'] = allowed and decision.get('letter_invitation', False)
        if decision['followup_at'] is not None:
            row.update(followup_at=decision['followup_at'], followup_quote=decision['evidence'])
        elif decision['followup_cancel'] or decision['initiative'] == 'pause' and decision['pause_until'] is None:
            row['followup_at'] = None
        sticker = decision['sticker'] if decision['sticker'] in sticker_choices else None
        if sticker:
            row['sticker_id'] = sticker
        if not voice_available:
            mode = 'text'
        row['presentation_status'] = 'VALIDATED'
        row.update(requested_format=mode, listening_preference=preference)
        if mode == 'voice' and voice_available and text != '[[skip]]':
            from runtime.media.voice_direction import TextOnlyVoicePlan
            path = server._state_root() / 'media' / (event.exchange_id + '.wav')
            try:
                async with server.media_semaphore:
                    metadata = await asyncio.to_thread(server.render_reply_audio, text, path,
                        tts_config_path=Path(os.environ['OLIVIA_TTS_CONFIG']),
                        voice_performance_plan=TextOnlyVoicePlan(text), environment=dict(os.environ))
                row.update(prepared_audio=str(path), reply_audio_duration=metadata['duration_seconds'])
            except Exception:
                row.pop('prepared_audio', None)
                row['voice_fallback'] = 'PERSONAL_CHAT_TTS_UNAVAILABLE'
        return text
    finally:
        server._CURRENT_LETTER_MEMORY_SOURCE.reset(source)
        server._CURRENT_LETTER_RECEIPT.reset(receipt)
        CURRENT.reset(presentation)


async def commit(server, row):
    if row.get("delivery_status") != "DELIVERED":
        return
    if row.get("private_world_status") == "PENDING":
        if not server._commit_private_world_letter(row):
            server._persist_store_state()
            raise RuntimeError("PERSONAL_CHAT_WORLD_COMMIT_UNAVAILABLE")
        server._persist_store_state()
    if row.get("delivered_format") == "audio" and row.get("media_world_status") != "COMMITTED":
        server._record_published_media(row, reply_text=row["reply_text"],
            delivery_id=row["private_world_delivery_id"], path=Path(row["prepared_audio"]),
            components=("speech",), presentation="audio")
        server._persist_store_state()
        if row.get("media_world_status") != "COMMITTED":
            raise RuntimeError("PERSONAL_CHAT_AUDIO_WORLD_UNAVAILABLE")
    if row.get("candidate_delivery_status") not in {"CREATED", "DUPLICATE", "SKIPPED", "DISABLED"}:
        if row.get('origin') == 'proactive':
            row['candidate_delivery_status'] = 'SKIPPED'
        elif server.private_world_candidate_store is None:
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
        if row.get('origin') != 'proactive':
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


def selected_channels(server):
    """Saved, evidenced contact choice takes precedence over development bindings."""
    from .contact_invitation import status
    from runtime.reply.proactive_letters import read_json
    port = getattr(server, 'private_world_port', None)
    access = status(server.store.letters, port.snapshot() if port is not None else None)
    if 'invitation_id' in access:
        return set(access['channels'])
    # Existing explicitly authorized test bindings survive upgrades only until
    # an actual invitation/choice exists. Credentials alone never unlock access.
    saved = read_json(server._state_root() / 'personal-chat' / 'existing-access.json')
    return set(saved.get('channels', [])) & {'qq', 'wechat'}


def install_personal_chat(app, server):
    async def start(application):
        configured = os.environ.get("OLIVIA_PERSONAL_CHAT_CONFIG")
        if not configured and server._state_root() is not None:
            saved = server._state_root() / "personal-chat" / "config.json"
            if saved.is_file():
                configured = str(saved)
        if not configured and server._state_root() is None:
            return
        try:
            if server._state_root() is None:
                raise RuntimeError("PERSONAL_CHAT_DURABLE_STATE_REQUIRED")
            server._require_store_state_available()
            if not configured:
                _publish_status(server, {'status': {name: 'SETUP_REQUIRED' for name in selected_channels(server)}})
                return
            config = json.loads(_absolute_file(configured).read_text(encoding="utf-8"))
            if not isinstance(config, dict) or set(config) - {"wechat", "qq"} or not config:
                raise ValueError("PERSONAL_CHAT_CONFIG_INVALID")
            selected = selected_channels(server)
            missing = selected - config.keys()
            config = {name: value for name, value in config.items() if name in selected}
            if not config:
                _publish_status(server, {'status': {name: 'SETUP_REQUIRED' for name in missing}})
                return
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
            def sticker_allowed(key):
                from reply_context import ReplyMode
                from runtime.letter_stickers.selection import allowed_stickers
                try:
                    context = server.letters_adapter.build_reply_context(ReplyMode.FUTURE_IM, future_im_enabled=True)
                    return key in allowed_stickers(context.private_behavior)
                except Exception:
                    return False
            service = PersonalChatService(server.store.personal_chats, server._persist_store_state,
                lambda event, row: generate(server, event, row), lambda row: recoverable_commit(server, row), bindings,
                sticker_allowed=sticker_allowed)
            from .probe import ProbeJournal
            journal = ProbeJournal(server._state_root() / "personal-chat-diagnostics")
            runtime = {"stop": stop_event, "tasks": [], "service": service, "status": {},
                       "errors": {}, "roundtrips": {}, "journal": journal}
            runtime['status'].update({name: 'SETUP_REQUIRED' for name in missing})
            application[_RUNTIME] = runtime
            from .initiative import Initiative
            initiative = Initiative(server.store.personal_chats)

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
                    if event.channel not in selected_channels(server):
                        runtime['errors'][event.channel] = 'PERSONAL_CHAT_CONTACT_NOT_ACCEPTED'
                        return  # Never replay pre-invitation messages later.
                    runtime['errors'].pop(event.channel, None)
                    initiative.received(event, send)
                    for attempt in range(2):
                        try:
                            await service.handle(event, send)
                            break
                        except Exception:
                            retryable = any(r.get('delivery_status') == 'FAILED' and r.get('generation_attempts', 0) < 2
                                and r.get('binding_id') == event.binding_id
                                and set(r.get('source_messages', {})) & dict(event.sources).keys()
                                for r in server.store.personal_chats)
                            if attempt or not retryable:
                                raise
                            await asyncio.sleep(.5)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # A durable failed/uncertain exchange must not kill reception.
                    runtime['errors'][event.channel] = _failure_code(exc)
                    server._safe_log('personal_chat_exchange_failed', channel=event.channel,
                                     error_code=runtime['errors'][event.channel])
                finally:
                    _publish_status(server, runtime)

            async def run(name, factory):
                while not stop_event.is_set():
                    try:
                        runtime['status'][name] = 'LISTENING'
                        _publish_status(server, runtime)
                        await factory(handle)
                        if stop_event.is_set():
                            break
                    except asyncio.CancelledError:
                        runtime['status'][name] = 'STOPPED'
                        _publish_status(server, runtime)
                        raise
                    except ValueError as exc:
                        runtime['status'][name] = 'SETUP_REQUIRED'
                        runtime['errors'][name] = _failure_code(exc)
                        _publish_status(server, runtime)
                        return
                    except Exception as exc:
                        runtime['errors'][name] = _failure_code(exc)
                    runtime['status'][name] = 'RECONNECTING'
                    _publish_status(server, runtime)
                    try:
                        await asyncio.wait_for(stop_event.wait(), 30)
                    except asyncio.TimeoutError:
                        pass
                runtime['status'][name] = 'STOPPED'
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
            async def proactive_loop():
                from .events import PersonalMessage
                import uuid
                while not stop_event.is_set():
                    await asyncio.sleep(15)
                    if not initiative.ready():
                        continue
                    old, sender = initiative.target
                    if old.channel not in selected_channels(server):
                        continue
                    if runtime['status'].get(old.channel) != 'LISTENING':
                        continue
                    if callable(getattr(sender, 'is_available', None)) and not sender.is_available():
                        continue
                    event = PersonalMessage(old.channel, old.account_id, old.owner_id,
                                            'proactive-' + uuid.uuid4().hex, '')
                    fresh = sender.for_exchange(event) if callable(getattr(sender, 'for_exchange', None)) else sender
                    try:
                        followup = initiative.pending_followup()
                        if followup and followup['followup_at'] > datetime.now().timestamp():
                            followup = None
                        await service.proactive(event, fresh,
                            lambda: initiative.ready() and initiative.target[0] is old,
                            followup=followup)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        runtime['errors']['initiative'] = _failure_code(exc)
                    finally:
                        initiative.attempted()
                        _publish_status(server, runtime)
            runtime['tasks'].append(asyncio.create_task(proactive_loop()))
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
