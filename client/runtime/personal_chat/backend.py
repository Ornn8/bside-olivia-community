"""Opt-in lifecycle inside the existing local server, not a second data writer."""
import asyncio
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime
import json
import math
import os
import re
import secrets
import threading
from time import monotonic
from pathlib import Path

from aiohttp import web

from .service import JEV_ERROR_CODES, PersonalChatService, persist_state
from runtime.private_world.life_rhythm import LOCAL
from runtime.reply.jev_billing import account_key_missing


_RUNTIME = web.AppKey("personal_chat", dict)
ACTIVATE_SAVED_CONFIG = web.AppKey("personal_chat_activate_saved_config", object)
_CONSUMER_TIMEOUT_SECONDS = 15
_VOICE_RENDER_TIMEOUT_SECONDS = 20 * 60
_QQ_DEFAULT_VOICE_TIMEOUT_SECONDS = 60


async def _record_semantic_shadow(server, row, task, revision=None):
    revision = revision or (row.get('input_revision', 0), row.get('generation_attempts', 0))
    try:
        result = await task
        if result is not None and revision == (row.get('input_revision', 0), row.get('generation_attempts', 0)):
            row['semantic_shadow'] = result
            await persist_chat(server)
    except Exception:
        # Diagnostic collection cannot fail or delay a user reply.
        pass


def _start_semantic_shadow_recorder(server, row, shadow):
    tasks = getattr(server, '_semantic_shadow_tasks', None)
    if tasks is None:
        tasks = server._semantic_shadow_tasks = set()
    revision = (row.get('input_revision', 0), row.get('generation_attempts', 0))
    recorder = asyncio.create_task(_record_semantic_shadow(server, row, shadow, revision))
    for task in (shadow, recorder):
        tasks.add(task)
        task.add_done_callback(tasks.discard)


async def _stop_semantic_shadow(server):
    tasks = tuple(getattr(server, '_semantic_shadow_tasks', ()))
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _recover_chat_loop(server, runtime, *, interval_seconds=30):
    from runtime.image_understanding import commit_image_memory
    stop = runtime['stop']
    while not stop.is_set():
        failure = None
        try:
            await runtime['service'].recover()
        except Exception as exc:
            failure = _failure_code(exc)
        for row in tuple(server.store.personal_chats):
            if row.get('image_world_status') == 'PENDING':
                try:
                    await commit_image_memory(server, row)
                except Exception as exc:
                    failure = _failure_code(exc)
        if failure:
            runtime['status']['recovery'] = 'FAILED'
            runtime['errors']['recovery'] = failure
            server._safe_log('personal_chat_recovery_failed', error_code=failure)
        else:
            runtime['status']['recovery'] = 'READY'
            runtime['errors'].pop('recovery', None)
        _publish_status(server, runtime)
        try:
            await asyncio.wait_for(stop.wait(), interval_seconds)
        except asyncio.TimeoutError:
            pass


async def deliver_speech(server,row,send):
    from .speech import deliver
    return await deliver(server,row,send)


def _qq_default_voice_timeout_seconds():
    try:
        value = float(os.environ.get('OLIVIA_QQ_DEFAULT_VOICE_TIMEOUT_SECONDS', ''))
    except ValueError:
        return _QQ_DEFAULT_VOICE_TIMEOUT_SECONDS
    return value if math.isfinite(value) and 5 <= value <= 120 else _QQ_DEFAULT_VOICE_TIMEOUT_SECONDS


def _discard_chat_audio(path):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass  # An unused generated artifact cannot block receipt or delivery.


async def prepare_chat_audio(server, text, path, *, timeout_seconds=None):
    """Submit chat speech independently of letter and photo media work."""
    from runtime.media.voice_direction import TextOnlyVoicePlan
    from runtime.media.media_paths import configured_media_path
    from runtime.remote_pipeline import enabled as remote_enabled
    config_path = configured_media_path(os.environ, 'OLIVIA_TTS_CONFIG')
    if config_path is None and not remote_enabled(os.environ):
        raise RuntimeError('PERSONAL_CHAT_TTS_UNAVAILABLE')
    timeout = _VOICE_RENDER_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    if (type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0 < timeout <= _VOICE_RENDER_TIMEOUT_SECONDS):
        raise ValueError('PERSONAL_CHAT_TTS_TIMEOUT_INVALID')
    abandoned = threading.Event()
    environment = {**os.environ, 'OLIVIA_MEDIA_CHANNEL': 'qq'}
    def render():
        try:
            return server.render_reply_audio(text, path, tts_config_path=config_path,
                voice_performance_plan=TextOnlyVoicePlan(text),
                environment=environment)
        finally:
            # The blocking cloud/local worker may outlive the asyncio waiter or
            # even the loop. Its late output is never an outgoing reply.
            if abandoned.is_set():
                _discard_chat_audio(path)
    worker = asyncio.create_task(asyncio.to_thread(render))
    def finished(task):
        if not task.cancelled():
            task.exception()
        if abandoned.is_set():
            _discard_chat_audio(path)
    worker.add_done_callback(finished)
    try:
        return await asyncio.wait_for(asyncio.shield(worker), timeout)
    except (TimeoutError, asyncio.CancelledError):
        # This ends only the wait; it does not promise cancellation/refund of a
        # submitted provider job. Do not synthesize or send a second voice.
        abandoned.set()
        if worker.done():
            _discard_chat_audio(path)
        raise


def _failure_code(exc):
    code = str(exc)
    return code if code in JEV_ERROR_CODES or re.fullmatch(r"(?:PERSONAL_CHAT|WECHAT|QQ|LLM|IMAGE)_[A-Z0-9_]{1,80}", code) else "PERSONAL_CHAT_UNAVAILABLE"


def _generation_failure_code(code):
    if isinstance(code, str) and code in JEV_ERROR_CODES:
        return code
    from runtime.diagnostics.failure_context import CODES, REWRITE_ERROR_CODES
    known = CODES | REWRITE_ERROR_CODES | {'INPUT_TOO_LONG', 'RECALL_CONTEXT_BUDGET_EXCEEDED',
                     'PERSONA_NOT_READY', 'IDEMPOTENCY_CONFLICT',
                     'LLM_TIMEOUT', 'LLM_INTERNAL',
                     'CURRENT_TURN_INTERPRETATION_FAILED', 'REVIEW_FAILED',
                     'REPLY_QUALITY_BLOCKED', 'REWRITE_FAILED', 'REVIEWER_UNAVAILABLE',
                     'REVIEWER_RESPONSE_INVALID', 'REVIEWER_DISABLED',
                     'FRESH_INTIMACY_CLAIMS_REQUIRED', 'INTIMACY_CLAIM_SOURCE_CONFLICT',
                     'INTIMACY_REQUEST_INCONSISTENT', 'PERSONAL_CHAT_DECISION_INVALID'}
    if isinstance(code, str) and code.startswith('PERSONAL_CHAT_') and code.removeprefix('PERSONAL_CHAT_') in known:
        return code
    if isinstance(code, str) and code in known:
        return code if code.startswith(('LLM_', 'PERSONAL_CHAT_')) else 'PERSONAL_CHAT_' + code
    return 'PERSONAL_CHAT_GENERATION_FAILED'


_KEY_ERRORS = {'JEV_BILLING_ACCOUNT_UNAVAILABLE', 'OLIVIA_KEY_REQUIRED'}


