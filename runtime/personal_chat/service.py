"""One owner, two transports, and the existing canonical exchange consumers."""
import asyncio
from datetime import datetime, timezone

from .events import PersonalMessage


class PersonalChatService:
    def __init__(self, rows, persist, generate, commit, bindings, *, sticker_allowed=lambda key: True):
        self.rows, self.persist = rows, persist
        self.generate, self.commit = generate, commit
        self.bindings = dict(bindings)
        self.sticker_allowed = sticker_allowed
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
                    next(iter(sources)), row['content'], tuple(sources.items()), row.get('input_kind','text'))
                terminal = row.get('delivery_status') == 'SENDING' or (
                    row.get('delivery_status') == 'FAILED' and row.get('generation_attempts', 0) >= 2)
                if not terminal or remaining.keys() == overlap:
                    await self._handle_one(stored, send.for_exchange(stored) if callable(getattr(send, 'for_exchange', None)) else send)
                for key in overlap:
                    del remaining[key]
            if remaining:
                fresh = combine([PersonalMessage(event.channel, event.account_id, event.owner_id, key, text, input_kind=event.input_kind)
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
                    await self.commit(row)
                    return
                if row.get("delivery_status") not in {"GENERATED", "FAILED", "GENERATING"}:
                    raise RuntimeError("PERSONAL_CHAT_DELIVERY_REQUIRES_ATTENTION")
            else:
                now = datetime.now(timezone.utc).isoformat()
                row = {"letter_id": event.exchange_id, "content": event.text,
                       "source_messages": dict(event.sources),
                       "binding_id": event.binding_id,
                       "input_kind": event.input_kind,
                       "channel": event.channel, "reply_mode": "future_im",
                       "life_received_at": now, "created_at": datetime.now(timezone.utc).timestamp(),
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
                except BaseException:
                    row.update(delivery_status="FAILED", letter_status="FAILED", error_code="PERSONAL_CHAT_GENERATION_FAILED")
                    self.persist()
                    raise
            if callable(getattr(send, 'is_available', None)) and not send.is_available():
                raise RuntimeError('PERSONAL_CHAT_CHANNEL_DISCONNECTED')
            audio = row.get('prepared_audio')
            if audio and callable(getattr(send, 'prepare_audio', None)):
                try:
                    audio = await send.prepare_audio(audio)
                except Exception:
                    audio = None
                    row['voice_fallback'] = 'PERSONAL_CHAT_AUDIO_UPLOAD_UNAVAILABLE'
            row["delivery_status"] = "SENDING"
            self.persist()
            try:
                if audio and callable(getattr(send, 'audio', None)):
                    await send.audio(audio)
                    row['delivered_format'] = 'audio'
                else:
                    await send(row["reply_text"])
                    row['delivered_format'] = 'text'
            except BaseException:
                row["error_code"] = "PERSONAL_CHAT_DELIVERY_UNKNOWN"
                self.persist()
                raise
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
                import re
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
            await self.commit(row)

    async def recover(self):
        """Recover local consumers only; never initiate an outbound resend."""
        async with self.lock:
            for row in self.rows:
                if row.get("delivery_status") == "DELIVERED":
                    await self.commit(row)
