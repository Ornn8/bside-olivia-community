# P02-13 PrivateWorld behavior projection

`project_private_world` is a pure, provider-free projection. It converts hidden
numeric state into the finite `PrivateBehaviorView` enums accepted by
`ReplyContext`; raw scores and the complete snapshot never enter the prompt.

Current nickname permissions are returned as bounded labels. The model receives
only the boolean `home_history_allowed`, which decides whether already retrieved
home history may be acknowledged; raw home-access levels never enter the
projection and it does not create or describe a home scene.

Relationship projection adds two finite fields: `intimacy_ceiling`, derived
only from the relationship stage, and `granted_intimacy`, the highest persisted
grant tier. A grant statement, grant identifier, growth-window field, or raw
score is never projected. The stage mapping and append-only semantics are
defined in [`P02_20_INTIMACY_MODEL.md`](P02_20_INTIMACY_MODEL.md).

Local Continuation is filtered per fact. `control_only` and `pending`
statements, identifiers, and awareness labels never enter the model payload.
Only `character_known` facts become typed `KnownContinuationFact` values in
`PrivateBehaviorView`. The reviewer and one-shot rewriter receive the same
known-fact subset, but never hidden scores or control state.

The projection does not persist, change awareness, infer relationship events,
or promote a fact from pending to known. Those changes require the explicit
local administration boundary.
