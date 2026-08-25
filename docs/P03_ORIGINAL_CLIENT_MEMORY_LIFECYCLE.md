# P03 original-client Mem0 lifecycle

The original Olivia Settings surface is the sole user interface for this
optional local Mem0 lifecycle. It exposes only confirmed loopback `POST`
operations:

- `/toy/companion/memory/pause`
- `/toy/companion/memory/resume`

Both use the existing `p03.original-companion-mutation.v1` envelope, require a
loopback origin and `X-Olivia-Companion-Action: confirmed`, and are idempotent
by request ID. There is no clear operation in this contract; data deletion is a
separate, independently reviewable change.

## Pause and resume boundary

`pause` persists its state in the existing memory-admin audit SQLite file. The
state survives restart. While paused, Mem0 retrieval and every new Mem0 write
are blocked. Archive rendering, canonical letter persistence, and PrivateWorld
continue unchanged.

Delivery uses two checks: the current persisted state blocks any undelivered
canonical reply immediately, and the durable pause window prevents replies
completed during a prior pause from being backfilled after `resume`. The final
state check and provider write share the audit-file lifecycle lock with `pause`;
therefore once `pause` returns, no already-started delivery can reach the
provider afterwards. `resume` only permits future canonical replies.

If lifecycle audit initialization or schema validation is unavailable,
retrieval and delivery fail closed with the stable public reason code
`MEMORY_ADMIN_AUDIT_UNAVAILABLE`. `/toy/companion/status` reports `PAUSED` only
when its memory capability carries `MEMORY_ADMIN_PAUSED`; it does not report
`READY` for that state. The success status shape is versioned by
[`original_client_memory_lifecycle.schema.json`](../contracts/original_client_memory_lifecycle.schema.json).

This contract does not add a CLI, environment-variable activation gate,
browser console, provider installation, or any Persona, review, Live, music,
media, installer, or release behavior.
