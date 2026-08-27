# P02 bounded ReplyQualityGate

`runtime/reply/reply_quality_gate.py` runs one deterministic scan and one semantic review on
a candidate. A hard deterministic violation or reviewer `rewrite`/`block`
verdict may trigger exactly one rewrite by the original configured reply
Provider. The rewritten text is scanned and reviewed once more; there is no
loop.

The extended runtime adapters receive the prepared generation messages only to
recover a bounded current-user excerpt. The reviewer sees no raw archive or
hidden state. The rewriter receives the current user message capped at 3000
characters, public Persona review rules, trusted facts, candidate, mode, output
constraints, and violation codes. It returns replacement plain text only.

`ReplyPipeline` executes the synchronous gate in a worker thread. Provider
review and rewrite calls therefore do not block the aiohttp event loop. Each
model call has its own bounded timeout, and the outer reply timeout still owns
the full request.

After the rewrite budget is consumed, a deterministic hard violation or
reviewer `block` fails closed. A final reviewer `rewrite` containing only soft
violations is accepted with warnings. Reviewer unavailability may degrade-pass
only when deterministic checks are clean. Rewrite failure is blocked with the
sanitized code `REWRITE_FAILED`.

The result exposes deterministic, reviewer, and rewrite call counts so the
one-rewrite bound is testable. No candidate or reviewer material is persisted;
only the canonical text, quality status, and violation codes reach storage.
