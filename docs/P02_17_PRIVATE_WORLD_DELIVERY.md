# P02-17 canonical reply PrivateWorld commit

The only relationship delivery point is a quality-gate accepted canonical reply
persisted with `reply_revision`. Its stable `delivery_id` is
`letter_id:reply_revision`; the SQLite ledger applies it once.

`DeliveryEvent` accepts only `CANONICAL_REPLY_DELIVERED` and has no stage,
intimacy, nickname, home-access, continuation, boundary, or affection mutation
payload. It retains this single responsibility. Character relationship facts
use the dedicated typed `RelationshipFactCommand` and
`PrivateWorldRelationshipCommitter`; canonical delivery cannot act as an
authorization adapter.

The current production status is **typed contract + trusted internal entry
ready**. Automatic reply integration is not enabled: reply generation and LLM
candidate analysis do not submit relationship facts. A future caller may commit
one only after an explicit trusted approval step supplies the typed command and
verifiable canonical delivery evidence.

The letter is first stored with `private_world_status=PENDING`, then committed.
Startup recovery retries pending records without changing or deleting canonical
text. An unavailable backend leaves the reply intact and records only a stable
error code. Generation failure, quality block, request retry, TTS/video/music
failure, and media rerender never call the commit path.

SQLite read/write errors and semantic snapshot failures (including malformed
JSON, missing required fields, or invalid bounded values) are backend failures,
not invalid delivery events. They leave the canonical reply as
`private_world_status=PENDING` with `PRIVATE_WORLD_UNAVAILABLE`, so a later
startup recovery may safely retry the same delivery id.
