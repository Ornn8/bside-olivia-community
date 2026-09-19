from datetime import datetime, timezone

from persona_assembly import assemble_persona
from persona_loader import PersonaSnapshot
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime


def test_non_ready_writer_does_not_claim_linli_identity() -> None:
    context = ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 9, 10, tzinfo=timezone.utc)),
    )

    for status, source in (("POLICY_ONLY", "persona_v2"), ("DRAFT", "draft")):
        snapshot = PersonaSnapshot(
            schema_version="p02.persona.v2",
            persona_id="synthetic.policy",
            declarations=(),
            status=status,
            source=source,
        )
        assembled = assemble_persona(
            snapshot,
            context,
            user_input="Synthetic input.",
            max_units=4_000,
        )

        assert "Answer as Linli, not as a service agent or therapist." not in assembled.system_content
