# P02-07 deterministic ReplyPolicy

`runtime/reply/reply_policy.py` performs narrow, deterministic checks on a reply candidate
and a typed `ReplyContext`. It returns stable `ViolationCode` values, hard/soft
severity, and character spans. It does not call a model, rewrite text, persist
state, or attempt broad semantic hallucination detection.

Hard checks cover output length, standalone stage directions in spoken output,
known internal control markup, serialized private-state keys, and a small list
of explicit permanent-availability or exclusive-relationship promises.

Shared-history authorization is deliberately structured. A caller may provide
`SharedHistoryClaim` spans with an explicit authorization result; unauthorized
claims are blocked. Without that evidence, this deterministic scanner does not
guess whether ordinary prose describes invented history. Semantic review
belongs to P02-08 rather than an over-broad regular expression.

Intimacy enforcement is likewise structured: `IntimacyClaim` spans carry a
bounded tier, and the deterministic scanner rejects unsolicited claims or a
tier above the projected ceiling. The configured semantic reviewer is the
production source for both the current-turn request classification and claims.
A claim tuple is candidate-bound; after rewriting, the new candidate is
reviewed again and scanned only with its fresh spans. Disabled review can use
the explicit compatibility input, but it fails closed with
`FRESH_INTIMACY_CLAIMS_REQUIRED` if a rewrite makes those spans stale.

Violations contain only code, severity, and offsets. They do not duplicate the
candidate text, private state, prompt, or evidence content.