def reply_failures(server, runtime):
    """Channel error codes, and when a stored turn failed (for errors that came from one)."""
    errors = {channel: _failure_code(RuntimeError(code))
              for channel, code in runtime.get('errors', {}).items() if channel in ('qq', 'wechat', 'recovery')}
    failed_at = {}
    latest = {row.get('channel'): row for row in getattr(server.store, 'personal_chats', [])}
    for channel, row in latest.items():
        if channel in runtime.get('status', {}) and (row.get('delivery_status') in {'FAILED', 'DELIVERY_UNCONFIRMED'}
                or row.get('delivery_status') == 'SENDING' and row.get('error_code')):
            code = row.get('error_code', '')
            if code in _KEY_ERRORS and not account_key_missing():
                continue  # The key was connected after this turn failed.
            if channel not in errors:
                errors[channel] = (code if isinstance(code, str) and
                                   (code in JEV_ERROR_CODES or re.fullmatch(r'(?:PERSONAL_CHAT|IMAGE|LLM)_[A-Z0-9_]{1,80}', code))
                                   else 'PERSONAL_CHAT_GENERATION_FAILED')
                stamp = row.get('life_received_at') or row.get('user_sent_at')
                if isinstance(stamp, str):
                    failed_at[channel] = stamp
    return errors, failed_at


def reply_errors(server, runtime):
    return reply_failures(server, runtime)[0]


def _publish_status(server, runtime):
    """Content-free local diagnostics; never expose accounts or credentials."""
    counts = {}
    for row in server.store.personal_chats:
        state = row.get("delivery_status")
        if state in {"RECEIVED", "GENERATING", "GENERATED", "MEDIA_PENDING", "SENDING", "DELIVERY_UNCONFIRMED", "DELIVERED", "FAILED"}:
            counts[state] = counts.get(state, 0) + 1
    errors = reply_errors(server, runtime)
    value = {"channels": dict(runtime["status"]), "errors": errors,
             "last_seen_at": dict(runtime.get("last_seen_at", {})),
             "delivery_health": dict(runtime.get("delivery_health", {})),
             "e2e_verified_at": dict(runtime.get("e2e_verified_at", {})),
             "connection_test_pending": sorted(runtime.get("connection_tests", {}).keys()),
             "exchanges": counts, "diagnostic_roundtrips": dict(runtime.get("roundtrips", {})),
             "consumer_pending": sum(bool(r.get("consumer_error_code")) for r in server.store.personal_chats)}
    writer = getattr(server, "_atomic_write_store_file", None)
    if callable(writer):
        try:
            writer(server._state_root() / "personal-chat-status.json", json.dumps(value, sort_keys=True))
        except OSError:
            # This diagnostic projection is not the durable conversation store.
            # A failed status write must not stop listeners or recovery work.
            server._safe_log('personal_chat_status_write_failed',
                             error_code='PERSONAL_CHAT_STATUS_WRITE_FAILED')


async def persist_chat(server):
    await persist_state(getattr(server, '_persist_store_state_async', server._persist_store_state))


def _contact_basis(state):
    snapshot, profile = state['snapshot'], state['profile']
    current = snapshot.get('current') or {}
    return deepcopy({'tier': profile.tier, 'caution': profile.caution,
        'current': {key: current.get(key) for key in ('source_id', 'occurred_at', 'activity_kind')},
        'stale': snapshot.get('stale'),
        'current_class': snapshot.get('world', {}).get('schedule', {}).get('current_class')})


def _contact_opportunities(state, now, row):
    from runtime.reply.proactive_runtime import opportunities
    offered = opportunities(state, now)
    source = next((r for r in state['rows'] if r.get('letter_id') == row.get('followup_source_id')
                   and r.get('origin') != 'proactive' and r.get('delivery_status') == 'DELIVERED'), None)
    if (source and source.get('followup_at') is not None
            and source['followup_at'] <= now.timestamp() < source['followup_at'] + 7200
            and source.get('followup_quote') == row.get('followup_quote')):
        offered.insert(0, {'id': 'followup:' + source['letter_id'], 'kind': 'followup',
            'description': json.dumps({'source_id': source['letter_id'], 'quote': source['followup_quote'],
                                      'due_at': source['followup_at']}, ensure_ascii=False, separators=(',', ':'))})
    return offered[:16]


def _appointment_due(server, now):
    from .initiative import Initiative
    followup = Initiative(server.store.personal_chats, clock=lambda: now.timestamp()).pending_followup()
    return followup is not None and followup['followup_at'] <= now.timestamp()


def _contact_eligible(server, event):
    from runtime.reply.proactive_runtime import live_state
    now = datetime.now(LOCAL)
    state = live_state(server, channel=event.channel, now=now, exclude_id=event.exchange_id,
                       appointment_due=_appointment_due(server, now))
    if state['gates']['paused'] or state['gates']['blocked_reasons']:
        return False
    row = next((r for r in server.store.personal_chats if r.get('letter_id') == event.exchange_id), {})
    if row.get('proactive_basis') is not None and row['proactive_basis'] != _contact_basis(state):
        return False
    decision = row.get('proactive_decision', {}).get('decision', {})
    if decision.get('action') == 'defer':
        return False
    if decision.get('opportunity_id') is not None:
        return row.get('proactive_opportunity') in _contact_opportunities(state, now, row)
    return True


async def _proactive_contact(server, runtime, initiative):
    from .events import PersonalMessage
    from runtime.reply import proactive_runtime
    import uuid
    enabled = proactive_runtime.enabled()
    with (proactive_runtime.contact_slot(server, 'im') if enabled else nullcontext(True)) as acquired:
        if not acquired or not initiative.ready():
            return
        old, sender = initiative.target
        def channel_ready():
            return (old.channel in selected_channels(server)
                and runtime['status'].get(old.channel) == 'CONNECTED'
                and (not callable(getattr(sender, 'is_available', None)) or sender.is_available()))
        if not channel_ready():
            return
        event = PersonalMessage(old.channel, old.account_id, old.owner_id, 'proactive-' + uuid.uuid4().hex, '')
        fresh = sender.for_exchange(event) if callable(getattr(sender, 'for_exchange', None)) else sender
        def eligible():
            if initiative.target[0] is not old or not channel_ready():
                return False
            if enabled:
                return _contact_eligible(server, event)
            return (any(r.get('letter_id') == event.exchange_id for r in server.store.personal_chats)
                    or initiative.ready())
        try:
            followup = initiative.pending_followup()
            if followup and followup['followup_at'] > datetime.now().timestamp():
                followup = None
            await runtime['service'].proactive(event, fresh, eligible, followup=followup)
        finally:
            initiative.attempted()


async def generate(server, event, row):
    from runtime.reply.jev_billing import billing_scope
    with billing_scope('personal-chat:' + event.exchange_id + ':' + str(row.get('input_revision', 0))):
        return await _generate_billed(server, event, row)


