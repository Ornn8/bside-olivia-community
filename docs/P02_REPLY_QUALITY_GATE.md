# P02-09 bounded ReplyQualityGate

`reply_quality_gate.py` runs one deterministic scan and one semantic review on
a candidate. A hard deterministic violation or reviewer `rewrite`/`block`
verdict may trigger exactly one rewrite by the original reply model. The
rewritten text is scanned and reviewed once more; there is no loop.

After the rewrite budget is consumed, a deterministic hard violation or
reviewer `block` fails closed. A final reviewer `rewrite` containing only soft
violations is accepted with warnings. Reviewer unavailability may degrade-pass
only when deterministic checks are clean. Rewrite failure is blocked with the
sanitized code `REWRITE_FAILED`.

The result exposes deterministic, reviewer, and rewrite call counts so the
one-rewrite bound is testable. This module does not generate the first
candidate, persist canonical text, update private state, render media, or wire
the server.
