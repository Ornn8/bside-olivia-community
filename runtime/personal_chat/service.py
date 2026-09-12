"""One owner, two transports, and the existing canonical exchange consumers."""
import asyncio
from datetime import datetime, timezone

from .events import PersonalMessage


class PersonalChatService:
    def __init__(self, rows, persist, generate, commit, bindings):
        self.rows, self.persist = rows, persist
        self.generate, self.commit = generate, commit
        self.bindings = dict(bindings)
        # One owner shares memory/world across both channels; serialize exchanges.
        self.lock = asyncio.Lock()

    async def handle(self, event, send):
        if not isinstance(event, PersonalMessage) or self.bindings.get(event.channel) != (event.account_id, event.owner_id):
            raise ValueError("PERSONAL_CHAT_OWNER_MISMATCH")
        if not event.text.strip() or len(event.text) > 10000:
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
                       "channel": event.channel, "reply_mode": "future_im",
                       "life_received_at": now, "created_at": datetime.now(timezone.utc).timestamp(),
                       "delivery_status": "GENERATING", "letter_status": "PROCESSING"}
                self.rows.append(row)
                self.persist()
            if row.get("delivery_status") != "GENERATED":
                if row.get("generation_attempts", 0) >= 2:
                    raise RuntimeError("PERSONAL_CHAT_GENERATION_RETRY_EXHAUSTED")
                row["generation_attempts"] = row.get("generation_attempts", 0) + 1
                row.update(delivery_status="GENERATING", letter_status="PROCESSING")
                self.persist()
                try:
                    text = await self.generate(event, row)
                    if not isinstance(text, str) or not text.strip() or len(text) > 10000:
                        raise ValueError("PERSONAL_CHAT_REPLY_INVALID")
                    row.update(reply_text=text, delivery_status="GENERATED")
                    self.persist()
                except BaseException:
                    row.update(delivery_status="FAILED", letter_status="FAILED", error_code="PERSONAL_CHAT_GENERATION_FAILED")
                    self.persist()
                    raise
            row["delivery_status"] = "SENDING"
            self.persist()
            try:
                await send(row["reply_text"])
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
            await self.commit(row)

    async def recover(self):
        """Recover local consumers only; never initiate an outbound resend."""
        async with self.lock:
            for row in self.rows:
                if row.get("delivery_status") == "DELIVERED":
                    await self.commit(row)