async def _generate_billed(server, event, row):
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
    # Freeze before generation so the writer and attachment use one preference.
    if 'image_reply_settings' not in row:
        row['image_reply_settings'] = server.video_reply_settings_store.image_snapshot()
        await persist_chat(server)
    from runtime.image_understanding import understand_incoming, incoming_context
    await understand_incoming(server, event, row)
    observation_context = incoming_context(row)
    from runtime import incoming_media
    await incoming_media.understand_incoming(server, event, row)
    if event.media:
        observation_context += incoming_media.incoming_context(row)
    content = event.text + observation_context
    source = server._CURRENT_LETTER_MEMORY_SOURCE.set(f"reply:{event.exchange_id}:1")
    receipt = server._CURRENT_LETTER_RECEIPT.set(datetime.fromisoformat(row["life_received_at"]))
    from .presentation import CURRENT, parse, parse_social, recent_delivery_formats
    from .initiative import letter_invitation_allowed
    allowed = letter_invitation_allowed(server.store.personal_chats, getattr(server.store, 'letters', []),
                                         datetime.now().timestamp())
    voice_block = ('WECHAT_TEXT' if event.channel == 'wechat' else
                   'TRANSPORT_UNAVAILABLE' if not row.get('voice_available') else
                   'PROVIDER_UNAVAILABLE' if not server._voice_reply_configured(os.environ) else None)
    voice_available = voice_block is None
    from .speech import supported as speech_supported
    speech_enabled = event.channel == 'qq' and await speech_supported(os.environ)
    row['voice_ready'] = voice_available
    image_offered = bool(event.channel == 'qq' and row.get('image_available') and row['image_reply_settings'].get('enabled')
                         and os.environ.get('OLIVIA_GPU_API_URL') and os.environ.get('OLIVIA_GPU_API_KEY'))
    daily_worker = getattr(server, '_daily_video_worker', None)
    if 'daily_video_candidates' not in row:
        # Her current moment is offered exactly when a photo could be: it is the
        # same proactive share, and the photo switch is the user's media consent.
        row['daily_video_candidates'] = ([candidate for candidate in daily_worker.candidates()
                                          if candidate['event_kind'] != 'moment' or image_offered]
            if event.channel == 'qq' and daily_worker is not None else [])
        await persist_chat(server)
    daily_candidates = row['daily_video_candidates'] if event.channel == 'qq' else []
    semantic_kinds = ['text'] + (['audio_speech'] if voice_available else [])
    if image_offered:
        semantic_kinds.append('image')
    if daily_candidates:
        semantic_kinds.append('video_speech')
    context = adapter.build_reply_context(ReplyMode.FUTURE_IM, future_im_enabled=True)
    # A generation retry belongs to the same received turn. Keep its trusted
    # time stable; all other context/evidence is still rebuilt and hash-checked.
    from dataclasses import replace
    from runtime.reply.reply_context import TrustedTime
    if row.get('generation_context_at'):
        try:
            context = replace(context, trusted_time=TrustedTime(
                datetime.fromisoformat(row['generation_context_at']), source=context.trusted_time.source))
        except (ValueError, TypeError):
            row.pop('generation_context_at', None)
    row.setdefault('generation_context_at', context.trusted_time.instant.isoformat())
    from runtime.image_reply import photo_reply_context
    context = photo_reply_context(context, row['image_reply_settings'], channel=event.channel)
    from .stickers import choices
    from runtime.letter_stickers.packs import installed as installed_packs
    sticker_choices = choices(server.store.personal_chats, context.private_behavior, channel=event.channel,
                              installed=installed_packs(getattr(server, '_local_data_root', lambda: None)())
                              if event.channel == 'qq' else ())
    delayed_delivery = False
    try:
        sent_at = datetime.fromisoformat(row['user_sent_at']) if row.get('user_sent_at') else None
        received_at = datetime.fromisoformat(row['life_received_at'])
        delayed_delivery = sent_at is not None and (received_at - sent_at).total_seconds() > 60
    except (TypeError, ValueError):
        pass
    revision = row.get('input_revision', 0)
    from .service import TURN_IS_CURRENT
    def turn_is_current():
        current = TURN_IS_CURRENT.get()
        return revision == row.get('input_revision', 0) and (not callable(current) or current())
    def record_stage_timing(timings):
        if turn_is_current():
            from runtime.diagnostics.support_bundle import project_chat_task
            safe = project_chat_task({'stage_timing_seconds': timings})
            row.update(safe)
    async def save_companion_decision(record):
        if not turn_is_current():
            raise RuntimeError('JEV_INPUT_SUPERSEDED')
        row['companion_decision'] = deepcopy(record)
        await persist_chat(server)
    async def proactive_decide(messages, world, emotion):
        from runtime.reply import proactive_runtime
        from runtime.reply.companion_runtime import CompanionRuntimeError
        now = context.trusted_time.instant
        state = proactive_runtime.live_state(server, channel=event.channel, now=now, exclude_id=event.exchange_id,
                                             appointment_due=bool(row.get('followup_source_id')))
        if not isinstance(world, dict) or world.get('kind') != 'character_life_reference':
            raise CompanionRuntimeError('JEV_CONTEXT_UNAVAILABLE')
        observed = world.get('current') or world.get('last_observation') or {}
        current = state['snapshot'].get('current') or {}
        if (any(observed.get(key) != current.get(key) for key in ('source_id', 'occurred_at'))
                or 'schedule' in world and world['schedule'].get('current_class') !=
                state['snapshot'].get('world', {}).get('schedule', {}).get('current_class')):
            raise CompanionRuntimeError('JEV_CONTEXT_UNAVAILABLE')
        offered = _contact_opportunities(state, now, row)
        value = proactive_runtime.packet(channel=event.channel, now=now, profile=state['profile'],
            world=world, rhythm=state['snapshot'].get('rhythm', {}), emotion=emotion,
            messages=messages, opportunities=offered, rows=state['rows'],
            available_media=['text'] + (['audio_speech'] if voice_available else []), hard_gates=state['gates'])
        try:
            result = await proactive_runtime.decide(value)
        except ValueError:
            raise CompanionRuntimeError('JEV_CONFIG_INVALID') from None
        if not turn_is_current():
            raise CompanionRuntimeError('JEV_INPUT_SUPERSEDED')
        row['proactive_decision'] = proactive_runtime.record_decision(result)
        row['proactive_basis'] = _contact_basis(state)
        row['proactive_opportunity'] = next((item for item in offered
            if item['id'] == result.decision['opportunity_id']), None)
        try:
            await persist_chat(server)
        except Exception:
            raise CompanionRuntimeError('JEV_DECISION_NOT_SAVED') from None
        return result
    from runtime.reply.proactive_runtime import enabled as proactive_enabled
    proactive_metadata = ({'proactive_decide': proactive_decide}
                          if row.get('origin') == 'proactive' and proactive_enabled() else {})
    presentation = CURRENT.set({'voice_available': voice_available, 'listening_preference': 'voice_ok',
                                'recent_delivery_formats': recent_delivery_formats(server.store.personal_chats,
                                    channel=event.channel, binding_id=event.binding_id),
                                'structured': True, 'raw_user_text': event.text,
                                'speech_enabled': speech_enabled,
                                'bedtime_offer_enabled': speech_enabled,
                                'daily_video_candidates': daily_candidates,
                                'incoming_observation_context': observation_context,
                                'semantic_kinds': semantic_kinds,
                                'received_source_id': f'reply:{event.exchange_id}:user',
                                'input_revision': revision,
                                'turn_is_current': turn_is_current,
                                'recovery_namespace': json.dumps([event.channel, event.account_id,
                                    event.owner_id, event.binding_id], ensure_ascii=False),
                                'record_stage_timing': record_stage_timing,
                                'generation_attempts': row.get('generation_attempts', 1),
                                 'last_decision_rejection_reason': row.get('decision_rejection_reason'),
                                'companion_decision': row.get('companion_decision'),
                                'story_continuation': next(({
                                    'source_id': 'speech:' + str(r.get('letter_id')),
                                    'kind': 'fiction_summary' if (r.get('speech_intent') or {}).get('mode') in ('story', 'asmr_story') else 'speech_summary',
                                    'text': r['speech_script']['continuation_summary']}
                                    for r in reversed(server.store.personal_chats) if r.get('binding_id')==event.binding_id
                                    and r.get('speech_delivery_status')=='DELIVERED' and isinstance(r.get('speech_script'),dict)
                                    and r['speech_script'].get('continuation_summary')),None),
                                'save_companion_decision': save_companion_decision,
                                'decision_now': datetime.now(LOCAL).isoformat(),
                                'due_followup': row.get('followup_quote'),
                                'channel': event.channel, 'incoming_format': event.input_kind,
                                'user_sent_at': row.get('user_sent_at'), 'received_at': row['life_received_at'],
                                'delayed_delivery': delayed_delivery,
                                'letter_invitation_allowed': allowed,
                                'sticker_choices': sticker_choices,
                                'proactive': row.get('origin') == 'proactive', **proactive_metadata})
    from .context import READ_WINDOW, freeze_read_window
    from itertools import chain
    read_window = None
    try:
        read_window = READ_WINDOW.set(freeze_read_window(
            chain(getattr(server.store, 'letters', []), server.store.personal_chats),
            channel=event.channel, binding_id=event.binding_id, current_id=event.exchange_id))
        attempt_id = event.exchange_id + (f':input-{revision}' if revision else '') + ':' + str(row.get('generation_attempts', 1))
        request = ReplyRequest(content=content or '应用主动聊天检查：现在是否有值得和对方分享的话？没有则跳过。', request_id="personal-chat:" + attempt_id,
            idempotency_key=attempt_id,
            max_input_chars=min(adapter.config.max_input_chars, 40000 + len(content or '')),
            gateway_scope=(server.GatewayRequestScope.PERSONAL_CHAT_JSON
                           if server.supports_scoped_reasoning(adapter.config) else None))
        try:
            result = await asyncio.wait_for(server.reply_pipeline.run(request,
                context),
                server._reply_pipeline_timeout_seconds(ReplyMode.FUTURE_IM.value))
        except TimeoutError:
            raise RuntimeError('PERSONAL_CHAT_GENERATION_TIMEOUT') from None
        companion = getattr(result, 'companion_decision', None)
        contact = getattr(result, 'proactive_decision', None)
        if not turn_is_current():
            shadow = getattr(result, 'semantic_shadow_task', None)
            if shadow is not None:
                shadow.cancel()
                await asyncio.gather(shadow, return_exceptions=True)
            return None  # The service refreshes intake before using this result.
        if companion is not None:
            await save_companion_decision(companion)
            row.update(companion_timing=getattr(result, 'companion_timing', None),
                       companion_delivery=getattr(result, 'companion_delivery', None))
            row.pop('sticker_id', None)
            row.pop('mailbox_notice_letter_id', None)
        elif result.state is ReplyState.COMPLETED:
            for key in ('companion_decision', 'companion_timing', 'companion_delivery'):
                row.pop(key, None)
        shadow = getattr(result, 'semantic_shadow_task', None)
        if shadow is not None:
            _start_semantic_shadow_recorder(server, row, shadow)
        from runtime.diagnostics.support_bundle import project_chat_task
        quality_fields = ('quality_status', 'reviewer_calls', 'rewrite_calls', 'decision_rejection_reason',
                          'decision_dropped_media',
                          'quality_error_code', 'quality_failure_stage', 'quality_violation_codes',
                          'stage_timing_seconds', 'stage_cache_hits', 'stage_actual_calls', 'degraded_stages')
        quality = project_chat_task({'channel': event.channel, **{
            field: getattr(result, field, None) for field in quality_fields},
            'quality_error_code': getattr(result, 'error_code', None),
            'quality_violation_codes': getattr(result, 'violation_codes', None)})
        for field in quality_fields:
            row.pop(field, None)
            if field in quality:
                row[field] = quality[field]
        if proactive_metadata and result.error_code == 'JEV_DECISION_NOT_SAVED':
            raise RuntimeError('JEV_DECISION_NOT_SAVED')
        await persist_chat(server)
        if result.state is not ReplyState.COMPLETED:
            from runtime.diagnostics.failure_context import CODES, provider_failure_context
            exc = RuntimeError(_generation_failure_code(result.error_code))
            # Keep provider submission semantics across the result/exception
            # boundary. Review and format repair retain their bounded retry.
            if result.error_code in CODES | {'INPUT_TOO_LONG', 'IDEMPOTENCY_CONFLICT'}:
                exc.retryable = result.retryable
            exc.failure_context = provider_failure_context(getattr(result, 'failure_context', {}))
            raise exc
        if contact is not None and contact['decision']['action'] == 'defer':
            return '[[skip]]'
        from .decision import decode
        try:
            decision = decode(result.text, user=event.text, now=datetime.now().timestamp(),
                              proactive=row.get('origin') == 'proactive',
                              allow_user_silence=getattr(result, 'silence_authorized', False) is True,
                              allow_speech=bool(event.channel == 'qq'
                                   and (row.get('companion_decision') or {}).get('speech_request')),
                               daily_video_candidates=daily_candidates)
        except ValueError as exc:
            # Diagnostic categories only; never persist rejected model text.
            reason = getattr(exc, 'reason', 'UNKNOWN')
            row['decision_rejection_reason'] = reason
            server._safe_log('personal_chat_decision_rejected', reason=reason,
                missing_fields=getattr(exc, 'missing_fields', []),
                extra_field_count=getattr(exc, 'extra_field_count', 0))
            raise
        if decision.get('dropped_controls'):
            row['decision_dropped_controls'] = decision['dropped_controls']
            server._safe_log('personal_chat_decision_normalized', channel=event.channel,
                             decision_dropped_controls=decision['dropped_controls'])
        if decision.get('defaulted_fields'):
            row['decision_defaulted_fields'] = decision['defaulted_fields']
            server._safe_log('personal_chat_decision_normalized', channel=event.channel,
                             decision_defaulted_fields=decision['defaulted_fields'])
        dropped_media = decision.get('dropped_media') or row.get('decision_dropped_media')
        if dropped_media == 'UNREQUESTED_SPEECH':
            row['decision_dropped_media'] = dropped_media
            server._safe_log('personal_chat_decision_normalized', channel=event.channel,
                             decision_dropped_media=dropped_media)
        if contact is not None and decision['skip']:
            raise RuntimeError('JEV_PLAN_UNSUPPORTED')
        if not turn_is_current():
            return None  # Intake changed: apply neither stale controls nor silence.
        # User controls are independently validated against this original input.
        # They take effect even when the user asks for no outgoing reply; do not
        # pretend that a quiet or failed outbound turn was delivered.
        changed_controls = False
        for kind, until in [('initiative', 'pause_until'), ('letter', 'letter_until')]:
            if decision[kind] != 'keep':
                row[kind + '_preference'] = decision[kind]
                row[until] = decision[until]
                changed_controls = True
        if decision['followup_at'] is not None:
            row.update(followup_at=decision['followup_at'], followup_quote=decision['evidence'])
            changed_controls = True
        elif decision['followup_cancel'] or decision['initiative'] == 'pause' and decision['pause_until'] is None:
            row['followup_at'] = None
            changed_controls = True
        if changed_controls:
            row['user_controls_applied'] = True
            await persist_chat(server)
        if decision['skip']:
            if row.get('origin') != 'proactive':
                kind = decision['silence_kind']
                row.update(companion_timing=kind, companion_delivery=None,
                           silence_reason='USER_REQUESTED_WAIT' if kind == 'wait_user' else 'USER_REQUESTED_NO_REPLY')
                await persist_chat(server)
                return None
            return '[[skip]]'
        text, mode = decision['text'].strip(), decision['delivery']
        from .decision import repeats_recent
        if repeats_recent(text, [r for r in server.store.personal_chats if r is not row],
                          channel=event.channel, binding_id=event.binding_id):
            if row.get('origin') != 'proactive':
                # A new user message can warrant the same greeting or answer.
                # Repetition is a style warning after content validation, not
                # a reason to discard a reply or buy another generation.
                row['decision_warning_codes'] = ['REPEATED_REPLY']
                server._safe_log('personal_chat_decision_warning', channel=event.channel,
                                 decision_warning_codes=['REPEATED_REPLY'])
            else:
                # Repeated unsolicited proactive contact retains its existing guard.
                row['decision_rejection_reason'] = 'REPEATED_REPLY'
                server._safe_log('personal_chat_decision_rejected', reason='REPEATED_REPLY', missing_fields=[], extra_field_count=0)
                error = ValueError('PERSONAL_CHAT_DECISION_INVALID')
                error.reason = 'REPEATED_REPLY'
                raise error
        basis = 'WRITER_SELECTION'
        if companion is not None:
            delivery = row['companion_delivery']
            if delivery == 'video_speech' and not decision.get('daily_video_request'):
                # The plan chose her current-moment video but the writer left the
                # video out: the written reply still goes out, without a video.
                delivery = row['companion_delivery'] = 'text'
                row['daily_video_dropped'] = 'WRITER_OMITTED'
            if delivery not in semantic_kinds:
                raise RuntimeError('JEV_PLAN_UNSUPPORTED')
            mode = 'voice' if delivery == 'audio_speech' else 'text'
            basis = 'JEV_MEDIA_PLAN'
            from runtime.reply.companion_runtime import media_locked
            if (delivery == 'text' and event.channel == 'qq' and voice_available
                    and not media_locked((row.get('companion_decision') or {}).get('plan'))):
                reason = decision['text_reason'] if decision['delivery'] == 'text' else None
                mode = 'text' if reason else 'voice'
                basis = reason.upper() if reason else 'QQ_DEFAULT_VOICE'
        if contact is not None:
            delivery = contact['decision']['medium']
            if delivery not in {'text', 'audio_speech'} or delivery == 'audio_speech' and not voice_available:
                raise RuntimeError('JEV_PLAN_UNSUPPORTED')
            mode = 'voice' if delivery == 'audio_speech' else 'text'
            basis = 'PROACTIVE_MEDIA_PLAN'
            row.pop('sticker_id', None)
            row.pop('mailbox_notice_letter_id', None)
        from .mailbox_notice import attach_notice
        if companion is None and contact is None and not row.get('degraded_stages'):
            text = attach_notice(getattr(server.store, 'letters', []), row, text)
        row['letter_invitation'] = contact is None and allowed and decision.get('letter_invitation', False)
        sticker = decision['sticker'] if decision['sticker'] in sticker_choices else None
        if sticker and companion is None and contact is None:
            row['sticker_id'] = sticker
        if not voice_available:
            mode = 'text'
            basis = voice_block
        if row.get('degraded_stages'):
            mode, basis = 'text', 'AUXILIARY_TEXT_RECOVERY'
            row.pop('sticker_id', None)
            row.pop('mailbox_notice_letter_id', None)
        row['presentation_status'] = 'VALIDATED'
        row.update(requested_format=mode, listening_preference='voice_ok', delivery_basis=basis)
        if not turn_is_current():
            return text  # The service merges new input before any draft is sent.
        if decision.get('daily_video_request'):
            if companion is None or row.get('companion_delivery') == 'video_speech':
                row.update(daily_video_request=decision['daily_video_request'], daily_video_status='PENDING_ACK')
        from runtime.reply.character_emotion_context import store_expression_context
        store_expression_context(row, getattr(result, 'expression_context', None), text)
        speech_intent = (row.get('companion_decision') or {}).get('speech_request')
        script = decision.get('speech')
        if getattr(result, 'reviewed_content', None) is not None:
            row['content_review'] = dict(version=1, hashes=result.reviewed_content)
        if script:
            if ((row.get('companion_decision') or {}).get('plan', {}).get('proposal', {}).get('timing')
                    in {'wait_user', 'defer', 'no_reply'}):
                raise RuntimeError('JEV_PLAN_UNSUPPORTED')
            if not speech_intent or event.channel != 'qq':
                raise ValueError('SPEECH_INTENT_INVALID')
            row.update(speech_script=script, speech_intent=speech_intent, requested_format='text',
                       speech_status='PENDING', speech_delivery_status='PENDING')
            mode = 'text'
        if mode == 'voice' and voice_available and text != '[[skip]]':
            # A cancelled older revision can still finish in its worker thread.
            # Give each render its own output so it cannot overwrite new audio.
            path = server._state_root() / 'media' / (event.exchange_id + '-' + secrets.token_hex(8) + '.wav')
            timeout = (_qq_default_voice_timeout_seconds() if basis == 'QQ_DEFAULT_VOICE'
                       else _VOICE_RENDER_TIMEOUT_SECONDS)
            started = monotonic()
            voice_status = 'failed'
            row.update(voice_prepare_status='running', voice_prepare_timeout_seconds=timeout)
            try:
                metadata = await prepare_chat_audio(server, text, path, timeout_seconds=timeout)
                if not turn_is_current():
                    _discard_chat_audio(path)
                    return text
                row.update(prepared_audio=str(path), reply_audio_duration=metadata['duration_seconds'])
                row.pop('voice_fallback', None)
                voice_status = 'completed'
            except asyncio.CancelledError:
                voice_status = 'cancelled'
                raise
            except Exception as exc:
                if not turn_is_current():
                    _discard_chat_audio(path)
                    return text
                voice_status = 'timeout' if isinstance(exc, TimeoutError) else 'failed'
                _discard_chat_audio(path)
                row.pop('prepared_audio', None)
                row['voice_fallback'] = ('PERSONAL_CHAT_TTS_TIMEOUT' if voice_status == 'timeout'
                                         else 'PERSONAL_CHAT_TTS_UNAVAILABLE')
                if basis == 'QQ_DEFAULT_VOICE' or companion is not None:
                    # The reply is written and paid for. When speech cannot be made,
                    # deliver this already-reviewed body as text instead of nothing.
                    row['delivery_basis'] = ('VOICE_RENDER_TIMEOUT' if voice_status == 'timeout'
                                             else 'VOICE_RENDER_FAILED')
                elif contact is not None:
                    raise RuntimeError('JEV_PLAN_UNSUPPORTED') from None
                else:
                    raise RuntimeError(row['voice_fallback']) from None
            finally:
                if turn_is_current():
                    row.update(voice_prepare_status=voice_status,
                               voice_prepare_seconds=round(monotonic() - started, 3))
        return text
    finally:
        if read_window is not None:
            READ_WINDOW.reset(read_window)
        server._CURRENT_LETTER_MEMORY_SOURCE.reset(source)
        server._CURRENT_LETTER_RECEIPT.reset(receipt)
        CURRENT.reset(presentation)


