"""One owner, two transports, and the existing canonical exchange consumers."""
import asyncio
import re
import inspect
import uuid
import hashlib
import logging
from contextvars import ContextVar
from datetime import datetime, timezone

from .events import PersonalMessage
from runtime.reply.character_emotion_context import rebind_expression_context
from runtime.reply.companion_decision import ERROR_CODES as _JEV_PORT_ERRORS
from runtime.cloud_policy import get_policy
from runtime import proactive_config, ui_language
from runtime.ui_i18n_backend import tr
from runtime.reply.proactive_runtime import unprompted
from .qq_faces import face_labels
JEV_ERROR_CODES = _JEV_PORT_ERRORS | {
    'JEV_PLAN_UNSUPPORTED', 'JEV_CONTEXT_UNAVAILABLE', 'JEV_STORED_DECISION_INVALID',
    'JEV_SERVICE_UNAVAILABLE', 'JEV_CONTEXT_BUDGET_EXCEEDED', 'JEV_INPUT_SUPERSEDED',
    'JEV_DECISION_NOT_SAVED', 'JEV_CONFIG_INVALID',
}
# A retry cannot succeed on an insufficient balance; it would only buy another generation.
BALANCE_ERROR_CODES = frozenset({
    'JEV_BALANCE_INSUFFICIENT', 'JEV_BILLING_HTTP_402', 'PERSONAL_CHAT_PROVIDER_QUOTA_EXHAUSTED',
    'LLM_QUOTA_EXHAUSTED', 'GPU_INSUFFICIENT_BALANCE',
})
TURN_IS_CURRENT = ContextVar('personal_chat_turn_is_current', default=None)
# A reply cut off by a restart is written again, once, only for a message that recent.
RESUME_WINDOW_SECONDS = 15 * 60


def _boundary(item, index):
    return (float(item.get('read_boundary_created_at', item.get('created_at', 0))),
            item.get('read_boundary_sequence', item.get('received_sequence', index)))


def _priority(contact):
    """A cloud contact answering what he just bought: it goes out between his messages."""
    from runtime.cloud_contact import PRIORITY_PURPOSES
    return bool(contact) and contact.get('contact_purpose') in PRIORITY_PURPOSES


def _sent_time(row):
    try:
        value = datetime.fromisoformat((row.get('read_boundary_sent_at') or row['user_sent_at']).replace('Z', '+00:00'))
        return value if value.utcoffset() is not None else None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def chat_text(reply_text):
    """The reply as it is sent and stored: a Chinese full stop becomes a line break, and Chinese chat does not end a
    sentence with a stray ASCII period (English sentences keep theirs)."""
    text = re.sub(r'。+[ \t]*', '\n', reply_text)
    if not ui_language.is_english():
        text = re.sub(r'(?<!\.)\.(?!\.)(?=\s|$)', '', text)
    return text.strip()


def _clear_draft(row):
    for key in ('reply_text', 'prepared_audio', 'reply_audio_duration', 'voice_fallback', 'sticker_id', 'qq_poke',
                'letter_invitation', 'initiative_preference', 'pause_until', 'letter_preference',
                'letter_until', 'followup_at', 'followup_quote', 'listening_preference', 'requested_format',
                'presentation_status', 'delivery_basis', 'voice_ready', 'quality_status', 'reviewer_calls', 'rewrite_calls',
                'mailbox_notice_letter_id', 'decision_rejection_reason', 'semantic_shadow', 'expression_context',
                'companion_decision', 'companion_timing', 'companion_delivery', 'photo_with_words', 'companion_context_recovery',
                'speech_script', 'speech_intent', 'speech_status', 'speech_delivery_status', 'content_review',
                'piano_input', 'piano_status', 'piano_delivery_status', 'piano_midi', 'piano_file_notice', 'piano_input_error',
                'proactive_decision', 'proactive_basis', 'proactive_opportunity', 'generation_failure_notice',
                'generation_context_at', 'stage_timing_seconds', 'stage_actual_calls', 'stage_cache_hits',
                'degraded_stages', 'default_voice_status', 'default_voice_seconds', 'default_voice_duration',
                'voice_prepare_seconds', 'voice_prepare_status', 'voice_prepare_timeout_seconds',
                'generation_retryable', 'generation_failure_context', 'generation_failures', 'generation_notice_code',
                'silence_reason', 'skip_reason', 'proactive_skip', 'user_controls_applied',
                'daily_video_request', 'daily_video_status', 'daily_video_error_code', 'daily_video_candidates',
                'writer_checkpoint', 'generation_run_id', 'generation_attempt_id', 'generation_recovery',
                'send_attempt_id', 'send_input_revision', 'delivery_receipt'):
        row.pop(key, None)


async def persist_state(persist):
    result = persist()
    if inspect.isawaitable(result):
        await result


async def photo_notice(row, send, persist, kind, text):
    await delivery_notice(row, send, persist, 'image_' + kind + '_notice', text)


def photo_failure_text(row):
    """A refused balance needs a top-up, not a "try again later" hint."""
    if row.get('image_error_code') == 'GPU_INSUFFICIENT_BALANCE':
        return tr('照片这次没生成成功，没能发给你。Olivia 可用余额不足，请充值后重试。')
    return tr('照片这次没生成成功，没能发给你。稍后再试一下。')


async def delivery_notice(row, send, persist, key, text):
    """At most one attempt per notice, including uncertain platform delivery."""
    if row.get(key):
        return
    row[key] = 'SENDING'
    await persist_state(persist)
    try:
        receipt = await send(text)
        row[key] = 'DELIVERED' if isinstance(receipt, (str, int)) and not isinstance(receipt, bool) and str(receipt) else 'UNKNOWN'
    except asyncio.CancelledError:
        row[key] = 'UNKNOWN'
        raise
    except Exception:
        row[key] = 'UNKNOWN'
    finally:
        await persist_state(persist)


