# Shared reply context

User-facing generation shares `assemble_reply_messages` in `reply_pipeline.py`.
Memory retrieval, paired original evidence, recent correspondence, relationship
projection, trusted time and daily world state must not be reimplemented with
channel-specific limits.

- Text letters, proactive letters, voice replies, spoken video and musical reply
  modes enter through `ReplyPipeline._prepare_generation_request`.
- QQ and WeChat (ordinary and proactive) use the same pipeline with the IM
  presentation context. Timestamps and the current-source exclusion remain intact.
- Direct adapter generation delegates through `LetterAdapter.reply_context_messages`.
- Original-song lyrics receive the product adapter through the media renderer and
  use the same context assembly. The standalone planner fallback is for library
  use without a running product; product callers must pass the adapter.
- Review and rewrite consume the assembled generation evidence. Only historical
  character-reply fragments actually included in the prompt become typed review
  evidence.

Channels may add output-format and media constraints. They must not silently use a
different memory, relationship or world snapshot provider. Preserve request limits,
pause/forget behavior and per-user/source isolation.

Knowledge boundaries, conversational intent and correction behavior are shared in
the required `character_participation` block in `persona_assembly.py`. Do not add
channel-specific expertise or defensive-persona rules. The cross-mode contract is
covered by `tests/persona/test_shared_participation.py`.

Media rendering projects the canonical reply. It must not invent a second exchange
or independently advance relationship state. Delivery consumers retain their
existing idempotent world, life and memory commits.

Regression entry point: `tests/memory/test_multitopic_original_recall.py`, including
all seven reply modes, both social channels and their proactive variants, and the
song planner. Test final provider/reviewer input, not just the search API.
