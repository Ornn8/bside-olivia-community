# P02 reply reviewer and configured-model transport

`runtime/reply/reply_reviewer.py` keeps the provider-neutral JSON contract;
`reply_reviewer.py` remains an exact module alias for legacy imports. The response must
match `contracts/reply_review.schema.json` with id
`p02.reply-review.v2`: verdict, bounded violations, four 0-100 consistency
scores, completed status, an explicit current-turn `intimacy_request`, and
candidate-bound `intimacy_claims`. A completed review must contain both
intimacy fields; a disabled, unavailable, or invalid review contains neither,
so missing assessment cannot be confused with a completed empty claim list.

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
- the bounded relationship stage, intimacy ceiling, and granted tier (never
  grant statements, growth windows, or raw scores);
- character-known Local Continuation facts already filtered by
  `PrivateWorld` projection;
- the public Persona profile and a bounded set of current-mode/personality
  rules;
- at most 600 characters of the current user message as an identified excerpt.
- for the identity/relationship layer only, at most 1,200 characters from
  canonical `history:` records in the current memory selection that are
  explicitly attributed to Linli; prior user and unmarked records are excluded.

The production memory selector also creates a frozen `TrustedReviewEvidence`
value from those same validated records. The reviewer accepts relationship
history only through that separate typed channel; it never parses Persona
prompt blocks for authority. A preloaded message may forge `history_actor`, a
stable-looking fragment id, and a `character_reply:` prefix, but reviewer
history still remains empty. When the current selection has no reliably
attributed Linli reply, character reply history is empty.

It does not receive PrivateWorld numeric values, raw home-access records,
pending/control-only continuation facts, databases, filesystem paths, provider
configuration, or the complete archive. Review prompts and responses are not
persisted.

The existing `identity_boundary` layer owns the intimacy assessment in its
normal layer call. It may emit `STAGE_DRIFT`,
`ACKNOWLEDGED_FEELING_REWRITE`, `INTIMACY_VIOLATION`,
`UNSOLICITED_INTIMACY`, or `RELATIONSHIP_RETRACTION`. A user's wishes,
self-labels, unilateral nicknames, repeated messages, or lack of refusal do
not advance a relationship. Future debt, imagined contact, metaphor, and a
user's unilateral claim are not completed intimacy. Linli's refusal,
disagreement, fatigue, or short reply is autonomy rather than a violation
unless it contradicts confirmed history. Liking the conversation is not
evidence that Linli likes the user.

The continuity layer distinguishes unsupported factual memory from ordinary
emotional acknowledgment, style, current-input paraphrase, inference, and
conditional language. Ordinary inference is allowed only when it does not
assert an unsupported past or current fact. Invented current locations,
current actions, and recurring habits are `MEMORY_FABRICATION`. In text-letter
mode, a closing question is `STYLE_DRIFT` only when it adds no necessary
information or choice and merely forces continuation; useful concrete
questions remain allowed.

## Evidence-bound hard findings and adjudication

This protocol is enabled only for `text_letter`. Spoken and musical-video
reviews retain the pre-protocol response schema and call count: they neither
require `hard_evidence` nor invoke adjudication.

In `text_letter`, `identity_boundary`, `voice_style`, and `continuity_memory`
cannot create a hard finding from a score or drift flag alone. Every hard code
must have exactly one typed `hard_evidence` item with:

- a unique stable `evidence_id` and the matching hard `code`;
- zero-based, end-exclusive `start` and `end` offsets inside the current
  candidate;
- one bounded `claim_kind`: `identity_claim`, `current_fact`, `past_fact`,
  `shared_history`, `habit`, `location`, `action`, or `relationship`;
- one bounded `support_source`: `current_user`, `character_history`, `memory`,
  `world_fact`, `known_continuation`, or `none`;
- a short uppercase `reason_code` that contains no reply excerpt.

The canonical evidence field is `code`. For provider compatibility the parser
also accepts `matching_code` as its sole exact alias; an item with both, neither,
an extra field, or a value that differs from the hard finding fails closed. The
`voice_style` layer uses its own bounded claim kinds: `forced_question`,
`generic_assistant_tone`, `fixed_structure`, `forced_uplift`, `voice_mismatch`,
and `length_or_mode`.

Those three layer responses also require the boolean
`independent_soft_issue`. It is `true` only when that layer has a separate,
localized soft mismatch besides its hard claims. With no hard claim, `true`
requires score 1 and no drift, while `false` requires score 2 and no drift. A
hard claim cannot be used to infer this field. Missing, non-boolean, or
score/drift-inconsistent values make the review unavailable.

Missing, duplicate, mismatched, out-of-range, or schema-invalid evidence makes
the whole enabled Letter review unavailable and therefore fails closed. A
semantic duplicate is keyed by at least `(code, start, end)` across layers;
changing ids, kinds, sources, or reasons cannot make it distinct. Other review
layers keep their existing response schema.

