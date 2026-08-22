# P02-13 PrivateWorld behavior projection

`project_private_world` is a pure, provider-free projection. It converts hidden
numeric state into the finite `PrivateBehaviorView` enums accepted by
`ReplyContext`; raw scores and the complete snapshot never enter the prompt.

Current nickname permissions are returned as bounded labels. Home access is
projected only as the finite permission enum used to decide whether already
retrieved home history may be acknowledged; it does not create or describe a
home scene.

Local Continuation is filtered per fact. `control_only` and `pending`
statements, identifiers, and awareness labels never enter the model payload.
Only `character_known` facts become typed `KnownContinuationFact` values in
`PrivateBehaviorView`. The reviewer and one-shot rewriter receive the same
known-fact subset, but never hidden scores or control state.

The projection does not persist, change awareness, infer relationship events,
or promote a fact from pending to known. Those changes require the explicit
local administration boundary.
