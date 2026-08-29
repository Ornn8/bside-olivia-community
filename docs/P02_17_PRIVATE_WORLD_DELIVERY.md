# P02-17 canonical reply PrivateWorld commit

The only relationship delivery point is a quality-gate accepted canonical reply
persisted with `reply_revision`. Its stable `delivery_id` is
`letter_id:reply_revision`; the SQLite ledger applies it once.

`DeliveryEvent` accepts only `CANONICAL_REPLY_DELIVERED` and has no stage,
intimacy, nickname, home-access, or continuation mutation payload. Confirmed
relationship and intimacy changes must use a typed command through
`PrivateWorldCommandService`; canonical delivery cannot act as an authorization
adapter.

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
