"""One owner, two transports, and the existing canonical exchange consumers."""
import asyncio
import re
import inspect
from contextvars import ContextVar
from datetime import datetime, timezone

from .events import PersonalMessage
QUEUE_NOTICE = ContextVar('personal_chat_queue_notice', default=None)


def queue_notice_text(rows):
    from .initiative_profile import profile_from_rows
    profile = profile_from_rows(rows)
    if profile.caution != 'normal':
        return '我这边还要等一会儿，晚点再回复你，你先忙自己的就好。'
    return {
        'reserved': '我这边还需要等一会儿，稍后再回复你。',
        'familiar': '我这边还得等一会儿，等好了再跟你聊。',
        'trusted': '稍等我一会儿，等好了我就来找你聊，你先忙。',
        'close': '等我一会儿呀，等好了就回来陪你聊，不用一直守着。',
        'committed': '让我等这一会儿，等好了就回来陪你。你先做自己的事，别一直等着呀。',
    }[profile.tier]


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
    def __init__(self, rows, persist, generate, commit, bindings, *, sticker_allowed=lambda key: True, photo=None, prepare_photo=None):
        self.rows, self.persist = rows, persist
        self.generate, self.commit = generate, commit
        self.bindings = dict(bindings)
        self.sticker_allowed = sticker_allowed
        self.photo = photo
        self.prepare_photo = prepare_photo
        self.photo_tasks = {}
        # One owner shares memory/world across both channels; serialize exchanges.
        self.lock = asyncio.Lock()
        self.batch_lock = asyncio.Lock()
        self.proactive_generation = None
        self.user_revision = 0

    async def _generate(self, event, row, send):
        async def queued():
            if event.channel == 'qq' and row.get('origin') != 'proactive':
                await delivery_notice(row, send, self.persist, 'queue_notice',
                    queue_notice_text(self.rows))
        token = QUEUE_NOTICE.set(queued)
        try:
            return await self.generate(event, row)
        finally:
            QUEUE_NOTICE.reset(token)

    async def handle(self, event, send):
        from .events import combine
        if not isinstance(event, PersonalMessage) or self.bindings.get(event.channel) != (event.account_id, event.owner_id):
            raise ValueError('PERSONAL_CHAT_OWNER_MISMATCH')
        self.user_revision += 1
        if self.proactive_generation is not None:
            self.proactive_generation.cancel()
        # A platform replay can regroup a previously delivered burst differently.
        # Resolve original IDs before choosing a new canonical exchange identity.
        async with self.batch_lock:
            remaining = dict(event.sources)
            for row in list(self.rows):
                if row.get('channel') != event.channel or row.get('binding_id', event.binding_id) != event.binding_id:
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
                    await self._handle_one(stored, send.for_exchange(stored) if callable(getattr(send, 'for_exchange', None)) else send)
                for key in overlap:
                    del remaining[key]
            if remaining:
                fresh = combine([PersonalMessage(event.channel, event.account_id, event.owner_id, key, text, input_kind=event.input_kind, sent_at=event.sent_at,
                                                images=tuple(item for item in event.images if item[0] == key))
                                 for key, text in remaining.items()])
                await self._handle_one(fresh, send.for_exchange(fresh) if callable(getattr(send, 'for_exchange', None)) else send)

    async def proactive(self, event, send, eligible=lambda: True, followup=None):
        revision = self.user_revision
        async with self.batch_lock:
            if not eligible() or revision != self.user_revision:
                return
            await self._handle_one(event, send, proactive=True, followup=followup, proactive_revision=revision)

    async def _handle_one(self, event, send, proactive=False, followup=None, proactive_revision=None):
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
                    await self.commit(row)
                    return
                if row.get('delivery_status') == 'SKIPPED':
                    return
                position = self.rows.index(row)
                if row.get('delivery_status') == 'GENERATED' and any(
                    old is not row and old.get('delivery_status') == 'DELIVERED'
                    and (float(old.get('created_at', 0)), index)
                        > (float(row.get('created_at', 0)), position)
                    for index, old in enumerate(self.rows)
                ):
                    # A disconnected channel can replay an unsent old draft
                    # after this owner's conversation has moved on elsewhere.
                    row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                               error_code='PERSONAL_CHAT_STALE_REPLY')
                    await persist_state(self.persist)
                    return
                if row.get("delivery_status") not in {"GENERATED", "FAILED", "GENERATING"}:
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
            if row.get("delivery_status") != "GENERATED":
                if row.get("generation_attempts", 0) >= 2:
                    raise RuntimeError("PERSONAL_CHAT_GENERATION_RETRY_EXHAUSTED")
                for key in ('prepared_audio', 'reply_audio_duration', 'voice_fallback', 'sticker_id',
                            'letter_invitation', 'initiative_preference', 'pause_until', 'letter_preference',
                            'letter_until', 'followup_at', 'listening_preference', 'requested_format', 'presentation_status'):
                    row.pop(key, None)
                if not proactive:
                    row.pop('followup_quote', None)
                row["generation_attempts"] = row.get("generation_attempts", 0) + 1
                row.update(delivery_status="GENERATING", letter_status="PROCESSING")
                await persist_state(self.persist)
                try:
                    row['voice_available'] = callable(getattr(send, 'audio', None))
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
                    if proactive and text.strip() == '[[skip]]':
                        row.update(delivery_status='SKIPPED', letter_status='SKIPPED')
                        await persist_state(self.persist)
                        return
                    if not isinstance(text, str) or not text.strip() or len(text) > 10000:
                        raise ValueError("PERSONAL_CHAT_REPLY_INVALID")
                    row.update(reply_text=text, delivery_status="GENERATED")
                    await persist_state(self.persist)
                except BaseException as exc:
                    code = str(exc)
                    if not re.fullmatch(r'(?:PERSONAL_CHAT|IMAGE|LLM)_[A-Z0-9_]{1,80}', code):
                        code = 'PERSONAL_CHAT_GENERATION_FAILED'
                    row.update(delivery_status="FAILED", letter_status="FAILED", error_code=code)
                    await persist_state(self.persist)
                    raise
            if callable(getattr(send, 'is_available', None)) and not send.is_available():
                raise RuntimeError('PERSONAL_CHAT_CHANNEL_DISCONNECTED')
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
                row['reply_text'] = text
            row["delivery_status"] = "SENDING"
            await persist_state(self.persist)
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
            row.update(delivery_status="DELIVERED", letter_status="COMPLETED", reply_revision=1,
                       private_world_status="PENDING", daily_life_status="PENDING",
                       private_world_delivery_id=event.exchange_id + ":1",
                       private_world_occurred_at=datetime.now(timezone.utc).isoformat())
            import hashlib
            row["private_world_reply_sha256"] = hashlib.sha256(row["reply_text"].encode()).hexdigest()
            row["private_world_semantic_key"] = "canonical." + hashlib.sha256(event.exchange_id.encode()).hexdigest()
            row.pop("error_code", None)
            await persist_state(self.persist)
            if row.get('sticker_id') and callable(getattr(send, 'image', None)):
                from pathlib import Path
                if not re.fullmatch(r'linli-\d{2,3}', row['sticker_id']) or not self.sticker_allowed(row['sticker_id']):
                    row['sticker_delivery_status'] = 'LOCKED'
                else:
                    row['sticker_delivery_status'] = 'SENDING'
                    await persist_state(self.persist)
                    try:
                        await send.image(Path(__file__).resolve().parents[1] / 'letter_stickers' / (row['sticker_id'] + '.png'))
                        row['sticker_delivery_status'] = 'DELIVERED'
                    except Exception:
                        row['sticker_delivery_status'] = 'UNKNOWN'
                await persist_state(self.persist)
            self._schedule_photo(row, send)
            if event.channel == 'qq' and row.get('image_status') == 'FAILED':
                await photo_notice(row, send, self.persist, 'failure', '照片这次没生成成功，没能发给你。稍后再试一下。')
            await self.commit(row)

    def _schedule_photo(self, row, send):
        # Media runs after the text ACK and never holds the conversation lock.
        key = row['letter_id']
        if (not callable(self.photo) or row.get('channel') != 'qq' or not callable(getattr(send, 'image', None))
                or key in self.photo_tasks or row.get('image_delivery_status') in {'SENDING', 'UNKNOWN', 'DELIVERED'}
                or row.get('image_status') in {'FAILED', 'SKIPPED'}):
            return
        async def deliver():
            try:
                if callable(self.prepare_photo):
                    await self.prepare_photo(row, send)
                await self.photo(row, send)
                if row.get('image_status') == 'FAILED':
                    await photo_notice(row, send, self.persist, 'failure',
                        '照片这次没生成成功，没能发给你。稍后再试一下。')
            except asyncio.CancelledError:
                raise
            except Exception:
                row['image_error_code'] = 'PERSONAL_CHAT_IMAGE_UNAVAILABLE'
                await persist_state(self.persist)
                if row.get('image_delivery_status') != 'DELIVERED':
                    await photo_notice(row, send, self.persist, 'failure',
                        '照片没能确认发送成功，先告诉你一声。')
            finally:
                self.photo_tasks.pop(key, None)
        self.photo_tasks[key] = asyncio.create_task(deliver())

    async def recover(self):
        """Recover local consumers only; never initiate an outbound resend."""
        for row in list(self.rows):
            async with self.lock:
                if row.get("delivery_status") == "DELIVERED":
                    await self.commit(row)
            # Let a waiting message proceed between recovered exchanges.
            await asyncio.sleep(0)