async def commit(server, row):
    if row.get("delivery_status") != "DELIVERED":
        return
    failures = []
    from runtime.image_understanding import commit_image_memory
    # Memory first: the daily-life extraction can be slow and must never hold it back.
    for consume in (_commit_mailbox_notice, _commit_world, _commit_candidates, _commit_memory, commit_image_memory, _commit_life):
        try:
            await consume(server, row)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures.append(exc)
    if failures:
        raise failures[0]


async def prepare_chat_photo(server, row, send):
    from runtime.image_reply import prepare
    await prepare(server, row, row.get('content', ''), row['reply_text'], channel='qq')


async def deliver_photo(server, row, send):
    from runtime.image_reply import prepare
    from runtime.image_understanding import commit_image_memory
    from runtime.image_reply import is_companion_image
    primary_image = is_companion_image(row)
    if row.get('delivery_status') != ('MEDIA_PENDING' if primary_image else 'DELIVERED'):
        return
    await prepare_chat_photo(server, row, send)
    if row.get('image_status') != 'COMPLETED' or not row.get('prepared_image'):
        return
    if callable(getattr(send, 'is_available', None)) and not send.is_available():
        return  # Safe to send the saved picture only when a later owner replay reconnects.
    row['image_delivery_status'] = 'SENDING'
    await persist_chat(server)
    try:
        receipt = await send.image(row['prepared_image'])
        if isinstance(receipt, bool) or not isinstance(receipt, (str, int)) or not str(receipt):
            raise RuntimeError('QQ_SEND_UNCONFIRMED')
    except BaseException:
        row['image_delivery_status'] = 'UNKNOWN'
        await persist_chat(server)
        raise
    row.update(image_delivery_status='DELIVERED', image_delivery_receipt=str(receipt),
               image_world_status='PENDING')
    if primary_image:
        # The planner's scene draft was never spoken. Conversation records contain only the
        # acknowledged artifact; its visual description has its own consumer.
        from runtime.reply.character_emotion_context import rebind_expression_context
        previous_text = row['reply_text']
        row['reply_text'] = '[已发送图片]'
        rebind_expression_context(row, previous_text=previous_text)
        previous_revision = row.get('reply_revision')
        row.update(delivery_status='DELIVERED', letter_status='COMPLETED', delivered_format='image',
                   reply_revision=1, private_world_status='PENDING', daily_life_status='PENDING',
                   private_world_delivery_id=row['letter_id'] + ':1',
                   private_world_occurred_at=datetime.now().astimezone().isoformat())
        rebind_expression_context(row, previous_revision=previous_revision)
        import hashlib
        row['private_world_reply_sha256'] = hashlib.sha256(row['reply_text'].encode()).hexdigest()
        row['private_world_semantic_key'] = 'canonical.' + hashlib.sha256(row['letter_id'].encode()).hexdigest()
        row.pop('error_code', None)
    await persist_chat(server)
    await commit_image_memory(server, row)


