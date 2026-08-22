# P02 reply reviewer and configured-model transport

`reply_reviewer.py` keeps the provider-neutral JSON contract. The response must
match `contracts/reply_review.schema.json` with id
`p02.reply-review.v1`: verdict, bounded violations, four 0-100 consistency
scores, and completed status.

`reply_model_quality.py` supplies the runtime transport. When a configured
non-mock Provider is present, `ReplyPipeline` replaces its `NullReviewer` with
a reviewer that calls the same configured Provider after first-generation
completion. It requests strict JSON and validates the response through the
existing adapter and schema.

## Reviewer input boundary

The model reviewer receives only:

- candidate reply text;
- current mode and output constraints;
- trusted world facts already allowed in `ReplyContext`;
- the public Persona profile and a bounded set of current-mode/personality
  rules;
- at most 600 characters of the current user message as an identified excerpt.

It does not receive PrivateWorld numeric values, control views, database
contents, filesystem paths, provider configuration, the complete archive, or
stored reviewer prompts. Review prompts and responses are not persisted.

## Runtime controls

- `OLIVIA_REPLY_REVIEW_ENABLED=false` disables semantic review and restores the
  existing deterministic-clean degraded path;
- `OLIVIA_REPLY_REWRITE_ENABLED=false` leaves review enabled but disables model
  rewrite;
- `OLIVIA_REPLY_REVIEW_TIMEOUT_SECONDS` sets a bounded 0.1-120 second timeout;
  the default is the smaller of 12 seconds and the configured Provider timeout.

Provider absence, timeout, transport errors, non-JSON output, and schema-invalid
output become sanitized `REVIEWER_UNAVAILABLE` or
`REVIEWER_RESPONSE_INVALID` results. A clean deterministic reply may then pass
as `accepted_degraded`; a hard violation cannot bypass the single-rewrite gate.