class PersonalChatService:
    def __init__(self, rows, persist, generate, commit, bindings, *, sticker_allowed=lambda key: True, sticker_asset=None, photo=None, prepare_photo=None, speech=None, default_voice=None, inspect_writer=None, piano=None):
        self.piano = piano
        self.rows, self.persist = rows, persist
        self.generate, self.commit = generate, commit
        self.bindings = dict(bindings)
        self.run_id = uuid.uuid4().hex
        self.inspect_writer = inspect_writer
        self.sticker_allowed = sticker_allowed
        self.sticker_asset = sticker_asset
        self.photo = photo
        self.prepare_photo = prepare_photo
        self.photo_tasks = {}
        self.photo_slots = asyncio.Semaphore(2)
        self.speech = speech
        self.default_voice = default_voice
        self.speech_tasks = {}
        self.typing = {}
        self.active_user_turns = set()
        self.handling = 0
        self.consumer_tasks = {}
        # One owner shares memory/world across both channels; serialize exchanges.
        self.lock = asyncio.Lock()
        self.batch_lock = asyncio.Lock()
        self.intake_lock = asyncio.Lock()
        self.proactive_generation = None
        self.user_revision = max((r.get('received_sequence', 0) for r in rows
                                  if isinstance(r.get('received_sequence', 0), int)), default=0)

    def _validate_input(self, event):
        if not isinstance(event, PersonalMessage) or self.bindings.get(event.channel) != (event.account_id, event.owner_id):
            raise ValueError('PERSONAL_CHAT_OWNER_MISMATCH')
        if not event.text.strip() or len(event.text) > 10000:
            raise ValueError('PERSONAL_CHAT_INPUT_INVALID')

    @staticmethod
    def _stored_event(row, channel, account_id, owner_id):
        sources = row['source_messages']
        return PersonalMessage(channel, account_id, owner_id, next(iter(sources)),
            row['content'], tuple(sources.items()), row.get('input_kind', 'text'), row.get('user_sent_at'),
            tuple(tuple(item) for item in row.get('incoming_images', [])),
            tuple(tuple(item) for item in row.get('incoming_media', [])))

    async def ingest(self, event):
        """Save owner input before waiting for generation; replay IDs are idempotent."""
        from .events import combine
        self._validate_input(event)
        async with self.intake_lock:
            remaining = dict(event.sources)
            for row in self.rows:
                if row.get('channel') != event.channel or row.get('binding_id', event.binding_id) != event.binding_id:
                    continue
                sources = row.get('source_messages', {})
                if not sources and row.get('letter_id') == event.exchange_id:
                    sources = {event.message_id: row['content']}
                overlap = remaining.keys() & sources.keys()
                if any(remaining[key] != sources[key] for key in overlap):
                    raise ValueError('PERSONAL_CHAT_ID_CONFLICT')
                for key in overlap:
                    del remaining[key]
            if remaining:
                incoming = combine([PersonalMessage(event.channel, event.account_id, event.owner_id, key, text,
                    input_kind=event.input_kind, sent_at=event.sent_at,
                    images=tuple(item for item in event.images if item[0] == key),
                    media=tuple(item for item in event.media if item[0] == key))
                    for key, text in remaining.items()])
                now = datetime.now(timezone.utc)
                self.user_revision += 1
                self.rows.append({'letter_id': incoming.exchange_id, 'content': incoming.text,
                    'source_messages': dict(incoming.sources), 'binding_id': incoming.binding_id,
                    'input_kind': incoming.input_kind, 'incoming_images': [list(item) for item in incoming.images],
                    'incoming_media': [list(item) for item in incoming.media],
                    'channel': incoming.channel, 'reply_mode': 'future_im',
                    'life_received_at': now.isoformat(), 'created_at': now.timestamp(),
                    'user_sent_at': incoming.sent_at, 'received_sequence': self.user_revision,
                    'delivery_status': 'RECEIVED', 'letter_status': 'PROCESSING'})
                if self.proactive_generation is not None:
                    self.proactive_generation.cancel()
            # Retry persistence even for a repeated ID: an earlier write may have
            # failed after the row was added in memory. Never generate on that basis.
            await persist_state(self.persist)

    def pending(self, channel):
        """Resume receipts and finished drafts that have no send reservation."""
        binding = self.bindings.get(channel)
        if binding is None:
            return ()
        account_id, owner_id = binding
        binding_id = PersonalMessage(channel, account_id, owner_id, '', '').binding_id
        rows = [row for row in self.rows if row.get('channel') == channel
                and row.get('binding_id') == binding_id
                and (row.get('delivery_status') == 'RECEIVED'
                     or channel == 'qq' and row.get('delivery_status') == 'GENERATED'
                     and isinstance(row.get('reply_text'), str)
                     and row['reply_text'].strip() and len(row['reply_text']) <= 10000)
                and not row.get('send_attempt_id') and not row.get('delivery_receipt')
                and row.get('source_messages') and not row.get('superseded_by')]
        rows.sort(key=lambda row: (row.get('received_sequence', 0), float(row.get('created_at', 0))))
        return tuple(self._stored_event(row, channel, account_id, owner_id) for row in rows)

    async def _merge_receipts(self, event):
        """Coalesce raw receipts once; preserve IDs when a platform regroups a burst."""
        async with self.intake_lock:
            keys = dict(event.sources).keys()
            rows = [row for row in self.rows if row.get('channel') == event.channel
                    and row.get('binding_id') == event.binding_id and row.get('delivery_status') == 'RECEIVED'
                    and row.get('source_messages') and row['source_messages'].keys() & keys]
            if len(rows) < 2:
                return
            rows.sort(key=lambda row: (row.get('received_sequence', 0), float(row.get('created_at', 0))))
            self._combine_rows(event, rows)
            await persist_state(self.persist)

    def _combine_rows(self, event, rows):
        from .events import combine
        merged = combine([self._stored_event(row, event.channel, event.account_id, event.owner_id) for row in rows])
        if len(merged.images) + len(merged.media) > 4:
            raise ValueError('PERSONAL_CHAT_INPUT_BATCH_TOO_LARGE')
        canonical = rows[0]
        if canonical.get('incoming_images') != [list(item) for item in merged.images]:
            canonical.pop('incoming_image_status', None)
        latest = max(rows, key=lambda row: row.get('read_boundary_sequence', row.get('received_sequence', 0)))
        sent_at = max((at for row in rows if (at := _sent_time(row)) is not None), default=None)
        canonical.update(content=merged.text, source_messages=dict(merged.sources), input_kind=merged.input_kind,
                         incoming_images=[list(item) for item in merged.images],
                          incoming_media=[list(item) for item in merged.media],
                         read_boundary_created_at=latest.get('read_boundary_created_at', latest['created_at']),
                         read_boundary_sequence=latest.get('read_boundary_sequence', latest['received_sequence']),
                         read_boundary_sent_at=sent_at.isoformat() if sent_at else None)
        for row in rows[1:]:
            row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                       superseded_by=canonical['letter_id'], skip_reason='MERGED_RECEIPT')
        return merged

    def _new_inputs(self, row):
        cutoff = row.get('read_boundary_sequence', row.get('received_sequence', 0))
        sent_at = _sent_time(row)
        return [other for other in self.rows if other is not row
                and other.get('binding_id') == row.get('binding_id')
                and other.get('delivery_status') == 'RECEIVED' and not other.get('superseded_by')
                and other.get('received_sequence', 0) > cutoff
                and (sent_at is None or _sent_time(other) is None or _sent_time(other) >= sent_at)]

    async def _refresh_draft(self, row, event):
        """Called under intake_lock before SENDING; return a new input revision."""
        pending = self._new_inputs(row)
        if not pending:
            return None
        _clear_draft(row)
        latest = max(pending, key=lambda item: item.get('received_sequence', 0))
        gap = float(latest.get('created_at', 0)) - float(row.get('read_boundary_created_at', row.get('created_at', 0)))
        before, after = _sent_time(row), _sent_time(latest)
        if gap > 300 or before is not None and after is not None and (after - before).total_seconds() > 300:
            row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                       error_code='PERSONAL_CHAT_STALE_REPLY', skip_reason='STALE_REPLY')
            await persist_state(self.persist)
            return self._stored_event(latest, event.channel, event.account_id, event.owner_id)
        try:
            merged = self._combine_rows(event, [row, *pending])
        except ValueError:
            # Preserve every receipt and expose the bounded-input failure. The
            # next input can still see the unanswered original through context.
            row.update(delivery_status='FAILED', letter_status='FAILED', generation_attempts=2,
                       error_code='PERSONAL_CHAT_INPUT_BATCH_TOO_LARGE')
            await persist_state(self.persist)
            raise RuntimeError('PERSONAL_CHAT_INPUT_BATCH_TOO_LARGE') from None
        row.update(delivery_status='RECEIVED', letter_status='PROCESSING', generation_attempts=0,
                   input_revision=row.get('input_revision', 0) + 1)
        row.pop('error_code', None)
        await persist_state(self.persist)
        return merged

    async def _generate(self, event, row, send):
        from runtime.incoming_media import MEDIA_RESOLVER
        start_typing = getattr(send, 'start_typing', None)
        if callable(start_typing) and event.exchange_id not in self.typing:
            try:
                self.typing[event.exchange_id] = start_typing()
            except Exception:
                logging.getLogger(__name__).debug('ONEBOT_TYPING_START_UNAVAILABLE')
        timings = row.setdefault('reply_timing_seconds', {})
        timings.setdefault('queue_wait', round(max(0, min(86400,
            datetime.now(timezone.utc).timestamp() - row.get('created_at', datetime.now(timezone.utc).timestamp()))), 4))
        timings.setdefault('voice_wait', 0.0)
        media_token = MEDIA_RESOLVER.set(getattr(send, 'resolve_media', None))
        from .qq_games import THROWER
        thrower = THROWER.set(getattr(send, 'game', None))  # her answer to his dice or rock-paper-scissors
        current = TURN_IS_CURRENT.set(lambda: not self._new_inputs(row))
        try:
            return await self.generate(event, row)
        finally:
            TURN_IS_CURRENT.reset(current)
            THROWER.reset(thrower)
            MEDIA_RESOLVER.reset(media_token)

    def replies_in_flight(self):
        """A reply to the owner is being generated or sent. An update restart waits for it."""
        return self.handling > 0 or bool(self.active_user_turns)

    async def handle(self, event, send):
        self.handling += 1
        try:
            await self.ingest(event)
            # A platform replay can regroup a previously delivered burst differently.
            # Resolve original IDs before choosing a new canonical exchange identity.
            async with self.batch_lock:
                # One initial draft plus at most one regenerated draft per pass.
                # Continued input returns to the durable queue and its merge window.
                for _ in range(2):
                    updated = await self._handle_batch(event, send)
                    if updated is None:
                        return
                    event = updated
        finally:
            self.handling -= 1

    async def notify_generation_failure(self, event, send):
        """One system notice per exhausted input; never publish an unchecked draft."""
        sources = dict(event.sources).keys()
        for row in reversed(self.rows):
            if (row.get('binding_id') != event.binding_id or row.get('origin') == 'proactive'
                    or row.get('superseded_by') or row.get('delivery_status') != 'FAILED'
                    or (row.get('generation_attempts', 0) < 2 and row.get('generation_retryable') is not False)
                    or not sources & row.get('source_messages', {}).keys()):
                continue
            correlated = send.for_exchange(event) if callable(getattr(send, 'for_exchange', None)) else send
            code = row.get('generation_notice_code') or row.get('error_code')
            notices = get_policy('notices')
            text = tr(notices.get(code, notices['DEFAULT']))
            await delivery_notice(row, correlated, self.persist, 'generation_failure_notice', text)
            return

    async def _handle_batch(self, event, send):
        from .events import combine
        await self._merge_receipts(event)
        remaining = dict(event.sources)
        for row in list(self.rows):
            if row.get('superseded_by') or row.get('channel') != event.channel or row.get('binding_id', event.binding_id) != event.binding_id:
                continue
            sources = row.get('source_messages', {})
            if not sources and row.get('letter_id') == event.exchange_id:
                sources = {event.message_id: row['content']}
            overlap = remaining.keys() & sources.keys()
            if not overlap:
                continue
            if any(remaining[key] != sources[key] for key in overlap):
                raise ValueError('PERSONAL_CHAT_ID_CONFLICT')
            stored = PersonalMessage(event.channel, event.account_id, event.owner_id,
                next(iter(sources)), row['content'], tuple(sources.items()), row.get('input_kind','text'), row.get('user_sent_at'),
                tuple(tuple(item) for item in row.get('incoming_images', [])),
                tuple(tuple(item) for item in row.get('incoming_media', [])))
            terminal = row.get('delivery_status') in {'SENDING', 'DELIVERY_UNCONFIRMED'} or (
                row.get('delivery_status') == 'FAILED' and (row.get('generation_attempts', 0) >= 2
                                                          or row.get('generation_retryable') is False))
            if not terminal or remaining.keys() == overlap:
                updated = await self._handle_one(stored, send.for_exchange(stored) if callable(getattr(send, 'for_exchange', None)) else send)
                if updated is not None:
                    return updated
            for key in overlap:
                del remaining[key]
        if remaining:
            fresh = combine([PersonalMessage(event.channel, event.account_id, event.owner_id, key, text, input_kind=event.input_kind, sent_at=event.sent_at,
                                            images=tuple(item for item in event.images if item[0] == key),
                                            media=tuple(item for item in event.media if item[0] == key))
                             for key, text in remaining.items()])
            return await self._handle_one(fresh, send.for_exchange(fresh) if callable(getattr(send, 'for_exchange', None)) else send)

    async def proactive(self, event, send, eligible=lambda: True, followup=None, contact=None):
        """contact: the fields of a cloud contact this exchange carries out (see runtime.cloud_contact)."""
        revision = self.user_revision
        async with self.batch_lock:
            if _priority(contact) and not self._unanswered(event):
                # His purchase is answered at the first gap between his messages, not once he stops writing.
                revision = self.user_revision
            if not eligible() or revision != self.user_revision:
                return
            await self._handle_one(event, send, proactive=True, followup=followup,
                                   proactive_revision=revision, proactive_eligible=eligible, contact=contact)

    @staticmethod
    async def _poke_first(row, send):
        """A poke she chose goes just before her message; a lost poke never holds the message back."""
        if row.get('qq_poke') != 'PENDING' or not callable(getattr(send, 'poke', None)):
            return
        try:
            await send.poke()
            row['qq_poke'] = 'SENT'
        except Exception:
            row['qq_poke'] = 'FAILED'

    @staticmethod
    async def _after_delivery(row, event, send):
        """A face she wrote follows a voice message: faces are never spoken."""
        changed = False
        labels = face_labels(row.get('reply_text', ''))
        if row.get('delivered_format') == 'audio' and labels:
            try:
                await getattr(send, 'plain', send)(labels[0])
                row['qq_face_after_voice'] = 'SENT'
            except Exception:
                row['qq_face_after_voice'] = 'FAILED'
            changed = True
        return changed

    def _unanswered(self, event):
        """A message of his on this binding is still waiting for her reply."""
        return any(row.get('binding_id') == event.binding_id and row.get('source_messages')
                   and not row.get('superseded_by')
                   and row.get('delivery_status') in {'RECEIVED', 'GENERATING', 'GENERATED'}
                   for row in self.rows)

    async def _handle_one(self, event, send, proactive=False, followup=None, proactive_revision=None,
                          proactive_eligible=None, contact=None):
        key = getattr(event, 'exchange_id', None)
        if not proactive:
            self.active_user_turns.add(key)
        try:
            return await self._handle_one_impl(event, send, proactive, followup, proactive_revision,
                                               proactive_eligible, contact)
        finally:
            self._stop_typing(event)
            if not proactive:
                self.active_user_turns.discard(key)

    def _stop_typing(self, event):
        cancel = self.typing.pop(getattr(event, 'exchange_id', None), None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                logging.getLogger(__name__).debug('ONEBOT_TYPING_STOP_UNAVAILABLE')

    async def _handle_one_impl(self, event, send, proactive=False, followup=None, proactive_revision=None,
                               proactive_eligible=None, contact=None):
        if not isinstance(event, PersonalMessage) or self.bindings.get(event.channel) != (event.account_id, event.owner_id):
            raise ValueError("PERSONAL_CHAT_OWNER_MISMATCH")
        if (not event.text.strip() and not proactive) or len(event.text) > 10000:
            raise ValueError("PERSONAL_CHAT_INPUT_INVALID")
        priority = proactive and _priority(contact)

        def outdated():
            # He wrote meanwhile: her own message gives way, the answer to his purchase does not.
            return proactive and not priority and proactive_revision != self.user_revision

        async with self.lock:
            if proactive and outdated():
                return
            row = next((r for r in self.rows if r.get("letter_id") == event.exchange_id), None)
            if row is not None:
                if row.get("content") != event.text:
                    raise ValueError("PERSONAL_CHAT_ID_CONFLICT")
                # Generated-but-unsent is resumable. SENDING is deliberately
                # not: the platform may have delivered before a crash/timeout.
                if row.get("delivery_status") == "DELIVERED":
                    self._schedule_photo(row, send)
                    self._schedule_speech(row, send)
                    self._schedule_commit(row)
                    await asyncio.sleep(0)
                    return
                if row.get('delivery_status') == 'SKIPPED':
                    return
                if row.get('delivery_status') == 'MEDIA_PENDING':
                    self._schedule_photo(row, send)
                    return
                position = self.rows.index(row)
                if row.get('delivery_status') in {'RECEIVED', 'GENERATED', 'FAILED', 'GENERATING'} and any(
                    old is not row
                    and (old.get('delivery_status') == 'DELIVERED'
                         and _boundary(old, index) > _boundary(row, position)
                         or old.get('delivery_status') == 'RECEIVED'
                         and old.get('binding_id') == row.get('binding_id')
                         and _boundary(old, index)[0] - _boundary(row, position)[0] > 300)
                    for index, old in enumerate(self.rows)
                ):
                    # Replays include failed or interrupted generation, not just
                    # unsent drafts. Never answer an old turn after a newer reply.
                    row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                               error_code='PERSONAL_CHAT_STALE_REPLY', skip_reason='STALE_REPLY')
                    await persist_state(self.persist)
                    return
                if row.get("delivery_status") not in {"RECEIVED", "GENERATED", "FAILED", "GENERATING"}:
                    raise RuntimeError("PERSONAL_CHAT_DELIVERY_REQUIRES_ATTENTION")
            else:
                now = datetime.now(timezone.utc).isoformat()
                row = {"letter_id": event.exchange_id, "content": event.text,
                       "source_messages": dict(event.sources),
                       "binding_id": event.binding_id,
                       "input_kind": event.input_kind,
                       "incoming_images": [list(item) for item in event.images],
                       "incoming_media": [list(item) for item in event.media],
                       "channel": event.channel, "reply_mode": "future_im",
                       "life_received_at": now, "created_at": datetime.now(timezone.utc).timestamp(),
                       "user_sent_at": event.sent_at,
                       "delivery_status": "GENERATING", "letter_status": "PROCESSING"}
                if proactive:
                    row.update(origin='proactive', source_messages={})
                    if followup:
                        row.update(followup_source_id=followup['letter_id'], followup_quote=followup['followup_quote'])
                    if contact:
                        row.update(contact)
                self.rows.append(row)
                await persist_state(self.persist)
            if not proactive and _sent_time(row):
                # Platform backlog can arrive for the first time after a newer
                # turn was answered. Arrival/row order cannot detect that case.
                for old in self.rows:
                    if (old is row or old.get('delivery_status') not in {'DELIVERED', 'RECEIVED'}
                            or old.get('binding_id') != event.binding_id):
                        continue
                    try:
                        before = _sent_time(row)
                        after = _sent_time(old)
                    except (KeyError, AttributeError, TypeError, ValueError):
                        continue
                    if (before is not None and after is not None and after > before
                            and (old.get('delivery_status') == 'DELIVERED' or (after - before).total_seconds() > 300)):
                        row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                                   error_code='PERSONAL_CHAT_STALE_REPLY', skip_reason='STALE_REPLY')
                        await persist_state(self.persist)
                        return
            if row.get("delivery_status") != "GENERATED":
                if row.get('generation_retryable') is False:
                    raise RuntimeError(row.get('error_code') or 'PERSONAL_CHAT_GENERATION_RETRY_EXHAUSTED')
                if row.get("generation_attempts", 0) >= 2:
                    raise RuntimeError("PERSONAL_CHAT_GENERATION_RETRY_EXHAUSTED")
                validated_controls = row.get('user_controls_applied') is True
                for key in ('prepared_audio', 'reply_audio_duration', 'voice_fallback', 'sticker_id', 'qq_poke',
                            'letter_invitation', 'initiative_preference', 'pause_until', 'letter_preference',
                            'letter_until', 'followup_at', 'listening_preference', 'requested_format', 'presentation_status',
                            'delivery_basis', 'voice_ready',
                            'expression_context', 'companion_timing', 'companion_delivery', 'photo_with_words',
                            'voice_prepare_seconds', 'voice_prepare_status', 'voice_prepare_timeout_seconds'):
                    if validated_controls and key in ('initiative_preference', 'pause_until', 'letter_preference',
                                                     'letter_until', 'followup_at', 'listening_preference'):
                        continue  # Same input revision retains independently validated user authority.
                    row.pop(key, None)
                if not proactive and not validated_controls:
                    row.pop('followup_quote', None)
                row["generation_attempts"] = row.get("generation_attempts", 0) + 1
                row.pop('generation_interrupted', None)
                row.pop('generation_retryable', None)
                row.pop('generation_failure_context', None)
                row.pop('decision_defaulted_fields', None)
                row.pop('decision_warning_codes', None)
                row.pop('decision_dropped_media', None)
                row.pop('decision_dropped_controls', None)
                row.update(delivery_status="GENERATING", letter_status="PROCESSING",
                           generation_run_id=self.run_id, generation_attempt_id=uuid.uuid4().hex)
                await persist_state(self.persist)
                try:
                    row['voice_available'] = callable(getattr(send, 'audio', None))
                    row['image_available'] = callable(self.photo) and callable(getattr(send, 'image', None))
                    if proactive:
                        if outdated():
                            row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                                       error_code='PERSONAL_CHAT_USER_PRIORITY', skip_reason='USER_PRIORITY')
                            await persist_state(self.persist)
                            return
                        task = asyncio.create_task(self._generate(event, row, send))
                        if not priority:  # a new message from him cancels only her own initiative
                            self.proactive_generation = task
                        try:
                            text = await task
                        except asyncio.CancelledError:
                            if asyncio.current_task().cancelling():
                                raise
                            row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                                       error_code='PERSONAL_CHAT_USER_PRIORITY', skip_reason='USER_PRIORITY')
                            await persist_state(self.persist)
                            return
                        finally:
                            self.proactive_generation = None
                    else:
                        text = await self._generate(event, row, send)
                    if not proactive:
                        # A no-reply result must not swallow a newer receipt.
                        async with self.intake_lock:
                            updated = await self._refresh_draft(row, event)
                            if updated is not None:
                                return updated
                            reason = row.get('silence_reason')
                            authorized_quiet = (
                                row.get('companion_timing') == 'wait_user' and reason == 'USER_REQUESTED_WAIT'
                                or row.get('companion_timing') == 'no_reply' and reason == 'USER_REQUESTED_NO_REPLY')
                            if text is None and authorized_quiet:
                                row.update(delivery_status='SKIPPED', letter_status='SKIPPED', skip_reason=reason)
                                await persist_state(self.persist)
                                return
                            if text is None and row.get('skip_reason') == 'DUPLICATE_CONTENT':
                                # Her draft said again, word for word, what she had just sent: it is not sent twice.
                                row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                                           error_code='PERSONAL_CHAT_DUPLICATE_CONTENT')
                                await persist_state(self.persist)
                                return
                    if proactive and text.strip() == '[[skip]]':
                        row.update(delivery_status='SKIPPED', letter_status='SKIPPED', skip_reason='PROACTIVE_NO_REPLY')
                        await persist_state(self.persist)
                        return
                    if not isinstance(text, str) or not text.strip() or len(text) > 10000:
                        raise ValueError("PERSONAL_CHAT_REPLY_INVALID")
                    row.update(reply_text=text, delivery_status="GENERATED")
                    rebind_expression_context(row)
                    await persist_state(self.persist)
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        code = 'PERSONAL_CHAT_GENERATION_INTERRUPTED'
                        row['generation_interrupted'] = True
                    else:
                        code = str(exc)
                    named = getattr(exc, 'code', None)
                    no_retry = get_policy('generation_retry')['no_retry_codes']
                    blocked_retry = code in no_retry or isinstance(named, str) and named in no_retry
                    balance = code in BALANCE_ERROR_CODES or isinstance(named, str) and named in BALANCE_ERROR_CODES
                    if isinstance(named, str) and named in no_retry:
                        code = named
                    if code in get_policy('notices'):
                        row['generation_notice_code'] = code
                    if (code not in no_retry or code in BALANCE_ERROR_CODES) and code not in JEV_ERROR_CODES and not re.fullmatch(r'(?:PERSONAL_CHAT|IMAGE|LLM)_[A-Z0-9_]{1,80}', code):
                        code = 'PERSONAL_CHAT_PROVIDER_QUOTA_EXHAUSTED' if balance else 'PERSONAL_CHAT_GENERATION_FAILED'
                    row.update(delivery_status="FAILED", letter_status="FAILED", error_code=code)
                    retryable = False if blocked_retry else getattr(exc, 'retryable', None)
                    if type(retryable) is bool:
                        row['generation_retryable'] = retryable
                    from runtime.diagnostics.failure_context import provider_failure_context
                    context = provider_failure_context(getattr(exc, 'failure_context', {}))
                    if context:
                        row['generation_failure_context'] = context
                        receipt = {**context, 'generation_attempt': row['generation_attempts']}
                        if type(retryable) is bool:
                            receipt['retryable'] = retryable
                        row['generation_failures'] = [*row.get('generation_failures', []), receipt][-2:]
                    await persist_state(self.persist)
                    raise
            if callable(getattr(send, 'is_available', None)) and not send.is_available():
                raise RuntimeError('PERSONAL_CHAT_CHANNEL_DISCONNECTED')
            if not proactive:
                async with self.intake_lock:
                    updated = await self._refresh_draft(row, event)
                    if updated is not None:
                        return updated
            if row.get('companion_delivery') == 'image' and row.get('photo_with_words') is not True:
                from runtime.image_reply import is_companion_image
                if not is_companion_image(row):
                    raise RuntimeError('JEV_PLAN_UNSUPPORTED')
                # Release the conversation lock while the existing media worker
                # generates the picture. No placeholder or scene draft is sent.
                row.update(delivery_status='MEDIA_PENDING', letter_status='PROCESSING')
                await persist_state(self.persist)
                self._schedule_photo(row, send)
                return
            # Keep the stored canonical text identical to the displayed IM text.
            text = chat_text(row['reply_text'])
            if not text:
                raise ValueError('PERSONAL_CHAT_REPLY_INVALID')
            if proactive:
                if outdated():
                    row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                               error_code='PERSONAL_CHAT_USER_PRIORITY', skip_reason='USER_PRIORITY')
                    await persist_state(self.persist)
                    return
                normalized = lambda value: re.sub(r'[\s。.]', '', value)
                cutoff = datetime.now(timezone.utc).timestamp() - 86400
                if any(old is not row and old.get('delivery_status') in {'DELIVERED', 'SENDING', 'DELIVERY_UNCONFIRMED'}
                       and float(old.get('created_at', 0)) >= cutoff
                       and normalized(old.get('reply_text', '')) == normalized(text)
                       for old in self.rows):
                    row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                               error_code='PERSONAL_CHAT_DUPLICATE_CONTENT', skip_reason='DUPLICATE_CONTENT')
                    await persist_state(self.persist)
                    return
            audio = row.get('prepared_audio')
            if audio and callable(getattr(send, 'prepare_audio', None)):
                try:
                    audio = await send.prepare_audio(audio)
                except Exception:
                    audio = None
                    row['voice_fallback'] = 'PERSONAL_CHAT_AUDIO_UPLOAD_UNAVAILABLE'
            if not (audio and callable(getattr(send, 'audio', None))):
                # Long speech is a separately persisted MP3 file. Its confirmation
                # does not require a second, ordinary voice-bubble generation.
                file_speech = (event.channel=='qq' and row.get('speech_script')
                               and (row.get('companion_decision') or {}).get('speech_request'))
                if file_speech:
                    from .speech import validate_script, validate_intent
                    validate_script(row['speech_script'])
                    validate_intent(row['speech_intent'])
                # Text chosen because speech could not be rendered is deliberate; a lost upload is not.
                text_fallback = row.get('delivery_basis') in {'VOICE_RENDER_TIMEOUT', 'VOICE_RENDER_FAILED'}
                if not file_speech and not row.get('piano_input') and not text_fallback and (row.get('companion_decision') is not None and row.get('companion_delivery') == 'audio_speech'
                        or row.get('proactive_decision', {}).get('decision', {}).get('medium') == 'audio_speech'):
                    row.update(delivery_status='FAILED', letter_status='FAILED', error_code='JEV_PLAN_UNSUPPORTED')
                    await persist_state(self.persist)
                    raise RuntimeError('JEV_PLAN_UNSUPPORTED')
                if (unprompted(row) and row.get('requested_format') == 'voice'
                        and 'text' not in proactive_config.media('qq')):
                    # Her own contact is a voice message or nothing: never the text of a lost voice.
                    row.update(delivery_status='SKIPPED', letter_status='SKIPPED', skip_reason='PROACTIVE_NO_REPLY',
                               proactive_skip='PERSONAL_CHAT_PROACTIVE_SKIP_VOICE_ONLY')
                    await persist_state(self.persist)
                    return
                previous_text = row['reply_text']
                row['reply_text'] = text
                rebind_expression_context(row, previous_text=previous_text)
            async with self.intake_lock:
                if not proactive:
                    updated = await self._refresh_draft(row, event)
                    if updated is not None:
                        return updated
                elif outdated():
                    row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                               error_code='PERSONAL_CHAT_USER_PRIORITY', skip_reason='USER_PRIORITY')
                    await persist_state(self.persist)
                    return
                if proactive and callable(proactive_eligible) and not proactive_eligible():
                    row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                               error_code='PERSONAL_CHAT_CONTACT_SUPERSEDED', skip_reason='CONTACT_SUPERSEDED')
                    await persist_state(self.persist)
                    return
                row["delivery_status"] = "SENDING"
                row['send_attempt_id'] = row.get('generation_attempt_id')
                row['send_input_revision'] = row.get('input_revision', 0)
                await persist_state(self.persist)
            if proactive and (outdated()
                              or callable(proactive_eligible) and not proactive_eligible()):
                row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                           error_code=('PERSONAL_CHAT_USER_PRIORITY' if outdated()
                                       else 'PERSONAL_CHAT_CONTACT_SUPERSEDED'),
                           skip_reason=('USER_PRIORITY' if outdated() else 'CONTACT_SUPERSEDED'))
                await persist_state(self.persist)
                return
            await self._poke_first(row, send)
            receipt = None
            try:
                from runtime.diagnostics.reply_telemetry import timing
                with timing('send_ack', row):
                    if audio and callable(getattr(send, 'audio', None)):
                        receipt = await send.audio(audio)
                        row['delivered_format'] = 'audio'
                    else:
                        receipt = await send(row["reply_text"])
                        row['delivered_format'] = 'text'
            except BaseException:
                self._stop_typing(event)
                row["error_code"] = "PERSONAL_CHAT_DELIVERY_UNKNOWN"
                await persist_state(self.persist)
                raise
            self._stop_typing(event)
            classifier = getattr(send, 'delivery_confirmation', None)
            if callable(classifier):
                confirmation = classifier(receipt)
                row['transport_confirmation'] = confirmation
                if event.channel == 'wechat' and isinstance(receipt, dict):
                    row['wechat_send_response'] = receipt
                if confirmation == 'UNCONFIRMED':
                    row.update(delivery_status='DELIVERY_UNCONFIRMED',
                               letter_status='PROCESSING',
                               error_code='PERSONAL_CHAT_DELIVERY_UNCONFIRMED')
                    await persist_state(self.persist)
                    return
            import hashlib
            row['delivery_receipt'] = dict(
                attempt_id=row.get('generation_attempt_id'), input_revision=row.get('input_revision', 0),
                confirmed=True, text_sha256=hashlib.sha256(row['reply_text'].encode()).hexdigest())
            previous_revision = row.get('reply_revision')
            row.update(delivery_status="DELIVERED", letter_status="COMPLETED", reply_revision=1,
                       private_world_status="PENDING", daily_life_status="PENDING",
                       private_world_delivery_id=event.exchange_id + ":1",
                       private_world_occurred_at=datetime.now(timezone.utc).isoformat())
            rebind_expression_context(row, previous_revision=previous_revision)
            import hashlib
            row["private_world_reply_sha256"] = hashlib.sha256(row["reply_text"].encode()).hexdigest()
            row["private_world_semantic_key"] = "canonical." + hashlib.sha256(event.exchange_id.encode()).hexdigest()
            row.pop("error_code", None)
            await persist_state(self.persist)
            if event.channel == 'qq' and await self._after_delivery(row, event, send):
                await persist_state(self.persist)
            if (row.get('companion_decision') is None and row.get('proactive_decision') is None
                    and row.get('sticker_id') and not unprompted(row) and callable(getattr(send, 'image', None))):
                from pathlib import Path
                if (not re.fullmatch(r'linli-\d{2,3}', row['sticker_id'])
                        or (event.channel != 'qq' and int(row['sticker_id'][6:]) > 108)
                        or not self.sticker_allowed(row['sticker_id'])):
                    row['sticker_delivery_status'] = 'LOCKED'
                else:
                    row['sticker_delivery_status'] = 'DOWNLOADING'
                    await persist_state(self.persist)
                    try:
                        from runtime.letter_stickers.selection import asset_filename
                        path = (await self.sticker_asset(row['sticker_id']) if self.sticker_asset else
                                Path(__file__).resolve().parents[1] / 'letter_stickers' / asset_filename(row['sticker_id']))
                        row['sticker_delivery_status'] = 'SENDING'
                        await persist_state(self.persist)
                        await send.image(path)
                        row['sticker_delivery_status'] = 'DELIVERED'
                    except Exception:
                        row['sticker_delivery_status'] = ('FAILED' if row['sticker_delivery_status']=='DOWNLOADING' else 'UNKNOWN')
                await persist_state(self.persist)
            self._schedule_photo(row, send)
            from runtime.image_reply import is_companion_image
            # Only a photo the user asked for gets a failure notice; a casual
            # picture that did not come out is simply not sent.
            if event.channel == 'qq' and row.get('image_status') == 'FAILED' and is_companion_image(row):
                await photo_notice(row, send, self.persist, 'failure', photo_failure_text(row))
            self._schedule_commit(row)
            await asyncio.sleep(0)

    def _schedule_commit(self, row):
        key = row.get('letter_id')
        if row.get('delivery_status') != 'DELIVERED' or not isinstance(key, str) or not key:
            return
        pending = self.consumer_tasks.get(key)
        if pending is not None and not pending.done():
            return
        async def consume():
            try:
                await self.commit(row)  # Backend owns durable failures/backoff.
            except asyncio.CancelledError:
                raise
            except Exception:
                # Unexpected consumer/persistence failures remain observable and
                # recoverable; they must never undo an acknowledged delivery.
                row.setdefault('consumer_error_code', 'PERSONAL_CHAT_CONSUMER_UNAVAILABLE')
                await persist_state(self.persist)
        task = asyncio.create_task(consume())
        self.consumer_tasks[key] = task
        def finished(done):
            if self.consumer_tasks.get(key) is done:
                self.consumer_tasks.pop(key, None)
            if not done.cancelled():
                done.exception()  # Failure state remains on the delivered row.
        task.add_done_callback(finished)

    def _schedule_photo(self, row, send):
        self._schedule_speech(row, send)
        # Media runs after the text ACK and never holds the conversation lock.
        key = row['letter_id']
        from runtime.image_reply import is_companion_image
        primary_image = is_companion_image(row)
        from runtime.image_reply import secondary_photo_allowed
        if (not secondary_photo_allowed(row) or row.get('proactive_decision') is not None
                or not callable(self.photo) or row.get('channel') != 'qq' or not callable(getattr(send, 'image', None))
                or key in self.photo_tasks or row.get('image_delivery_status') in {'SENDING', 'UNKNOWN', 'DELIVERED'}
                or row.get('image_status') == 'FAILED' and not primary_image
                or row.get('image_status') == 'SKIPPED' and not primary_image
                and not (row.get('share_video_event') and row.get('share_video_status') != 'SHARE_QUEUED')):
            return
        async def deliver():
            try:
                await persist_state(self.persist)
                if callable(self.prepare_photo):
                    await self.prepare_photo(row, send)
                await self.photo(row, send)
                if primary_image and row.get('photo_with_words') is not True and row.get('delivery_status') == 'DELIVERED':
                    self._schedule_commit(row)  # a photo turn with words was committed with its line
                if (primary_image and row.get('image_status') in {'FAILED', 'SKIPPED'}
                        and row.get('photo_with_words') is not True):
                    # A photo turn that already said its line stays delivered; the notice below tells the user.
                    row.update(delivery_status='FAILED', letter_status='FAILED',
                               error_code='PERSONAL_CHAT_IMAGE_UNAVAILABLE')
                    await persist_state(self.persist)
                if primary_image and row.get('image_status') in {'FAILED', 'SKIPPED'}:
                    await photo_notice(row, send, self.persist, 'failure', photo_failure_text(row))
            except asyncio.CancelledError:
                raise
            except Exception:
                if row.get('image_delivery_status') in {'SENDING', 'UNKNOWN'}:
                    # Upload/ACK ambiguity is not generation failure. The picture
                    # may already be visible; never announce failure or resend it.
                    row['image_error_code'] = 'PERSONAL_CHAT_DELIVERY_UNCONFIRMED'
                    if primary_image and row.get('delivery_status') != 'DELIVERED':
                        row.update(delivery_status='DELIVERY_UNCONFIRMED', letter_status='PROCESSING',
                                   error_code='PERSONAL_CHAT_DELIVERY_UNCONFIRMED')
                    await persist_state(self.persist)
                    return
                row['image_error_code'] = 'PERSONAL_CHAT_IMAGE_UNAVAILABLE'
                if primary_image and row.get('delivery_status') != 'DELIVERED':
                    row.update(delivery_status=('SENDING' if row.get('image_delivery_status') in {'SENDING', 'UNKNOWN'} else 'FAILED'),
                               letter_status='FAILED', error_code='PERSONAL_CHAT_IMAGE_UNAVAILABLE')
                await persist_state(self.persist)
                if primary_image and row.get('image_delivery_status') != 'DELIVERED':
                    await photo_notice(row, send, self.persist, 'failure',
                        tr('照片没能确认发送成功，先告诉你一声。'))
        async def scheduled():
            try:
                async with self.photo_slots:
                    await deliver()
            finally:
                self.photo_tasks.pop(key, None)
        self.photo_tasks[key] = asyncio.create_task(scheduled())

    def resume_media(self, channel, send):
        if channel != 'qq' or channel not in self.bindings:
            return
        account, owner = self.bindings[channel]
        binding = PersonalMessage(channel, account, owner, '', '').binding_id
        from runtime.image_reply import is_companion_image
        for row in self.rows:
            video_fallback = (row.get('delivery_status') == 'DELIVERED' and row.get('share_video_event')
                              and row.get('share_video_status') != 'SHARE_QUEUED'
                              and row.get('image_status') != 'FAILED')
            if (row.get('channel') != channel or row.get('binding_id') != binding
                    or not (video_fallback or row.get('delivery_status') == 'MEDIA_PENDING' and is_companion_image(row))
                    or row.get('superseded_by') or row.get('letter_id') in self.photo_tasks
                    or row.get('image_delivery_status') in {'SENDING', 'UNKNOWN', 'DELIVERED'}):
                continue
            if row.get('image_status') in {'PLANNING', 'GENERATING', 'RETRY_PENDING'}:
                row['image_recovery_required'] = True
            if (row.get('image_generation_binding') or row.get('image_status') == 'GENERATING') and row.get('image_status') != 'COMPLETED':
                row['image_receipt_required'] = True
            sources = row.get('source_messages') or {}
            event = PersonalMessage(channel, account, owner, next(iter(sources), row['letter_id']),
                                    row.get('content', ''), tuple(sources.items()))
            correlated = send.for_exchange(event) if callable(getattr(send, 'for_exchange', None)) else send
            self._schedule_photo(row, correlated)

    def _schedule_speech(self,row,send):
        key=row['letter_id']
        binding=self.bindings.get('qq')
        if not binding or row.get('binding_id')!=PersonalMessage('qq',*binding,'','').binding_id:
            return
        if row.get('piano_input'):
            if (row.get('delivery_status') != 'DELIVERED' or row.get('channel') != 'qq'
                    or not callable(self.piano) or not callable(getattr(send, 'file', None))
                    or key in self.speech_tasks or row.get('piano_delivery_status') in {'SENDING', 'UNKNOWN'}
                    or row.get('piano_delivery_status') == 'DELIVERED' and row.get('piano_completed_at')
                       and row.get('piano_notice_status') in {'SENDING', 'UNKNOWN', 'DELIVERED'} and row.get('piano_cleanup_recorded')):
                return
            async def play():
                try:
                    await self.piano(row, send)
                    self._schedule_commit(row)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if row.get('piano_delivery_status') not in {'SENDING', 'UNKNOWN', 'DELIVERED'}:
                        row['piano_status'] = 'FAILED'
                        await persist_state(self.persist)
                        await delivery_notice(row, send, self.persist, 'piano_failure_notice',
                                              tr('这次钢琴录音暂时没能完成，请稍后再试。'))
                finally:
                    self.speech_tasks.pop(key, None)
            self.speech_tasks[key] = asyncio.create_task(play())
            return
        if (row.get('delivery_status') == 'DELIVERED' and row.get('channel') == 'qq'
                and row.get('default_voice_status') == 'PENDING' and callable(self.default_voice)
                and callable(getattr(send, 'audio', None)) and key not in self.speech_tasks):
            async def voice():
                try:
                    await self.default_voice(row, send)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # An attachment failure cannot undo or repeat acknowledged text.
                    row['default_voice_status'] = 'UNKNOWN'
                finally:
                    self.speech_tasks.pop(key, None)
            self.speech_tasks[key] = asyncio.create_task(voice())
            return
        if (row.get('delivery_status')!='DELIVERED' or row.get('channel')!='qq'
                or not row.get('speech_script') or not callable(self.speech)
                or not callable(getattr(send,'file',None)) or key in self.speech_tasks
                or row.get('speech_delivery_status') in {'SENDING','UNKNOWN','DELIVERED'}):
            return
        async def deliver():
            try:
                await self.speech(row,send)
            except asyncio.CancelledError:
                raise
            except Exception:
                if row.get('speech_delivery_status') not in {'SENDING','UNKNOWN','DELIVERED'}:
                    row['speech_status']='FAILED'
                    await persist_state(self.persist)
                    await delivery_notice(row,send,self.persist,'speech_failure_notice',
                        tr('这段音频暂时没能生成成功，稿子已经保留，稍后可以重试。'))
            finally:
                self.speech_tasks.pop(key,None)
        self.speech_tasks[key]=asyncio.create_task(deliver())

    def resume_speech(self, channel, send):
        for row in self.rows:
            if row.get('channel')==channel:
                self._schedule_speech(row,send)

    def _cancelled_earlier(self, row):
        """A reply that an earlier run cancelled while it was being written (an orderly shutdown)."""
        return (row.get('delivery_status') == 'FAILED' and row.get('error_code') == 'PERSONAL_CHAT_GENERATION_INTERRUPTED'
                and row.get('generation_interrupted') is True
                and bool(row.get('generation_run_id')) and row.get('generation_run_id') != self.run_id)

    def _resumable(self, row, now):
        """The owner's message got no reply because we stopped, it is recent, and nothing newer has been answered."""
        channel = row.get('channel')
        binding = self.bindings.get(channel)
        if (binding is None or row.get('origin') == 'proactive' or row.get('superseded_by')
                or row.get('generation_resumed_at') or row.get('send_attempt_id') or row.get('delivery_receipt')
                or not row.get('source_messages') or not isinstance(row.get('letter_id'), str)
                or row.get('binding_id') != PersonalMessage(channel, *binding, '', '').binding_id):
            return False
        try:
            if not -60 <= now - _boundary(row, 0)[0] <= RESUME_WINDOW_SECONDS:
                return False
            mine = _boundary(row, self.rows.index(row))
            return not any(other is not row and other.get('binding_id') == row.get('binding_id')
                           and other.get('delivery_status') in {'DELIVERED', 'SENDING', 'DELIVERY_UNCONFIRMED'}
                           and _boundary(other, index) > mine for index, other in enumerate(self.rows))
        except (TypeError, ValueError):
            return False

    def _requeue(self, row, now):
        """Queue the message for a fresh generation, once: the normal path generates, sends and records it."""
        _clear_draft(row)
        row.update(delivery_status='RECEIVED', letter_status='PROCESSING', generation_attempts=0,
                   input_revision=row.get('input_revision', 0) + 1, generation_resumed_at=now)
        row.pop('error_code', None)
        row.pop('generation_interrupted', None)
        return self._stored_event(row, row['channel'], *self.bindings[row['channel']])

    async def recover(self, now=None):
        """Recover local consumers; never initiate an outbound resend.

        A recent message whose reply was cut off before anything was sent is queued again, once. Returns the
        events queued again so that a transport that is already connected can take them at once."""
        now = datetime.now(timezone.utc).timestamp() if now is None else now
        resumed = []
        for row in list(self.rows):
            if row.get('delivery_status') == 'GENERATING' and row.get('generation_run_id') != self.run_id:
                receipt = row.get('delivery_receipt')
                receipt = receipt if isinstance(receipt, dict) else {}
                attempt = row.get('generation_attempt_id')
                matched = (attempt and receipt.get('attempt_id') == attempt
                           and receipt.get('input_revision') == row.get('input_revision', 0))
                text = row.get('reply_text')
                confirmed = (matched and receipt.get('confirmed') is True and isinstance(text, str)
                             and isinstance(row.get('letter_id'), str)
                             and receipt.get('text_sha256') == hashlib.sha256(text.encode()).hexdigest())
                if confirmed:
                    row.update(delivery_status='DELIVERED', letter_status='COMPLETED', reply_revision=1,
                               private_world_status='PENDING', daily_life_status='PENDING',
                               private_world_delivery_id=row['letter_id'] + ':1',
                               private_world_occurred_at=datetime.now(timezone.utc).isoformat(),
                               private_world_reply_sha256=receipt['text_sha256'],
                               private_world_semantic_key='canonical.' + hashlib.sha256(row['letter_id'].encode()).hexdigest())
                    row.pop('error_code', None)
                elif attempt and row.get('send_attempt_id') == attempt:
                    row.update(delivery_status='DELIVERY_UNCONFIRMED', letter_status='PROCESSING',
                               error_code='PERSONAL_CHAT_DELIVERY_UNCONFIRMED')
                elif self._resumable(row, now):
                    resumed.append(self._requeue(row, now))
                else:
                    retained = bool(self.inspect_writer(row)) if callable(self.inspect_writer) else False
                    row.update(delivery_status='FAILED', letter_status='FAILED',
                               error_code='PERSONAL_CHAT_GENERATION_INTERRUPTED', generation_retryable=False,
                               generation_interrupted=True,
                               generation_recovery='CANDIDATE_RETAINED' if retained else 'NO_CANDIDATE')
                await persist_state(self.persist)
            elif self._cancelled_earlier(row) and self._resumable(row, now):
                resumed.append(self._requeue(row, now))
                await persist_state(self.persist)
            self._schedule_commit(row)
            # Let a waiting message proceed between recovered exchanges.
            await asyncio.sleep(0)
        return tuple(resumed)