async def _commit_mailbox_notice(server, row):
    if not row.get('mailbox_notice_letter_id'):
        return
    from .mailbox_notice import commit_notice
    store = getattr(server, 'store', None)
    if store is None:
        return
    commit_notice(getattr(store, 'letters', []), row, server._persist_store_state)


async def _commit_world(server, row):
    if row.get("private_world_status") == "PENDING":
        if not server._commit_private_world_letter(row):
            await persist_chat(server)
            raise RuntimeError("PERSONAL_CHAT_WORLD_COMMIT_UNAVAILABLE")
        await persist_chat(server)
    if row.get("delivered_format") == "audio" and row.get("media_world_status") != "COMMITTED":
        server._record_published_media(row, reply_text=row["reply_text"],
            delivery_id=row["private_world_delivery_id"], path=Path(row["prepared_audio"]),
            components=("speech",), presentation="audio")
        await persist_chat(server)
        if row.get("media_world_status") != "COMMITTED":
            raise RuntimeError("PERSONAL_CHAT_AUDIO_WORLD_UNAVAILABLE")

async def _commit_candidates(server, row):
    if row.get("private_world_status") != "COMMITTED":
        return  # Candidate evidence requires the canonical world delivery first.
    if row.get("candidate_delivery_status") not in {"CREATED", "DUPLICATE", "SKIPPED", "DISABLED"}:
        if ('candidate_analysis_attempts' not in row
                and row.get('consumer_error_code') == 'PERSONAL_CHAT_CANDIDATE_UNAVAILABLE'):
            # Existing failed queues already spent this budget before the fix.
            row['candidate_analysis_attempts'] = row.get('consumer_failures', 0)
        if 'candidate_analysis_result' not in row or row.get('candidate_analysis_status') == 'FAILED':
            if row.get('candidate_analysis_attempts', 0) >= 3:
                row['candidate_analysis_retry_status'] = 'EXHAUSTED'
                row.setdefault('candidate_analysis_failure_reason', 'PRIVATE_WORLD_CANDIDATE_ANALYSIS_UNAVAILABLE')
            if row.get('candidate_analysis_retry_status') in {'TERMINAL_REJECTION', 'EXHAUSTED'}:
                row.pop('candidate_analysis_retry_at', None)
                await persist_chat(server)
                return
        if row.get('origin') == 'proactive':
            row['candidate_delivery_status'] = 'SKIPPED'
        elif server.private_world_candidate_store is None:
            row["candidate_delivery_status"] = "DISABLED"
        else:
            result = await server._deliver_private_world_candidate(row, row["content"], row["reply_text"])
            status = getattr(result, "value", None)
            if status not in {"CREATED", "DUPLICATE", "SKIPPED"}:
                if row.get('candidate_analysis_retry_status') in {'TERMINAL_REJECTION', 'EXHAUSTED'}:
                    return
                raise RuntimeError("PERSONAL_CHAT_CANDIDATE_UNAVAILABLE")
            row["candidate_delivery_status"] = status
        await persist_chat(server)