Well-formed hard evidence conditionally spends at most one additional model
call per candidate. The call uses the same configured quality Gateway and
`deepseek-v4-flash`; it does not create a provider or retry path. The narrow
adjudicator receives the candidate, one shared context per trusted context
class, and claims that refer to it by `context_id`. `claim_kind` and
`support_source` remain descriptive output only; neither can select or expand
disclosure. Trusted `(layer, code)` selects the context class:

- `identity_boundary/IDENTITY_DRIFT` receives only release and canonical world
  authority;
- relationship, stage, acknowledged-feeling, intimacy, and relationship
  retraction codes from `identity_boundary` receive only release authority,
  canonical relationship/private context, and typed Linli-authored history;
- `continuity_memory/MEMORY_FABRICATION` receives only the bounded factual
  current-user excerpt and bounded assembled memory, even if the model labels
  the claim as relationship or shared history;
- `voice_style/STYLE_DRIFT` receives only release/style authority and the
  bounded current-user excerpt, never memory or Linli-authored history, even if
  model-supplied kind/source metadata asks for them;
- every other allowed combination receives only its minimal layer release
  authority.

Current-user wishes, self-labels, and untyped assembled memory never establish
a relationship. The adjudicator returns one candidate-bound `CONFIRM` or
`REJECT` decision per evidence id. A normal PASS candidate makes no
adjudication call. Malformed adjudication fails closed.

Only confirmed claims remain hard violations, using their exact candidate
spans. Up to 16 distinct `(code, span)` claims across identity, voice style, and
continuity are accepted within the existing 30,000-character fail-closed input
budget; shared contexts are serialized once, not copied per claim. If every
otherwise-hard claim is rejected and
`independent_soft_issue` is false, the original Letter candidate is immediately
`accepted_with_warnings` without using a rewriter. If it is true, the existing
rewrite path remains mandatory. A rewritten candidate always runs all five
layers again and, when needed, receives a fresh independent adjudication. No old
claim, span, or decision is reused; a confirmed hard claim that persists after
the rewrite blocks. `accepted_with_warnings` is a Letter-only quality status;
non-Letter modes retain their prior `accepted` status for soft findings.

For the one permitted Letter rewrite, the configured rewriter receives only actual
`CONFIRM` results as candidate-bound, single-use `code`/`start`/`end` evidence
(limit 16), never evidence ids, reasons, decisions, excerpts, or reasoning.
It must repair those spans without substituting unsupported facts or actions.
Invalid or inconsistent evidence fails before the rewriter; legacy code-only
calls remain, while non-Letter findings are not treated as adjudicated evidence.

Adjudication prompts and responses are transient. Candidate text is not added
to quality status, audit records, evidence objects, or logs, and the typed
evidence contains offsets and machine reason codes rather than quoted text.

Each candidate is reviewed independently. After the single permitted rewrite,
the second identity review must return fresh spans and the same request
classification. Stale spans, conflicting claim sources, changed request
classification, or malformed metadata fail closed. This adds no sixth review
layer; evidence-bound identity, voice-style, and continuity hard findings share
the single conditional adjudication call described above. An explicit hard
`STYLE_DRIFT` that remains after the rewrite is blocked; only a localized
voice-style score of 1 with no hard code and no drift flag may remain an
accepted warning.

## Runtime controls

- `OLIVIA_REPLY_REVIEW_ENABLED=false` disables semantic review and restores the
  existing deterministic-clean degraded path;
- `OLIVIA_REPLY_REWRITE_ENABLED=false` leaves review enabled but disables model
  rewrite;
- `OLIVIA_REPLY_REVIEW_TIMEOUT_SECONDS` sets a bounded 0.1-120 second timeout;
  the default is the smaller of 12 seconds and the configured Provider timeout.
  This control applies only to non-max review, including video modes. A scoped
  `deepseek-v4-flash` text Letter review instead uses the public
  `reasoning_timeout_seconds` setting shared by its max-reasoning stages.

Provider absence, timeout, transport errors, non-JSON output, and
schema-invalid output become sanitized `REVIEWER_UNAVAILABLE` or
`REVIEWER_RESPONSE_INVALID` results. A clean deterministic reply may then pass
as `accepted_degraded`; a hard violation cannot bypass the single-rewrite gate.

The configured gateway transport also keeps an ephemeral in-memory diagnostic tuple
for the most recently completed review. Every completion atomically replaces the tuple;
a successful completion publishes an empty tuple. It contains only bounded stage,
reason, and optional layer enums; never candidate text, raw Provider output, exception
text, or request identifiers. Known Provider invocation failures are `transport`, known
aggregation failures are `aggregation_contract`, and unexpected construction or
programming failures are `internal`. It changes neither public schemas nor external
codes and does not serialize concurrent Provider calls.
