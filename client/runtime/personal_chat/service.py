"""One owner, two transports, and the existing canonical exchange consumers."""
import asyncio
import re
from datetime import datetime, timezone

from .events import PersonalMessage


async def photo_notice(row, send, persist, kind, text):
    """At most one attempt per notice, including uncertain platform delivery."""
    key = 'image_' + kind + '_notice'
    if row.get(key):
        return
    row[key] = 'SENDING'
    persist()
    try:
        receipt = await send(text)
        row[key] = 'DELIVERED' if isinstance(receipt, (str, int)) and not isinstance(receipt, bool) and str(receipt) else 'UNKNOWN'
    except asyncio.CancelledError:
        row[key] = 'UNKNOWN'
        raise
    except Exception:
        row[key] = 'UNKNOWN'
    finally:
        persist()


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

    async def handle(self, event, send):
        from .events import combine
        if not isinstance(event, PersonalMessage) or self.bindings.get(event.channel) != (event.account_id, event.owner_id):
            raise ValueError('PERSONAL_CHAT_OWNER_MISMATCH')
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
        async with self.batch_lock:
            if not eligible():
                return
            await self._handle_one(event, send, proactive=True, followup=followup)

    async def _handle_one(self, event, send, proactive=False, followup=None):
        if not isinstance(event, PersonalMessage) or self.bindings.get(event.channel) != (event.account_id, event.owner_id):
            raise ValueError("PERSONAL_CHAT_OWNER_MISMATCH")
        if (not event.text.strip() and not proactive) or len(event.text) > 10000:
            raise ValueError("PERSONAL_CHAT_INPUT_INVALID")
        async with self.lock:
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
                    self.persist()
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
                self.persist()
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
                self.persist()
                try:
                    row['voice_available'] = callable(getattr(send, 'audio', None))
                    text = await self.generate(event, row)
                    if proactive and text.strip() == '[[skip]]':
                        row.update(delivery_status='SKIPPED', letter_status='SKIPPED')
                        self.persist()
                        return
                    if not isinstance(text, str) or not text.strip() or len(text) > 10000:
                        raise ValueError("PERSONAL_CHAT_REPLY_INVALID")
                    row.update(reply_text=text, delivery_status="GENERATED")
                    self.persist()
                except BaseException as exc:
                    code = str(exc)
                    if not re.fullmatch(r'(?:PERSONAL_CHAT|IMAGE|LLM)_[A-Z0-9_]{1,80}', code):
                        code = 'PERSONAL_CHAT_GENERATION_FAILED'
                    row.update(delivery_status="FAILED", letter_status="FAILED", error_code=code)
                    self.persist()
                    raise
            if callable(getattr(send, 'is_available', None)) and not send.is_available():
                raise RuntimeError('PERSONAL_CHAT_CHANNEL_DISCONNECTED')
            # Keep the stored canonical text identical to the displayed IM text.
            text = re.sub(r'。+[ \t]*', '\n', row['reply_text'])
            text = re.sub(r'(?<!\.)\.(?!\.)(?=\s|$)', '', text).strip()
            if not text:
                raise ValueError('PERSONAL_CHAT_REPLY_INVALID')
            if proactive:
                normalized = lambda value: re.sub(r'[\s。.]', '', value)
                cutoff = datetime.now(timezone.utc).timestamp() - 86400
                if any(old is not row and old.get('delivery_status') in {'DELIVERED', 'SENDING', 'DELIVERY_UNCONFIRMED'}
                       and float(old.get('created_at', 0)) >= cutoff
                       and normalized(old.get('reply_text', '')) == normalized(text)
                       for old in self.rows):
                    row.update(delivery_status='SKIPPED', letter_status='SKIPPED',
                               error_code='PERSONAL_CHAT_DUPLICATE_CONTENT')
                    self.persist()
                    return
            if event.channel == 'qq' and callable(self.prepare_photo) and callable(getattr(send, 'image', None)):
                await self.prepare_photo(row, send)
                if callable(getattr(send, 'is_available', None)) and not send.is_available():
                    raise RuntimeError('PERSONAL_CHAT_CHANNEL_DISCONNECTED')
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
            self.persist()
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
                self.persist()
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
                    self.persist()
                    return
            row.update(delivery_status="DELIVERED", letter_status="COMPLETED", reply_revision=1,
                       private_world_status="PENDING", daily_life_status="PENDING",
                       private_world_delivery_id=event.exchange_id + ":1",
                       private_world_occurred_at=datetime.now(timezone.utc).isoformat())
            import hashlib
            row["private_world_reply_sha256"] = hashlib.sha256(row["reply_text"].encode()).hexdigest()
            row["private_world_semantic_key"] = "canonical." + hashlib.sha256(event.exchange_id.encode()).hexdigest()
            row.pop("error_code", None)
            self.persist()
            if row.get('sticker_id') and callable(getattr(send, 'image', None)):
                from pathlib import Path
                if not re.fullmatch(r'linli-\d{2,3}', row['sticker_id']) or not self.sticker_allowed(row['sticker_id']):
                    row['sticker_delivery_status'] = 'LOCKED'
                else:
                    row['sticker_delivery_status'] = 'SENDING'
                    self.persist()
                    try:
                        await send.image(Path(__file__).resolve().parents[1] / 'letter_stickers' / (row['sticker_id'] + '.png'))
                        row['sticker_delivery_status'] = 'DELIVERED'
                    except Exception:
                        row['sticker_delivery_status'] = 'UNKNOWN'
                self.persist()
            self._schedule_photo(row, send)
            if event.channel == 'qq' and row.get('image_status') == 'FAILED':
                await photo_notice(row, send, self.persist, 'failure', '照片这次没生成成功，没能发给你。稍后再试一下。')
            await self.commit(row)

    def _schedule_photo(self, row, send):
        # New replies already prepared their photo; retain this recovery path for saved replies.
        key = row['letter_id']
        if (not callable(self.photo) or row.get('channel') != 'qq' or not callable(getattr(send, 'image', None))
                or key in self.photo_tasks or row.get('image_delivery_status') in {'SENDING', 'UNKNOWN', 'DELIVERED'}
                or row.get('image_status') in {'FAILED', 'SKIPPED'}):
            return
        async def deliver():
            try:
                await self.photo(row, send)
            except asyncio.CancelledError:
                raise
            except Exception:
                row['image_error_code'] = 'PERSONAL_CHAT_IMAGE_UNAVAILABLE'
                self.persist()
                if row.get('image_delivery_status') != 'DELIVERED':
                    await photo_notice(row, send, self.persist, 'failure',
                        '照片没能确认发送成功，先告诉你一声。')
            finally:
                self.photo_tasks.pop(key, None)
        self.photo_tasks[key] = asyncio.create_task(deliver())

    async def recover(self):
        """Recover local consumers only; never initiate an outbound resend."""
        async with self.lock:
            for row in self.rows:
                if row.get("delivery_status") == "DELIVERED":
                    await self.commit(row)