_LIFE_ATTEMPTS = 3
# Daily-life extraction describes what she is doing now. A chat that waited this
# long in a backlog (offline QQ, an earlier failure) would only pay to replay old
# moments in one burst; its memory is still kept by _commit_memory.
_LIFE_STALE_SECONDS = 6 * 3600


async def _commit_life(server, row):
    if row.get("daily_life_status") not in ("COMMITTED", "SKIPPED_STALE"):
        from runtime.private_world.jev_exchange import EXCHANGE_ERROR_CODES
        task = server.daily_life_tasks.get(f"reply:{row['letter_id']}:1")
        if task is None or task.done():
            if row.get('daily_life_retry_status') == 'TERMINAL_REJECTION':
                return
            if row.get('daily_life_failure_reason') in EXCHANGE_ERROR_CODES:
                # A completed semantic rejection cannot be repaired by paying to
                # repeat the same extraction. Keep its failure and pending state.
                row['daily_life_retry_status'] = 'TERMINAL_REJECTION'
                await persist_chat(server)
                return
            if row.get("daily_life_attempts", 0) >= _LIFE_ATTEMPTS:
                return
            try:
                received = datetime.fromisoformat(row["life_received_at"])
            except (KeyError, TypeError, ValueError):
                received = None
            if received is not None and (datetime.now(received.tzinfo) - received).total_seconds() > _LIFE_STALE_SECONDS:
                row["daily_life_status"] = "SKIPPED_STALE"
                await persist_chat(server)
                return
            row["daily_life_attempts"] = row.get("daily_life_attempts", 0) + 1
            server._schedule_daily_life_exchange(row)
            task = server.daily_life_tasks.get(f"reply:{row['letter_id']}:1")
        if task is not None:
            # This task is owned by the daily-life runtime, not this waiter.
            await asyncio.shield(task)
        if row.get("daily_life_status") != "COMMITTED":
            if row.get('daily_life_failure_reason') in EXCHANGE_ERROR_CODES:
                row['daily_life_retry_status'] = 'TERMINAL_REJECTION'
                await persist_chat(server)
            raise RuntimeError("PERSONAL_CHAT_DAILY_LIFE_UNAVAILABLE")
        if row.pop('daily_life_retry_status', None) is not None:
            await persist_chat(server)

