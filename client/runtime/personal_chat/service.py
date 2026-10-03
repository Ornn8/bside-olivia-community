"""One owner, two transports, and the existing canonical exchange consumers."""
import asyncio
import re
import inspect
from contextvars import ContextVar
from datetime import datetime, timezone

from .events import PersonalMessage
from runtime.reply.character_emotion_context import rebind_expression_context
from runtime.reply.companion_decision import ERROR_CODES as _JEV_PORT_ERRORS
JEV_ERROR_CODES = _JEV_PORT_ERRORS | {
    'JEV_PLAN_UNSUPPORTED', 'JEV_CONTEXT_UNAVAILABLE', 'JEV_STORED_DECISION_INVALID',
    'JEV_SERVICE_UNAVAILABLE', 'JEV_CONTEXT_BUDGET_EXCEEDED', 'JEV_INPUT_SUPERSEDED',
    'JEV_DECISION_NOT_SAVED', 'JEV_CONFIG_INVALID',
}
TURN_IS_CURRENT = ContextVar('personal_chat_turn_is_current', default=None)


def _sent_time(row):
    try:
        value = datetime.fromisoformat((row.get('read_boundary_sent_at') or row['user_sent_at']).replace('Z', '+00:00'))
        return value if value.utcoffset() is not None else None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _clear_draft(row):
    for key in ('reply_text', 'prepared_audio', 'reply_audio_duration', 'voice_fallback', 'sticker_id',
                'letter_invitation', 'initiative_preference', 'pause_until', 'letter_preference',
                'letter_until', 'followup_at', 'followup_quote', 'listening_preference', 'requested_format',
                'presentation_status', 'delivery_basis', 'voice_ready', 'quality_status', 'reviewer_calls', 'rewrite_calls',
                'mailbox_notice_letter_id', 'decision_rejection_reason', 'semantic_shadow', 'expression_context',
                'companion_decision', 'companion_timing', 'companion_delivery',
                'speech_script', 'speech_intent', 'speech_status', 'speech_delivery_status', 'content_review',
                'proactive_decision', 'proactive_basis', 'proactive_opportunity', 'generation_failure_notice',
                'generation_context_at', 'stage_timing_seconds', 'stage_actual_calls', 'stage_cache_hits',
                'voice_prepare_seconds', 'voice_prepare_status', 'voice_prepare_timeout_seconds'):
        row.pop(key, None)


async def persist_state(persist):
    result = persist()
    if inspect.isawaitable(result):
        await result


