# P03 original-client Mem0 lifecycle

The original Olivia Settings surface is the sole user interface for this
optional local Mem0 lifecycle. It exposes only confirmed loopback `POST`
operations:

- `/toy/companion/memory/pause`
- `/toy/companion/memory/resume`
- `/toy/companion/settings/video-reply`

These operations use the existing `p03.original-companion-mutation.v1` envelope, require a
loopback origin and `X-Olivia-Companion-Action: confirmed`, and are idempotent
by request ID. There is no clear operation in this contract; data deletion is a
separate, independently reviewable change.

The video-reply setting uses the same envelope and request replay ledger. Its
`enabled` value is a receive-boundary preference: a newly received letter
freezes the value once, so turning the setting off does not cancel video work
already received or queued, and turning it on does not upgrade an already
received text-only letter. Missing `video_reply_enabled` in a valid older
store means enabled by default; an unavailable, corrupt, or unwritable store
reports `VIDEO_REPLY_SETTINGS_UNAVAILABLE` and never claims `APPLIED`.
An exact request replay returns the original result; reusing a request ID with
a different payload returns `VIDEO_REPLY_REQUEST_CONFLICT` without changing
state.
The versioned `GET /toy/companion/status` payload may include
`capabilities.video_reply` as `{enabled, default_enabled}` or, when durable
settings are unavailable, `{state: "unavailable", reason_code:
"VIDEO_REPLY_SETTINGS_UNAVAILABLE"}`. Mutation errors are stable
`VIDEO_REPLY_ENABLED_INVALID`, `VIDEO_REPLY_REQUEST_CONFLICT`,
`VIDEO_REPLY_SETTINGS_UNAVAILABLE`, or `VIDEO_REPLY_SETTINGS_INVALID`.

## Pause and resume boundary

`pause` persists its state in the existing memory-admin audit SQLite file. The
state survives restart and is isolated by the normalized local `user_id` in
that audit file. While paused, Mem0 retrieval and every new Mem0 write are
blocked for that user. Archive rendering, canonical letter persistence, and
PrivateWorld continue unchanged.

Delivery uses two checks: the current persisted state blocks any undelivered
canonical reply immediately, and the durable per-user pause/resume window
permanently terminal-skips a reply that predates the most recent `resume` but
was not delivered before the matching `pause`. This covers fast
pause-then-resume without historical replay. The final state check and provider
write share the audit-file lifecycle lock with `pause`; the final gate repeats
both current-state and historical-window eligibility using that delivery's
canonical timestamp. Therefore once `pause` returns, no already-started
delivery can reach the provider afterwards.
`resume` only permits a later canonical reply.

Every accepted lifecycle request, including an already-paused or
already-resumed `NOOP`, is written to the existing operation ledger before its
terminal result is returned in the same SQLite transaction as its pause-window
change. The ledger is scoped by normalized `(user_id, request_id)`; a retry
with that same identity is `DUPLICATE` after restart, while reusing that ID for
the other lifecycle operation is a `MEMORY_ADMIN_REQUEST_CONFLICT`. The
deterministic audit-schema upgrade assigns pre-user-scope rows to the existing
default local user (`local-user`), rather than inferring any private identity.
That upgrade atomically renames, recreates, copies, drops, and versions the
ledger; an interrupted upgrade leaves the prior schema retryable. Status audit
and pending-correction counts use the same normalized user scope.

If lifecycle audit initialization or schema validation is unavailable,
retrieval and delivery fail closed with the stable public reason code
`MEMORY_ADMIN_AUDIT_UNAVAILABLE`. That unavailable lifecycle reason takes
precedence over stale startup or live outbox snapshots in health. `/toy/companion/status`
reports `PAUSED` only when its memory capability carries `MEMORY_ADMIN_PAUSED`;
it does not report `READY` for that state. The success status shape is versioned by
[`original_client_memory_lifecycle.schema.json`](../contracts/original_client_memory_lifecycle.schema.json).

This contract does not add a CLI, environment-variable activation gate,
browser console, provider installation, or any Persona, review, Live, music,
media, installer, or release behavior.
