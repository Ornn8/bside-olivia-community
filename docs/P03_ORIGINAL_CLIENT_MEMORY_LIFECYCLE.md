# P03 original-client Mem0 lifecycle

The original Olivia Settings surface is the sole user interface for this
optional local Mem0 lifecycle. It exposes only confirmed loopback `POST`
operations:

- `/toy/companion/memory/pause`
- `/toy/companion/memory/resume`
- `/toy/companion/memory/clear`
- `/toy/companion/memory/embedding/install`

All use the existing `p03.original-companion-mutation.v1` envelope and require
a loopback origin plus `X-Olivia-Companion-Action: confirmed`. Pause and resume
are idempotent by request ID; embedding installation is single-flight per cache
and returns `NOOP` after verified readiness.

`clear` is the independently reviewed destructive operation for the normalized
current local user. It is available only in the existing original Settings
memory panel, first requires the confirmed mutation header, then requires an
explicit `confirmed: true` request field after a second user confirmation. Its
default is never to execute. The operation deletes only that user's Mem0
`CONVERSATION_MEMORY` records through the existing adapter; it never reads,
writes, or deletes Archive or PrivateWorld data.

Clear uses the same audit-file lifecycle lock as the final Mem0 delivery gate.
An in-flight canonical write therefore completes before clear starts, and clear
removes that write rather than allowing it to reappear afterward. Before any
delete, the ledger persists the normalized request payload fingerprint and the
exact target IDs as a user-scoped pending intent. That intent blocks later
Mem0 writes for the same user; restart or retry resumes only its recorded IDs,
then re-reads the target domain before recording a terminal result. Each
terminal result, including `NOOP`, is scoped by normalized `(user_id,
request_id)` and its payload fingerprint: only the same payload is
`DUPLICATE`, while a different payload is a conflict and the same request ID
for another user remains independent. Provider, Qdrant, or audit failures fail
closed with stable path-free errors; the canonical reply body path continues
independently.

## Explicit embedding installation

When the existing Mem0 runtime reports `MEM0_EMBEDDING_CACHE_UNAVAILABLE`, the
same original Settings memory panel shows the concise `安装 Embedding` action.
Only this confirmed action may contact Hugging Face, anonymously and without an
API key. It downloads the fixed `BAAI/bge-small-zh-v1.5` revision
`7999e1d3359715c523056ef9478215996d62a620` into a staging directory, calculates
the existing manifest's per-file SHA-256 values, verifies the exact snapshot
contract, and promotes it only after verification. Any download, hash, or
ordinary promotion failure is path-free, leaves no READY cache, cleans staging,
and can be retried. The sole exception is a pre-commit rollback restore failure:
it returns the stable rejected result without claiming READY and retains the
recoverable staging/backup for diagnosis or recovery. The installed runtime
still passes `local_files_only=True`; a subsequent local service start reuses
the verified cache offline.

The confirmed mutation starts the single background job and returns promptly;
it does not use the ordinary short mutation timeout as a download deadline.
`/toy/companion/status` exposes the shared embedding state as `missing`,
`installing`, `ready`, or `error` under `capabilities.memory.embedding`. The
same Settings panel polls that read state while installing, then shows either a
retryable error or the offline-ready restart instruction. A ready embedding does
not by itself claim that the already-started Mem0 runtime is ready.

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
