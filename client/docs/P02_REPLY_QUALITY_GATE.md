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
the full request. For a Letter, that outer budget includes the two possible
same-layer transport retries, two conditional adjudications, and the single
rewrite between the initial and final five-layer reviews.

After the rewrite budget is consumed, a deterministic hard violation or
reviewer `block` fails closed. A final reviewer `rewrite` containing only soft
`STYLE_DRIFT` or `GENERIC_COUNSELOR` findings is accepted with warnings in every
communication mode, including text letters. Hard style findings (such as broken
text), factual or relationship findings, unknown codes, and an unexplained
`rewrite` verdict remain blocked. An enabled reviewer becoming unavailable is
blocked; a deliberately disabled reviewer may degrade-pass clean deterministic
checks. Rewrite failure is blocked with a sanitized rewrite error code.

The result exposes deterministic, reviewer, and rewrite call counts so the
one-rewrite bound is testable. No candidate or reviewer material is persisted;
only the canonical text, quality status, and violation codes reach storage.

Diagnostic bundles and failure logs expose `quality_violation_codes` through a
finite allowlist of policy and reviewer codes, deduplicated and bounded to the
first 32 input entries. They do not export candidate text, review explanations,
arbitrary code-shaped strings, or private evidence. The optional manifest
feature `reply_quality_violation_codes` identifies support for this metadata;
older records without it remain readable without inventing a violation reason.
