# P02-17 canonical reply PrivateWorld commit

The only relationship delivery point is a quality-gate accepted canonical reply
persisted with `reply_revision`. Its stable `delivery_id` is
`letter_id:reply_revision`; the SQLite ledger applies it once.

The letter is first stored with `private_world_status=PENDING`, then committed.
Startup recovery retries pending records without changing or deleting canonical
text. An unavailable backend leaves the reply intact and records only a stable
error code. Generation failure, quality block, request retry, TTS/video/music
failure, and media rerender never call the commit path.