async def _commit_memory(server, row):
    # Mem0 is consumed by the existing canonical outbox over persisted state;
    # no second writer or extraction algorithm is introduced here.
    if not row.get("legacy_memory_delivered"):
        if row.get('origin') != 'proactive':
            server.letters_adapter.remember_conversation(row["content"], row["reply_text"])
        row["legacy_memory_delivered"] = True
        await persist_chat(server)


async def recoverable_commit(server, row):
    """A delivered reply survives optional consumer failure without a resend."""
    now = datetime.now().timestamp()
    if now < row.get('consumer_retry_at', 0):
        return
    try:
        try:
            async with asyncio.timeout(_CONSUMER_TIMEOUT_SECONDS):
                await commit(server, row)
        except TimeoutError as exc:
            raise RuntimeError('PERSONAL_CHAT_CONSUMER_TIMEOUT') from exc
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        row["consumer_error_code"] = _failure_code(exc)
        row["consumer_failures"] = row.get("consumer_failures", 0) + 1
        row['consumer_retry_at'] = now + min(3600, 30 * 2 ** min(row['consumer_failures'] - 1, 7))
        await persist_chat(server)
        server._safe_log("personal_chat_consumer_pending", error_code=row["consumer_error_code"])
    else:
        if row.pop("consumer_error_code", None) is not None:
            row.pop("consumer_failures", None)
            row.pop('consumer_retry_at', None)
            await persist_chat(server)


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
    """Explicit settings selection survives restarts and relationship changes."""
    from .contact_invitation import status
    from runtime.reply.proactive_letters import read_json
    root = server._state_root()
    if root is not None:
        saved = read_json(root / 'personal-chat' / 'setup-choice.json')
        if isinstance(saved.get('channels'), list):
            return set(saved['channels']) & {'qq', 'wechat'}
    port = getattr(server, 'private_world_port', None)
    access = status(server.store.letters, port.snapshot() if port is not None else None)
    if 'invitation_id' in access:
        return set(access['channels'])
    if root is None:
        return set()
    # Preserve previously saved bindings even when old invitation rows are absent.
    configured = read_json(root / 'personal-chat' / 'config.json')
    if configured:
        return set(configured) & {'qq', 'wechat'}
    saved = read_json(root / 'personal-chat' / 'existing-access.json')
    return set(saved.get('channels', [])) & {'qq', 'wechat'}