async def photo_notice(row, send, persist, kind, text):
    await delivery_notice(row, send, persist, 'image_' + kind + '_notice', text)


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
    def __init__(self, rows, persist, generate, commit, bindings, *, sticker_allowed=lambda key: True, photo=None, prepare_photo=None, speech=None):
        self.rows, self.persist = rows, persist
        self.generate, self.commit = generate, commit
        self.bindings = dict(bindings)
        self.sticker_allowed = sticker_allowed
        self.photo = photo
        self.prepare_photo = prepare_photo
        self.photo_tasks = {}
        self.speech = speech
        self.speech_tasks = {}
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
            tuple(tuple(item) for item in row.get('incoming_images', [])))

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
                    images=tuple(item for item in event.images if item[0] == key))
                    for key, text in remaining.items()])
                now = datetime.now(timezone.utc)
                self.user_revision += 1
                self.rows.append({'letter_id': incoming.exchange_id, 'content': incoming.text,
                    'source_messages': dict(incoming.sources), 'binding_id': incoming.binding_id,
                    'input_kind': incoming.input_kind, 'incoming_images': [list(item) for item in incoming.images],
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
        """Only never-started receipts can be resumed automatically on reconnect."""
        binding = self.bindings.get(channel)
        if binding is None:
            return ()
        account_id, owner_id = binding
        binding_id = PersonalMessage(channel, account_id, owner_id, '', '').binding_id
        rows = [row for row in self.rows if row.get('channel') == channel
                and row.get('binding_id') == binding_id and row.get('delivery_status') == 'RECEIVED'
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
        if len(merged.images) > 4:
            raise ValueError('PERSONAL_CHAT_INPUT_BATCH_TOO_LARGE')
        canonical = rows[0]
        if canonical.get('incoming_images') != [list(item) for item in merged.images]:
            canonical.pop('incoming_image_status', None)
        latest = max(rows, key=lambda row: row.get('read_boundary_sequence', row.get('received_sequence', 0)))
        sent_at = max((at for row in rows if (at := _sent_time(row)) is not None), default=None)
        canonical.update(content=merged.text, source_messages=dict(merged.sources), input_kind=merged.input_kind,
                         incoming_images=[list(item) for item in merged.images],
                         read_boundary_created_at=latest.get('read_boundary_created_at', latest['created_at']),
                         read_boundary_sequence=latest.get('read_boundary_sequence', latest['received_sequence']),
                         read_boundary_sent_at=sent_at.isoformat() if sent_at else None)
        for row in rows[1:]:
            row.update(delivery_status='SKIPPED', letter_status='SKIPPED', superseded_by=canonical['letter_id'])
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
        current = TURN_IS_CURRENT.set(lambda: not self._new_inputs(row))
        try:
            return await self.generate(event, row)
        finally:
            TURN_IS_CURRENT.reset(current)

    async def handle(self, event, send):
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

    async def notify_generation_failure(self, event, send):
        """One system notice per exhausted input; never publish an unchecked draft."""
        sources = dict(event.sources).keys()
        for row in reversed(self.rows):
            if (row.get('binding_id') != event.binding_id or row.get('origin') == 'proactive'
                    or row.get('superseded_by') or row.get('delivery_status') != 'FAILED'
                    or row.get('generation_attempts', 0) < 2
                    or not sources & row.get('source_messages', {}).keys()):
                continue
            correlated = send.for_exchange(event) if callable(getattr(send, 'for_exchange', None)) else send
            await delivery_notice(row, correlated, self.persist, 'generation_failure_notice',
                '【系统提示】这条消息的回复生成失败，暂时没能回复。可以稍后重新发一下。')
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
                tuple(tuple(item) for item in row.get('incoming_images', [])))
            terminal = row.get('delivery_status') in {'SENDING', 'DELIVERY_UNCONFIRMED'} or (
                row.get('delivery_status') == 'FAILED' and row.get('generation_attempts', 0) >= 2)
            if not terminal or remaining.keys() == overlap:
                updated = await self._handle_one(stored, send.for_exchange(stored) if callable(getattr(send, 'for_exchange', None)) else send)
                if updated is not None:
                    return updated
            for key in overlap:
                del remaining[key]
        if remaining:
            fresh = combine([PersonalMessage(event.channel, event.account_id, event.owner_id, key, text, input_kind=event.input_kind, sent_at=event.sent_at,
                                            images=tuple(item for item in event.images if item[0] == key))
                             for key, text in remaining.items()])
            return await self._handle_one(fresh, send.for_exchange(fresh) if callable(getattr(send, 'for_exchange', None)) else send)

    async def proactive(self, event, send, eligible=lambda: True, followup=None):
        revision = self.user_revision
        async with self.batch_lock:
            if not eligible() or revision != self.user_revision:
                return
            await self._handle_one(event, send, proactive=True, followup=followup,
                                   proactive_revision=revision, proactive_eligible=eligible)

    async def _handle_one(self, event, send, proactive=False, followup=None, proactive_revision=None,
                          proactive_eligible=None):
        if not isinstance(event, PersonalMessage) or self.bindings.get(event.channel) != (event.account_id, event.owner_id):
            raise ValueError("PERSONAL_CHAT_OWNER_MISMATCH")
        if (not event.text.strip() and not proactive) or len(event.text) > 10000:
            raise ValueError("PERSONAL_CHAT_INPUT_INVALID")
        async with self.lock:
            if proactive and proactive_revision != self.user_revision:
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
                def boundary(item, index):
                    return (float(item.get('read_boundary_created_at', item.get('created_at', 0))),
                            item.get('read_boundary_sequence', item.get('received_sequence', index)))
                if row.get('delivery_status') in {'RECEIVED', 'GENERATED', 'FAILED', 'GENERATING'} and any(
                    old is not row and old.get('delivery_status') == 'DELIVERED'
                    and boundary(old, index) > boundary(row, position)
                    for index, old in enumerate(self.rows)
                ):
                    # Replays include failed or interrupted generation, not just
                    # unsent drafts. Never answer an old turn after a newer reply.
                    row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                               error_code='PERSONAL_CHAT_STALE_REPLY')
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
                       "channel": event.channel, "reply_mode": "future_im",
                       "life_received_at": now, "created_at": datetime.now(timezone.utc).timestamp(),
                       "user_sent_at": event.sent_at,
                       "delivery_status": "GENERATING", "letter_status": "PROCESSING"}
                if proactive:
                    row.update(origin='proactive', source_messages={})
                    if followup:
                        row.update(followup_source_id=followup['letter_id'], followup_quote=followup['followup_quote'])
                self.rows.append(row)
                await persist_state(self.persist)
            if not proactive and _sent_time(row):
                # Platform backlog can arrive for the first time after a newer
                # turn was answered. Arrival/row order cannot detect that case.
                for old in self.rows:
                    if (old is row or old.get('delivery_status') != 'DELIVERED'
                            or old.get('binding_id') != event.binding_id):
                        continue
                    try:
                        before = _sent_time(row)
                        after = _sent_time(old)
                    except (KeyError, AttributeError, TypeError, ValueError):
                        continue
                    if before is not None and after is not None and after > before:
                        row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                                   error_code='PERSONAL_CHAT_STALE_REPLY')
                        await persist_state(self.persist)
                        return
            if row.get("delivery_status") != "GENERATED":
                if row.get("generation_attempts", 0) >= 2:
                    raise RuntimeError("PERSONAL_CHAT_GENERATION_RETRY_EXHAUSTED")
                for key in ('prepared_audio', 'reply_audio_duration', 'voice_fallback', 'sticker_id',
                            'letter_invitation', 'initiative_preference', 'pause_until', 'letter_preference',
                            'letter_until', 'followup_at', 'listening_preference', 'requested_format', 'presentation_status',
                            'delivery_basis', 'voice_ready',
                            'expression_context', 'companion_timing', 'companion_delivery',
                            'voice_prepare_seconds', 'voice_prepare_status', 'voice_prepare_timeout_seconds'):
                    row.pop(key, None)
                if not proactive:
                    row.pop('followup_quote', None)
                row["generation_attempts"] = row.get("generation_attempts", 0) + 1
                row.update(delivery_status="GENERATING", letter_status="PROCESSING")
                await persist_state(self.persist)
                try:
                    row['voice_available'] = callable(getattr(send, 'audio', None))
                    row['image_available'] = callable(self.photo) and callable(getattr(send, 'image', None))
                    if proactive:
                        if proactive_revision != self.user_revision:
                            row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                                       error_code='PERSONAL_CHAT_USER_PRIORITY')
                            await persist_state(self.persist)
                            return
                        task = asyncio.create_task(self._generate(event, row, send))
                        self.proactive_generation = task
                        try:
                            text = await task
                        except asyncio.CancelledError:
                            if asyncio.current_task().cancelling():
                                raise
                            row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                                       error_code='PERSONAL_CHAT_USER_PRIORITY')
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
                            if text is None and row.get('companion_timing') in {'wait_user', 'defer', 'no_reply'}:
                                row.update(delivery_status='SKIPPED', letter_status='SKIPPED')
                                await persist_state(self.persist)
                                return
                    if proactive and text.strip() == '[[skip]]':
                        row.update(delivery_status='SKIPPED', letter_status='SKIPPED')
                        await persist_state(self.persist)
                        return
                    if not isinstance(text, str) or not text.strip() or len(text) > 10000:
                        raise ValueError("PERSONAL_CHAT_REPLY_INVALID")
                    row.update(reply_text=text, delivery_status="GENERATED")
                    rebind_expression_context(row)
                    await persist_state(self.persist)
                except BaseException as exc:
                    code = str(exc)
                    if code not in JEV_ERROR_CODES and not re.fullmatch(r'(?:PERSONAL_CHAT|IMAGE|LLM)_[A-Z0-9_]{1,80}', code):
                        code = 'PERSONAL_CHAT_GENERATION_FAILED'
                    row.update(delivery_status="FAILED", letter_status="FAILED", error_code=code)
                    await persist_state(self.persist)
                    raise
            if callable(getattr(send, 'is_available', None)) and not send.is_available():
                raise RuntimeError('PERSONAL_CHAT_CHANNEL_DISCONNECTED')
            if not proactive:
                async with self.intake_lock:
                    updated = await self._refresh_draft(row, event)
                    if updated is not None:
                        return updated
            if row.get('companion_delivery') == 'image':
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
            text = re.sub(r'。+[ \t]*', '\n', row['reply_text'])
            text = re.sub(r'(?<!\.)\.(?!\.)(?=\s|$)', '', text).strip()
            if not text:
                raise ValueError('PERSONAL_CHAT_REPLY_INVALID')
            if proactive:
                if proactive_revision != self.user_revision:
                    row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                               error_code='PERSONAL_CHAT_USER_PRIORITY')
                    await persist_state(self.persist)
                    return
                normalized = lambda value: re.sub(r'[\s。.]', '', value)
                cutoff = datetime.now(timezone.utc).timestamp() - 86400
                if any(old is not row and old.get('delivery_status') in {'DELIVERED', 'SENDING', 'DELIVERY_UNCONFIRMED'}
                       and float(old.get('created_at', 0)) >= cutoff
                       and normalized(old.get('reply_text', '')) == normalized(text)
                       for old in self.rows):
                    row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                               error_code='PERSONAL_CHAT_DUPLICATE_CONTENT')
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
                if not file_speech and (row.get('companion_decision') is not None and row.get('companion_delivery') == 'audio_speech'
                        or row.get('proactive_decision', {}).get('decision', {}).get('medium') == 'audio_speech'):
                    row.update(delivery_status='FAILED', letter_status='FAILED', error_code='JEV_PLAN_UNSUPPORTED')
                    await persist_state(self.persist)
                    raise RuntimeError('JEV_PLAN_UNSUPPORTED')
                previous_text = row['reply_text']
                row['reply_text'] = text
                rebind_expression_context(row, previous_text=previous_text)
            async with self.intake_lock:
                if not proactive:
                    updated = await self._refresh_draft(row, event)
                    if updated is not None:
                        return updated
                elif proactive_revision != self.user_revision:
                    row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                               error_code='PERSONAL_CHAT_USER_PRIORITY')
                    await persist_state(self.persist)
                    return
                if proactive and callable(proactive_eligible) and not proactive_eligible():
                    row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                               error_code='PERSONAL_CHAT_CONTACT_SUPERSEDED')
                    await persist_state(self.persist)
                    return
                row["delivery_status"] = "SENDING"
                await persist_state(self.persist)
            if proactive and (proactive_revision != self.user_revision
                              or callable(proactive_eligible) and not proactive_eligible()):
                row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                           error_code=('PERSONAL_CHAT_USER_PRIORITY' if proactive_revision != self.user_revision
                                       else 'PERSONAL_CHAT_CONTACT_SUPERSEDED'))
                await persist_state(self.persist)
                return
            receipt = None
            try:
                if audio and callable(getattr(send, 'audio', None)):
                    receipt = await send.audio(audio)
                    row['delivered_format'] = 'audio'
                else:
                    receipt = await send(row["reply_text"])
                    row['delivered_format'] = 'text'
            except BaseException:
                row["error_code"] = "PERSONAL_CHAT_DELIVERY_UNKNOWN"
                await persist_state(self.persist)
                raise
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
            if (row.get('companion_decision') is None and row.get('proactive_decision') is None
                    and row.get('sticker_id') and callable(getattr(send, 'image', None))):
                from pathlib import Path
                if (not re.fullmatch(r'linli-\d{2,3}', row['sticker_id'])
                        or (event.channel != 'qq' and int(row['sticker_id'][6:]) > 108)
                        or not self.sticker_allowed(row['sticker_id'])):
                    row['sticker_delivery_status'] = 'LOCKED'
                else:
                    row['sticker_delivery_status'] = 'SENDING'
                    await persist_state(self.persist)
                    try:
                        from runtime.letter_stickers.selection import asset_filename
                        await send.image(Path(__file__).resolve().parents[1] / 'letter_stickers' /
                                         asset_filename(row['sticker_id']))
                        row['sticker_delivery_status'] = 'DELIVERED'
                    except Exception:
                        row['sticker_delivery_status'] = 'UNKNOWN'
                await persist_state(self.persist)
            self._schedule_photo(row, send)
            from runtime.image_reply import is_companion_image
            # Only a photo the user asked for gets a failure notice; a casual
            # picture that did not come out is simply not sent.
            if event.channel == 'qq' and row.get('image_status') == 'FAILED' and is_companion_image(row):
                await photo_notice(row, send, self.persist, 'failure', '照片这次没生成成功，没能发给你。稍后再试一下。')
            self._schedule_commit(row)
            await asyncio.sleep(0)

    def _schedule_commit(self, row):
        key = row['letter_id']
        pending = self.consumer_tasks.get(key)
        if row.get('delivery_status') != 'DELIVERED' or pending is not None and not pending.done():
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
                or row.get('image_status') in {'FAILED', 'SKIPPED'}):
            return
        async def deliver():
            try:
                if callable(self.prepare_photo):
                    await self.prepare_photo(row, send)
                await self.photo(row, send)
                if primary_image and row.get('delivery_status') == 'DELIVERED':
                    self._schedule_commit(row)
                if primary_image and row.get('image_status') in {'FAILED', 'SKIPPED'}:
                    row.update(delivery_status='FAILED', letter_status='FAILED',
                               error_code='PERSONAL_CHAT_IMAGE_UNAVAILABLE')
                    await persist_state(self.persist)
                if primary_image and row.get('image_status') in {'FAILED', 'SKIPPED'}:
                    await photo_notice(row, send, self.persist, 'failure',
                        '照片这次没生成成功，没能发给你。稍后再试一下。')
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
                        '照片没能确认发送成功，先告诉你一声。')
            finally:
                self.photo_tasks.pop(key, None)
        self.photo_tasks[key] = asyncio.create_task(deliver())

    def _schedule_speech(self,row,send):
        key=row['letter_id']
        binding=self.bindings.get('qq')
        if not binding or row.get('binding_id')!=PersonalMessage('qq',*binding,'','').binding_id:
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
                        '这段音频暂时没能生成成功，稿子已经保留，稍后可以重试。')
            finally:
                self.speech_tasks.pop(key,None)
        self.speech_tasks[key]=asyncio.create_task(deliver())

    def resume_speech(self, channel, send):
        for row in self.rows:
            if row.get('channel')==channel:
                self._schedule_speech(row,send)

    async def recover(self):
        """Recover local consumers only; never initiate an outbound resend."""
        for row in list(self.rows):
            self._schedule_commit(row)
            # Let a waiting message proceed between recovered exchanges.
            await asyncio.sleep(0)