def install_personal_chat(app, server):
    from .setup import install_setup_routes
    install_setup_routes(app, server)
    from .daily_video import install_routes
    install_routes(app, server, _RUNTIME)

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
                jobs.append(("wechat", lambda handler, on_state: run_wechat(credentials, handler, stop_event,
                    cursor_store=_Cursor(server, credentials["account"]), state_callback=on_state)))
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
                jobs.append(("qq", lambda handler, on_state: run_qq(
                    qq["url"], token, *bindings["qq"], handler, stop_event, state_callback=on_state,
                    diagnostic_callback=lambda **fields: server._safe_log('personal_chat_transport_closed',
                        channel='qq', recorded_at_ms=int(datetime.now(LOCAL).timestamp() * 1000), **fields))))
            def sticker_allowed(key):
                from reply_context import ReplyMode
                from runtime.letter_stickers.selection import allowed_stickers
                try:
                    context = server.letters_adapter.build_reply_context(ReplyMode.FUTURE_IM, future_im_enabled=True)
                    from runtime.letter_stickers.packs import installed
                    return key in allowed_stickers(context.private_behavior, channel='qq',
                                                   installed=installed(server._local_data_root()))
                except Exception:
                    return False
            from runtime.image_assets import ensure_image
            service = PersonalChatService(server.store.personal_chats, lambda: persist_chat(server),
                lambda event, row: generate(server, event, row), lambda row: recoverable_commit(server, row), bindings,
                sticker_allowed=sticker_allowed,
                sticker_asset=lambda key: ensure_image(server._local_data_root(), 'stickers', key),
                photo=lambda row, send: deliver_photo(server, row, send),
                prepare_photo=lambda row, send: prepare_chat_photo(server, row, send),
                speech=lambda row, send: deliver_speech(server,row,send))
            from .probe import ProbeJournal
            journal = ProbeJournal(server._state_root() / "personal-chat-diagnostics")
            runtime = {"stop": stop_event, "tasks": [], "service": service, "status": {},
                       "errors": {}, "roundtrips": {}, "last_seen_at": {}, "journal": journal,
                       "delivery_health": {}, "e2e_verified_at": {}, "connection_tests": {}}
            if 'qq' in bindings:
                from .daily_video import DailyVideoWorker
                from .daily_video_author import author_preparation
                from runtime.remote_generation import RemoteGeneration
                runtime['daily_video'] = DailyVideoWorker(server._state_root() / 'media' / 'daily-video',
                    service, bindings['qq'],
                    lambda: RemoteGeneration(os.environ.get('OLIVIA_GPU_API_URL', ''), os.environ.get('OLIVIA_GPU_API_KEY', '')),
                    lambda: getattr(getattr(server, 'daily_life_runtime', None), 'store', None),
                    author_candidate=lambda candidate: author_preparation(server, candidate))
                server._daily_video_worker = runtime['daily_video']
            if 'qq' in config:
                from .setup import _qq_binding_fingerprint
                runtime['qq_binding_fingerprint'] = _qq_binding_fingerprint(config['qq'], token)
            runtime['status'].update({name: 'SETUP_REQUIRED' for name in missing})
            application[_RUNTIME] = runtime
            from .initiative import Initiative
            from runtime.reply import proactive_runtime
            initiative = Initiative(server.store.personal_chats,
                profile_provider=(lambda: proactive_runtime.live_profile(server)) if proactive_runtime.enabled() else None)

            async def handle_connection_test(event, send):
                # Connection diagnostics are never persona/memory inputs. The first
                # leg only proves inbound reception plus an outbound API attempt.
                # E2E verification is recorded only after the owner echoes the
                # challenge that was visible in the real QQ/Weixin client.
                text = event.text.strip()
                now = datetime.now(LOCAL)
                pending = runtime["connection_tests"].get(event.channel)
                if isinstance(pending, dict) and now.timestamp() - float(pending.get("created_at", 0)) > 600:
                    runtime["connection_tests"].pop(event.channel, None)
                    pending = None
                if isinstance(pending, dict) and text == pending.get("code"):
                    journal.delivered(str(pending["exchange_id"]))
                    runtime["connection_tests"].pop(event.channel, None)
                    runtime["roundtrips"][event.channel] = runtime["roundtrips"].get(event.channel, 0) + 1
                    runtime["e2e_verified_at"][event.channel] = now.isoformat()
                    runtime["delivery_health"][event.channel] = "E2E_VERIFIED"
                    if runtime["errors"].get(event.channel) in {
                        "PERSONAL_CHAT_DELIVERY_UNCONFIRMED",
                        "PERSONAL_CHAT_CONNECTION_TEST_CODE_MISMATCH",
                    }:
                        runtime["errors"].pop(event.channel, None)
                    _publish_status(server, runtime)
                    return True
                if text == "/连接测试":
                    if journal.reserve(event.exchange_id):
                        code = f"{secrets.randbelow(10000):04d}"
                        receipt = await send(
                            f"连接测试验证码：{code}\n请原样回复这 4 位数字。回复后设置页会显示端到端已验证。"
                        )
                        classifier = getattr(send, "delivery_confirmation", None)
                        confirmation = classifier(receipt) if callable(classifier) else "PLATFORM_CONFIRMED"
                        runtime["delivery_health"][event.channel] = confirmation
                        runtime["connection_tests"][event.channel] = {
                            "code": code,
                            "created_at": now.timestamp(),
                            "exchange_id": event.exchange_id,
                        }
                        if confirmation == "UNCONFIRMED":
                            runtime["errors"][event.channel] = "PERSONAL_CHAT_DELIVERY_UNCONFIRMED"
                        _publish_status(server, runtime)
                    return True
                if isinstance(pending, dict) and re.fullmatch(r"[0-9]{4}", text):
                    runtime["errors"][event.channel] = "PERSONAL_CHAT_CONNECTION_TEST_CODE_MISMATCH"
                    _publish_status(server, runtime)
                    return True
                return False

            async def handle(event, send):
                if await handle_connection_test(event, send):
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
                            runtime['errors'].pop(event.channel, None)
                            break
                        except Exception:
                            retryable = any(r.get('delivery_status') == 'FAILED' and r.get('generation_attempts', 0) < 2
                                and r.get('generation_retryable') is not False
                                and r.get('binding_id') == event.binding_id
                                and set(r.get('source_messages', {})) & dict(event.sources).keys()
                                for r in server.store.personal_chats)
                            if attempt or not retryable:
                                raise
                            await asyncio.sleep(.5)
                except asyncio.CancelledError:
                    server._safe_log('personal_chat_exchange_cancelled', channel=event.channel,
                                     recorded_at_ms=int(datetime.now(LOCAL).timestamp() * 1000))
                    raise
                except Exception as exc:
                    # A durable failed/uncertain exchange must not kill reception.
                    runtime['errors'][event.channel] = _failure_code(exc)
                    server._safe_log('personal_chat_exchange_failed', channel=event.channel,
                                     error_code=runtime['errors'][event.channel])
                    try:
                        await service.notify_generation_failure(event, send)
                    except Exception:
                        # A notice persistence/transport failure cannot stop reception
                        # or replace the original generation error.
                        server._safe_log('personal_chat_failure_notice_unavailable', channel=event.channel)
                finally:
                    _publish_status(server, runtime)

            def is_control_message(event):
                text = event.text.strip()
                if text == '/连接测试':
                    return True
                pending = runtime['connection_tests'].get(event.channel)
                return (isinstance(pending, dict)
                        and datetime.now(LOCAL).timestamp() - float(pending.get('created_at', 0)) <= 600
                        and re.fullmatch(r'[0-9]{4}', text) is not None)

            handle.is_control_message = is_control_message
            handle.handle_control = handle_connection_test
            async def ingest(event):
                if event.channel not in selected_channels(server):
                    runtime['errors'][event.channel] = 'PERSONAL_CHAT_CONTACT_NOT_ACCEPTED'
                    _publish_status(server, runtime)
                    return False
                return await service.ingest(event)

            handle.ingest = ingest
            handle.pending = lambda channel: service.pending(channel) if channel in selected_channels(server) else ()
            def ready(channel, send):
                service.resume_speech(channel, send)
                if runtime.get('daily_video') is not None:
                    runtime['daily_video'].bind(channel, send)
            handle.ready = ready

            async def run(name, factory):
                def on_state(state):
                    if state not in {"CONNECTING", "CONNECTED", "RECONNECTING", "AUTH_REQUIRED"}:
                        return
                    previous = runtime['status'].get(name)
                    runtime['status'][name] = state
                    if state == "CONNECTED":
                        runtime['last_seen_at'][name] = datetime.now(LOCAL).isoformat()
                        if runtime['errors'].get(name) in {
                            "QQ_TRANSPORT_DISCONNECTED", "QQ_CONNECTION_LOST_DURING_EXCHANGE", "WECHAT_POLL_UNAVAILABLE",
                            "PERSONAL_CHAT_UNAVAILABLE",
                        }:
                            runtime['errors'].pop(name, None)
                    if state != previous:
                        error = runtime['errors'].get(name)
                        server._safe_log('personal_chat_transport_state', channel=name, status=state.lower(),
                                         recorded_at_ms=int(datetime.now(LOCAL).timestamp() * 1000),
                                         **({'error_code': _failure_code(RuntimeError(error))} if error else {}))
                    _publish_status(server, runtime)

                while not stop_event.is_set():
                    try:
                        on_state("CONNECTING")
                        await factory(handle, on_state)
                        if stop_event.is_set():
                            break
                    except asyncio.CancelledError:
                        runtime['status'][name] = 'STOPPED'
                        _publish_status(server, runtime)
                        raise
                    except ValueError as exc:
                        code = _failure_code(exc)
                        runtime['errors'][name] = code
                        if code in {'WECHAT_AUTH_REQUIRED', 'WECHAT_SESSION_STALE'}:
                            runtime['status'][name] = 'AUTH_REQUIRED'
                            _publish_status(server, runtime)
                            return
                        if code == 'QQ_AUTH_REQUIRED':
                            runtime['status'][name] = 'AUTH_REQUIRED'
                            _publish_status(server, runtime)
                            try:
                                await asyncio.wait_for(stop_event.wait(), 15)
                            except asyncio.TimeoutError:
                                pass
                            continue
                        runtime['status'][name] = 'SETUP_REQUIRED'
                        _publish_status(server, runtime)
                        return
                    except Exception as exc:
                        runtime['errors'][name] = _failure_code(exc)
                    on_state("RECONNECTING")
                    try:
                        await asyncio.wait_for(stop_event.wait(), 5)
                    except asyncio.TimeoutError:
                        pass
                runtime['status'][name] = 'STOPPED'
                _publish_status(server, runtime)

            async def boot():
                # Listener liveness does not depend on slow/failed extraction.
                for name, factory in jobs:
                    runtime["tasks"].append(asyncio.create_task(run(name, factory)))
                if runtime.get('daily_video') is not None:
                    runtime['tasks'].append(asyncio.create_task(runtime['daily_video'].monitor()))
                await _recover_chat_loop(server, runtime)
            async def proactive_loop():
                while not stop_event.is_set():
                    await asyncio.sleep(15)
                    try:
                        await _proactive_contact(server, runtime, initiative)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        runtime['errors']['initiative'] = _failure_code(exc)
                    finally:
                        _publish_status(server, runtime)
            runtime['tasks'].append(asyncio.create_task(proactive_loop()))
            runtime["tasks"].append(asyncio.create_task(boot()))
        except Exception:
            server._safe_log("personal_chat_start_failed", error_code="PERSONAL_CHAT_CONFIG_UNAVAILABLE")

    async def stop(application):
        runtime = application.get(_RUNTIME)
        if runtime is None:
            await _stop_semantic_shadow(server)
            return
        runtime["stop"].set()
        tasks = list(runtime["tasks"])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if runtime.get('daily_video') is not None:
            await runtime['daily_video'].close()
            if getattr(server, '_daily_video_worker', None) is runtime['daily_video']:
                server._daily_video_worker = None
        tasks = [*runtime['service'].photo_tasks.values(), *runtime['service'].speech_tasks.values(),
                 *getattr(runtime['service'], 'consumer_tasks', {}).values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _stop_semantic_shadow(server)
        runtime["journal"].db.close()

    activation_lock = asyncio.Lock()
    async def activate_saved_config():
        async with activation_lock:
            # First binding used to require restarting the whole client. Preserve
            # active conversations when an existing binding is being changed.
            if app.get(_RUNTIME) is not None:
                return False
            await start(app)
            return app.get(_RUNTIME) is not None

    app[ACTIVATE_SAVED_CONFIG] = activate_saved_config
    app.on_startup.append(start)
    # Installed before memory/world cleanup so no new sends race shutdown.
    app.on_cleanup.append(stop)
